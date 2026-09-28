"""ASR / TTS 适配器。

默认供应商是讯飞（语音听写 IAT + 在线语音合成 TTS），粤语与普通话双通道；
未配置凭据时自动降级为浏览器原生语音识别 + 浏览器语音合成 + 预置语料兜底，
现场断网也能跑完演示主线。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import ssl
from email.utils import formatdate
from urllib.parse import urlencode

from .schemas import AsrRequest, TtsRequest

try:  # websockets 只在线路可用时才需要，缺失不影响降级模式
    import websockets
except ImportError:  # pragma: no cover - 取决于运行环境
    websockets = None

# 预置语料：现场无法收音时用于兜底演示，普通话与粤语各一条。
FALLBACK_TRANSCRIPTS = {
    "mandarin": "这个月钱都花哪了，有没有乱扣费",
    "cantonese": "畀我个孙转五百蚊",
}

FALLBACK_LINES = [
    {"dialect": "mandarin", "text": "给孙子转五百块"},
    {"dialect": "mandarin", "text": "给我女儿转五千块"},
    {"dialect": "mandarin", "text": "给老李转五十块"},
    {"dialect": "mandarin", "text": "这个月钱都花哪了"},
    {"dialect": "mandarin", "text": "我的卡丢了，赶紧挂失"},
    {"dialect": "mandarin", "text": "有没有乱扣费的订阅，帮我取消"},
    {"dialect": "mandarin", "text": "最近有什么安排要提醒我"},
    {"dialect": "cantonese", "text": "畀我个孙转五百蚊"},
    {"dialect": "cantonese", "text": "睇下我今个月用咗几多钱"},
    {"dialect": "cantonese", "text": "我张卡唔见咗，帮我挂失"},
]

# --------------------------------------------------------------------------
# 讯飞开放平台：鉴权与协议
# --------------------------------------------------------------------------

IAT_HOST, IAT_PATH = "iat-api.xfyun.cn", "/v2/iat"      # 语音听写
TTS_HOST, TTS_PATH = "tts-api.xfyun.cn", "/v2/tts"      # 在线语音合成

IAT_CHUNK_BYTES = 1280          # 每帧约 40ms 的 16k/16bit 单声道 PCM
ACCENT = {"mandarin": "mandarin", "cantonese": "cantonese"}


def xfyun_auth_url(host: str, path: str, api_key: str, api_secret: str,
                   when=None) -> str:
    """按讯飞开放平台规范生成带签名的 WebSocket 鉴权 URL（HMAC-SHA256）。"""
    date = formatdate(timeval=when, localtime=False, usegmt=True)
    origin = f"host: {host}\ndate: {date}\nGET {path} HTTP/1.1"
    signature = base64.b64encode(
        hmac.new(api_secret.encode(), origin.encode(), hashlib.sha256).digest()).decode()
    authorization = base64.b64encode(
        f'api_key="{api_key}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{signature}"'.encode()).decode()
    return f"wss://{host}{path}?" + urlencode(
        {"authorization": authorization, "date": date, "host": host})


def iat_frames(app_id: str, audio_b64: str, dialect: str) -> list[str]:
    """把音频切成讯飞要求的帧。status: 0 首帧 / 1 中间帧 / 2 末帧。"""
    chunks = [audio_b64[i:i + IAT_CHUNK_BYTES]
              for i in range(0, len(audio_b64), IAT_CHUNK_BYTES)] or [""]
    business = {"language": "zh_cn", "domain": "iat", "vad_eos": 3000,
                "accent": ACCENT.get(dialect, "mandarin")}
    frames = []
    for index, chunk in enumerate(chunks):
        if len(chunks) == 1:
            status = 2
        elif index == 0:
            status = 0
        elif index == len(chunks) - 1:
            status = 2
        else:
            status = 1
        frames.append(json.dumps({
            "common": {"app_id": app_id},
            "business": business,
            "data": {"status": status, "format": "audio/L16;rate=16000",
                     "encoding": "raw", "audio": chunk},
        }))
    return frames


def iat_text(message: str) -> str:
    """从一帧识别结果里取出文字。"""
    result = json.loads(message).get("data", {}).get("result")
    if not result:
        return ""
    return "".join(cw.get("w", "") for ws in result.get("ws", []) for cw in ws.get("cw", []))


def tts_payload(app_id: str, text: str, rate: float) -> str:
    return json.dumps({
        "common": {"app_id": app_id},
        "business": {"aue": "lame", "sfl": 0, "auf": "audio/L16;rate=16000",
                     "vcn": os.environ.get("XFYUN_TTS_VCN", "xiaoyan"),
                     "tte": "UTF8", "speed": max(0, min(100, int(rate * 50)))},
        "data": {"status": 2, "text": base64.b64encode(text.encode()).decode()},
    })


# --------------------------------------------------------------------------
# 供应商可用性与真实调用
# --------------------------------------------------------------------------


def stt_available() -> bool:
    return bool(websockets and os.environ.get("XFYUN_APP_ID")
                and os.environ.get("XFYUN_API_KEY") and os.environ.get("XFYUN_API_SECRET"))


def tts_available() -> bool:
    return bool(websockets and os.environ.get("XFYUN_APP_ID")
                and os.environ.get("XFYUN_TTS_KEY") and os.environ.get("XFYUN_TTS_SECRET"))


async def _iat_collect(url: str, frames: list[str]) -> str:
    async with websockets.connect(url, ssl=ssl.create_default_context()) as ws:
        for frame in frames:
            await ws.send(frame)
        text = ""
        while True:
            data = json.loads(await ws.recv())
            if data.get("code"):
                raise RuntimeError(f"讯飞语音听写返回错误 {data['code']}：{data.get('message')}")
            text += iat_text(json.dumps(data))
            if data.get("data", {}).get("status") == 2:
                return text


def _provider_stt(audio_b64: str, dialect: str) -> str | None:
    """接入讯飞语音听写的唯一入口；未配置凭据时返回 None，由调用方降级。"""
    if not stt_available():
        return None
    url = xfyun_auth_url(IAT_HOST, IAT_PATH,
                         os.environ["XFYUN_API_KEY"], os.environ["XFYUN_API_SECRET"])
    return asyncio.run(asyncio.wait_for(
        _iat_collect(url, iat_frames(os.environ["XFYUN_APP_ID"], audio_b64, dialect)),
        timeout=20))


async def _tts_collect(url: str, payload: str) -> str:
    async with websockets.connect(url, ssl=ssl.create_default_context()) as ws:
        await ws.send(payload)
        audio: list[str] = []
        while True:
            data = json.loads(await ws.recv())
            if data.get("code"):
                raise RuntimeError(f"讯飞语音合成返回错误 {data['code']}：{data.get('message')}")
            audio.append(data.get("data", {}).get("audio") or "")
            if data.get("data", {}).get("status") == 2:
                return "".join(audio)


def _provider_tts(text: str, rate: float) -> str | None:
    if not tts_available():
        return None
    url = xfyun_auth_url(TTS_HOST, TTS_PATH,
                         os.environ["XFYUN_TTS_KEY"], os.environ["XFYUN_TTS_SECRET"])
    return asyncio.run(asyncio.wait_for(
        _tts_collect(url, tts_payload(os.environ["XFYUN_APP_ID"], text, rate)), timeout=20))


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------


def transcribe(req: AsrRequest) -> dict:
    if req.audio_b64 and stt_available():
        try:
            text = _provider_stt(req.audio_b64, req.dialect)
            if text:
                return {"provider": "xfyun-iat", "transcript": text, "dialect": req.dialect}
        except Exception as exc:  # 云端失败一律降级，不能中断现场演示
            return {**_fallback_transcript(req), "cloud_error": str(exc)}
    if req.transcript_hint:
        # 浏览器原生语音识别（Edge/Chrome 支持 zh-CN 与 zh-HK）同样是真实 ASR。
        return {"provider": "browser-native-stt", "transcript": req.transcript_hint,
                "dialect": req.dialect}
    return _fallback_transcript(req)


def _fallback_transcript(req: AsrRequest) -> dict:
    return {"provider": "offline-fallback",
            "transcript": FALLBACK_TRANSCRIPTS.get(req.dialect, FALLBACK_TRANSCRIPTS["mandarin"]),
            "dialect": req.dialect}


def synthesize(req: TtsRequest) -> dict:
    if tts_available():
        try:
            audio = _provider_tts(req.text, req.rate)
            if audio:
                return {"provider": "xfyun-tts", "text": req.text, "dialect": req.dialect,
                        "audio_b64": audio, "audio_format": "mp3"}
        except Exception as exc:
            return {**browser_tts(req), "cloud_error": str(exc)}
    return browser_tts(req)


def browser_tts(req: TtsRequest) -> dict:
    return {
        "provider": "browser-speech-synthesis",
        "text": req.text,
        "dialect": req.dialect,
        "voice_hint": "zh-HK" if req.dialect == "cantonese" else "zh-CN",
        "rate": req.rate,
        "audio_b64": None,
    }


def fallback_lines() -> list[dict]:
    return FALLBACK_LINES

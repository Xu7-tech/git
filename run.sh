#!/usr/bin/env bash
# 一键启动演示（macOS / Linux）。真实 ASR/LLM 凭据可选。
set -e
export PYTHONIOENCODING=utf-8

# 可选：接入真实模型与云语音
#
# 推荐做法：cp .env.example .env 后把密钥填进去，启动时会自动读取。
#
#   export LLM_BASE_URL=https://api.deepseek.com/v1   # 换供应商只改这里
#   export LLM_API_KEY=sk-...
#   export LLM_MODEL=deepseek-chat
#   export XFYUN_APP_ID=... XFYUN_API_KEY=... XFYUN_API_SECRET=...
#   export XFYUN_TTS_KEY=... XFYUN_TTS_SECRET=... XFYUN_TTS_VCN=xiaoyan
#
# 模型有 8 秒超时上限，网络不稳时每条指令会先等超时再回落；
# 现场演示若无稳定网络，建议不配密钥，走本地规则解析。

python -m pip install -q -r requirements.txt

PORT="${PORT:-8077}"
python -m uvicorn backend.main:app --host 127.0.0.1 --port "$PORT"

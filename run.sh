#!/usr/bin/env bash
# 一键启动演示（macOS / Linux）。真实 ASR/LLM 凭据可选。
set -e
export PYTHONIOENCODING=utf-8

# 可选：接入真实模型与云语音
#   export DASHSCOPE_API_KEY=sk-...
#   export XFYUN_APP_ID=... XFYUN_API_KEY=... XFYUN_API_SECRET=...
#   export XFYUN_TTS_KEY=... XFYUN_TTS_SECRET=... XFYUN_TTS_VCN=xiaoyan

python -m pip install -q -r requirements.txt

PORT="${PORT:-8077}"
python -m uvicorn backend.main:app --host 127.0.0.1 --port "$PORT"

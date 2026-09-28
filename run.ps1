# 一键启动演示（Windows）。真实 ASR/LLM 凭据可选，不配置也能跑完整主线。
$env:PYTHONIOENCODING = "utf-8"

# 可选：接入真实模型与云语音（不设置则自动降级为规则引擎 + 浏览器语音）
#   $env:DASHSCOPE_API_KEY = "sk-..."     # 通义千问意图理解
#   $env:XFYUN_APP_ID      = "..."        # 讯飞语音听写（粤语/普通话）
#   $env:XFYUN_API_KEY     = "..."
#   $env:XFYUN_API_SECRET  = "..."
#   $env:XFYUN_TTS_KEY     = "..."
#   $env:XFYUN_TTS_SECRET  = "..."
#   $env:XFYUN_TTS_VCN     = "xiaoyan"    # 粤语需换成账号可用的粤语音色

python -m pip install -q -r requirements.txt

$port = 8077
Start-Process "http://127.0.0.1:$port"
python -m uvicorn backend.main:app --host 127.0.0.1 --port $port

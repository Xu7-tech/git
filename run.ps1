# 一键启动演示（Windows）。真实 ASR/LLM 凭据可选，不配置也能跑完整主线。
$env:PYTHONIOENCODING = "utf-8"

# 可选：接入真实模型与云语音（不设置则自动降级为规则解析器 + 浏览器语音）
#
# 推荐做法：cp .env.example .env 后把密钥填进去，本脚本会自动读取，不用每次设置。
#
# 也可以直接在这里设置：
#   $env:LLM_BASE_URL = "https://api.deepseek.com/v1"   # 换供应商只改这里
#   $env:LLM_API_KEY  = "sk-..."                        # DeepSeek 密钥
#   $env:LLM_MODEL    = "deepseek-chat"
#   $env:XFYUN_APP_ID = "..."                           # 讯飞语音听写（粤语/普通话）
#   $env:XFYUN_API_KEY = "..."
#   $env:XFYUN_API_SECRET = "..."
#   $env:XFYUN_TTS_KEY = "..."
#   $env:XFYUN_TTS_SECRET = "..."
#   $env:XFYUN_TTS_VCN = "xiaoyan"                      # 粤语需换成账号可用的粤语音色
#
# 注意：模型有 8 秒超时上限，网络不稳时每条指令会先等超时再回落。
#       现场演示若没有稳定网络，建议不配密钥，走本地规则解析（毫秒级）。

python -m pip install -q -r requirements.txt

$port = 8077
Start-Process "http://127.0.0.1:$port"
python -m uvicorn backend.main:app --host 127.0.0.1 --port $port

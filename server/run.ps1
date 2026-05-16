$env:ASR_PROVIDER="xunfei"
if (-not $env:XUNFEI_APPID) { $env:XUNFEI_APPID="" }
if (-not $env:XUNFEI_APISECRET) { $env:XUNFEI_APISECRET="" }
if (-not $env:XUNFEI_APIKEY) { $env:XUNFEI_APIKEY="" }
$env:LLM_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
$env:LLM_MODEL="qwen-flash"
if (-not $env:LLM_API_KEY) { $env:LLM_API_KEY="" }
if (-not $env:VOICEPRINT_PROVIDER) { $env:VOICEPRINT_PROVIDER="tencent" }
if (-not $env:TENCENT_SECRET_ID) { $env:TENCENT_SECRET_ID="" }
if (-not $env:TENCENT_SECRET_KEY) { $env:TENCENT_SECRET_KEY="" }
$env:DB_PATH=""

python server.py

#!/usr/bin/env python3
"""
psy-guard WebSocket 服务
支持三种 ASR 模式（ASR_PROVIDER 环境变量切换）：

  local   (默认) — FunASR WebSocket + 本地 LLM（OpenAI-compatible）
  api            — 云端 Whisper-compatible STT + 云端 LLM API
  xunfei         — 讯飞实时语音转写 WebSocket（持久流式，边说边出字）

通用 LLM 配置：
  LLM_BASE_URL  e.g. https://dashscope.aliyuncs.com/compatible-mode/v1
  LLM_MODEL     推荐 qwen-flash
  LLM_API_KEY   API Key

讯飞实时 ASR 配置（ASR_PROVIDER=xunfei 时生效）：
  XUNFEI_APPID      讯飞应用 APPID
  XUNFEI_APISECRET  讯飞 APISecret
  XUNFEI_APIKEY     讯飞 APIKey
"""

import asyncio
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import ssl
import struct
import time
import traceback
import uuid
import wave
from collections import deque
from email.utils import formatdate
from urllib.parse import urlencode

import aiohttp
import aiohttp.web
import aiosqlite
import websockets

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("psy-guard")

# ─────────────────────────────────────────────────────────────
#  配置
# ─────────────────────────────────────────────────────────────
PORT             = int(os.getenv("PORT", "8097"))
ASR_PROVIDER     = os.getenv("ASR_PROVIDER", "local").lower()
FUNASR_WS_URL    = os.getenv("FUNASR_WS_URL", "ws://localhost:10095")
ASR_API_URL      = os.getenv("ASR_API_URL", "https://api.openai.com/v1")
ASR_API_KEY      = os.getenv("ASR_API_KEY", "")
ASR_MODEL        = os.getenv("ASR_MODEL", "whisper-1")
XUNFEI_APPID     = os.getenv("XUNFEI_APPID", "")
XUNFEI_APISECRET = os.getenv("XUNFEI_APISECRET", "")
XUNFEI_APIKEY    = os.getenv("XUNFEI_APIKEY", "")
LLM_BASE_URL     = os.getenv("LLM_BASE_URL", "http://localhost:8086/v1")
LLM_MODEL        = os.getenv("LLM_MODEL", "gemma-4-E4B-it-Q4_K_M.gguf")
LLM_API_KEY      = os.getenv("LLM_API_KEY", "none")

SAMPLE_RATE      = 16000
SAMPLE_WIDTH     = 2
CHANNELS         = 1

# 批处理模式参数（local/api 模式）
WINDOW_SEC       = float(os.getenv("WINDOW_SEC", "5"))
WINDOW_BYTES     = int(WINDOW_SEC * SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)

MIN_TEXT_LEN     = int(os.getenv("MIN_TEXT_LEN", "2"))
CONTEXT_MAX_CHARS= int(os.getenv("CONTEXT_MAX_CHARS", "300"))
LLM_CONCURRENCY  = int(os.getenv("LLM_CONCURRENCY", "1"))

# 流式模式触发 LLM 的文字积累阈值
STREAM_LLM_CHARS = int(os.getenv("STREAM_LLM_CHARS", "10"))

DB_PATH          = os.getenv("DB_PATH", "/data/psy-guard.db")
ADMIN_WEBHOOK_URL= os.getenv("ADMIN_WEBHOOK_URL", "")
AUDIO_SAVE_DIR   = os.getenv("AUDIO_SAVE_DIR", "/data/recordings")
HTTP_PORT        = int(os.getenv("HTTP_PORT", "8098"))

# ─────────────────────────────────────────────────────────────
#  Admin session tracking (globals)
# ─────────────────────────────────────────────────────────────
active_sessions: dict = {}   # session_id → {session_id, client_ip, started_at, pcm_path}
admin_connections: set = set()

async def broadcast_admin(msg: dict):
    """Broadcast a JSON message to all connected admin WebSocket clients."""
    if not admin_connections:
        return
    text = json.dumps(msg, ensure_ascii=False)
    dead = set()
    for ws in list(admin_connections):
        try:
            await ws.send(text)
        except Exception:
            dead.add(ws)
    admin_connections.difference_update(dead)

# ─────────────────────────────────────────────────────────────
#  System Prompt
# ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """你是心理咨询室实时安全监控助手。分析以下对话片段，识别潜在危机信号。

## 预警等级

### high（立即触发）
- 来访者以第一人称表达当前或近期的自杀/自伤/伤害他人意图
- 来访者描述正在遭受虐待、性侵（非回顾已解决事件）
- 示例：「我不想活了」「我准备好了刀」「我想杀了他」

### medium（警示）
- 来访者表达强烈绝望感、无价值感、反复表达孤立无援
- 咨询师疑似违规：保证疗效、建议停药、不当双重关系

### low（关注，记录但不推通知）
- 持续失眠、社交退缩、轻微负面词汇，需持续观察

## 不触发预警的情况
- 咨询师在做风险评估提问（如"你有没有想过伤害自己"）
- 讨论过去已解决的经历（降级或不触发）
- 学术/理论讨论、文学/影视作品讨论

## 输出规则
- 无危机信号：输出 null（不含任何其他内容）
- 检测到信号：输出如下 JSON，不含其他内容
{"level":"high|medium|low","keyword":"触发词","suggestion":"给咨询师的一句话干预建议"}"""

# ─────────────────────────────────────────────────────────────
#  数据库初始化
# ─────────────────────────────────────────────────────────────
async def init_db():
    if not DB_PATH:
        return None
    try:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        db = await aiosqlite.connect(DB_PATH)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id TEXT PRIMARY KEY, session_id TEXT, level TEXT,
                keyword TEXT, text TEXT, suggestion TEXT, timestamp REAL)
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transcripts (
                id TEXT PRIMARY KEY, session_id TEXT, text TEXT, timestamp REAL)
        """)
        await db.commit()
        log.info(f"[DB] SQLite ready: {DB_PATH}")
        return db
    except Exception as e:
        log.warning(f"[DB] init failed, running without persistence: {e}")
        return None

# ─────────────────────────────────────────────────────────────
#  ASR — 本地模式（FunASR WebSocket）
# ─────────────────────────────────────────────────────────────
async def transcribe_local(pcm: bytes) -> str:
    log.info(f"[ASR/local] {len(pcm)} bytes ({len(pcm)/SAMPLE_RATE/SAMPLE_WIDTH:.1f}s)")
    try:
        async with websockets.connect(FUNASR_WS_URL, max_size=10*1024*1024, open_timeout=10) as ws:
            config = {
                "mode": "2pass", "wav_name": "audio", "wav_format": "pcm",
                "is_speaking": True, "itn": True, "audio_fs": SAMPLE_RATE,
                "chunk_size": [5, 10, 5], "chunk_interval": 10,
            }
            await ws.send(json.dumps(config))
            chunk = 960 * SAMPLE_WIDTH
            for i in range(0, len(pcm), chunk):
                await ws.send(pcm[i:i+chunk])
            await ws.send(json.dumps({"is_speaking": False}))
            text = ""
            async with asyncio.timeout(30):
                async for msg in ws:
                    data = json.loads(msg)
                    if data.get("is_final") and data.get("mode") == "2pass-offline":
                        text = data.get("text", "").strip()
                        break
                    if data.get("is_final") and not data.get("mode"):
                        break
            log.info(f"[ASR/local] result: {text!r}")
            return text
    except Exception as e:
        log.error(f"[ASR/local] Error: {e}")
        return ""

# ─────────────────────────────────────────────────────────────
#  ASR — API 模式（OpenAI Whisper-compatible）
# ─────────────────────────────────────────────────────────────
async def transcribe_api(pcm: bytes, session: aiohttp.ClientSession) -> str:
    log.info(f"[ASR/api] {len(pcm)} bytes ({len(pcm)/SAMPLE_RATE/SAMPLE_WIDTH:.1f}s)")
    try:
        wav_buf = io.BytesIO()
        with wave.open(wav_buf, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm)
        wav_data = wav_buf.getvalue()
        form = aiohttp.FormData()
        form.add_field("file", wav_data, filename="audio.wav", content_type="audio/wav")
        form.add_field("model", ASR_MODEL)
        form.add_field("language", "zh")
        headers = {"Authorization": f"Bearer {ASR_API_KEY}"}
        url = f"{ASR_API_URL.rstrip('/')}/audio/transcriptions"
        async with session.post(url, data=form, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=60)) as resp:
            if resp.status == 200:
                data = await resp.json()
                text = data.get("text", "").strip()
                log.info(f"[ASR/api] result: {text!r}")
                return text
            body = await resp.text()
            log.warning(f"[ASR/api] HTTP {resp.status}: {body[:200]}")
            return ""
    except Exception as e:
        log.error(f"[ASR/api] Error: {e}")
        return ""

# ─────────────────────────────────────────────────────────────
#  讯飞鉴权 URL（HMAC-SHA256）
# ─────────────────────────────────────────────────────────────
_XUNFEI_HOST = "ws-api.xfyun.cn"
_XUNFEI_PATH = "/v2/iat"

def _xunfei_auth_url() -> str:
    date = formatdate(timeval=None, localtime=False, usegmt=True)
    sign_origin = f"host: {_XUNFEI_HOST}\ndate: {date}\nGET {_XUNFEI_PATH} HTTP/1.1"
    sig = base64.b64encode(
        hmac.new(XUNFEI_APISECRET.encode(), sign_origin.encode(), hashlib.sha256).digest()
    ).decode()
    auth = base64.b64encode(
        f'api_key="{XUNFEI_APIKEY}", algorithm="hmac-sha256", '
        f'headers="host date request-line", signature="{sig}"'.encode()
    ).decode()
    params = urlencode({"authorization": auth, "date": date, "host": _XUNFEI_HOST})
    return f"wss://{_XUNFEI_HOST}{_XUNFEI_PATH}?{params}"

# ─────────────────────────────────────────────────────────────
#  讯飞流式 ASR 会话（每个客户端一个持久 WebSocket 连接）
# ─────────────────────────────────────────────────────────────
class XunfeiStreamSession:
    """
    持久讯飞 IAT WebSocket 会话。
    单一主循环处理连接、发送、接收和重连，避免竞态。
    """
    CHUNK_SIZE      = 1280   # 40ms @ 16kHz 16bit mono
    SESSION_MAX_SEC = 55     # 接近讯飞 60s 限制前重连
    MAX_BUF_BYTES   = 1280 * 4  # 160ms 上限；重连期间积压音频超过此值则丢弃最旧的部分

    def __init__(self, on_text, on_interim=None):
        self._on_text        = on_text
        self._on_interim     = on_interim
        self._buf            = bytearray()
        self._running        = False
        self._task           = None
        self._sentence_buf: dict[int, str] = {}  # 跨重连保留，stop时 flush
        self._needs_reconnect = False  # 句子完成后主动触发重连

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def feed(self, pcm: bytes):
        if self._running:
            self._buf.extend(pcm)

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._flush_pending()  # 停录时把未确认句子全部输出
        log.info("[ASR/stream] session closed")

    async def _flush_pending(self):
        """把 sentence_buf 里所有未收到 ls=True 的句子强制输出，防止丢内容。"""
        for sn in sorted(self._sentence_buf.keys()):
            text = self._sentence_buf.pop(sn, "").strip()
            if text:
                log.info(f"[ASR/stream] flush pending sn={sn}: {text!r}")
                try:
                    await self._on_text(text)
                except Exception:
                    pass

    @staticmethod
    def _make_ssl():
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    @staticmethod
    def _silence_frame() -> bytes:
        """全零 PCM 静音帧，保持连接活跃，允许讯飞 VAD 正常检测静音边界。"""
        return bytes(XunfeiStreamSession.CHUNK_SIZE)

    async def _run(self):
        """主循环：连接 → 持续发音频 → 断连自动重连，永不退出直到 stop()。"""
        while self._running:
            ws = None
            try:
                url = _xunfei_auth_url()
                ws = await websockets.connect(
                    url, max_size=10*1024*1024, open_timeout=10, ssl=self._make_ssl()
                )
                log.info("[ASR/stream] iFlytek session opened")
                # 重连期间积压的旧音频会导致 latency 随时间线性增长；
                # 每次新 session 只保留最近 MAX_BUF_BYTES，丢弃早于此的积压。
                if len(self._buf) > self.MAX_BUF_BYTES:
                    skipped = len(self._buf) - self.MAX_BUF_BYTES
                    del self._buf[:skipped]
                    log.info(f"[ASR/stream] dropped {skipped//32}ms stale audio to maintain real-time latency")
                first    = True
                t_start  = time.time()
                recv_task = asyncio.create_task(self._recv_loop(ws))

                while self._running:
                    # 句子完成后主动重连，不等讯飞超时
                    if self._needs_reconnect:
                        self._needs_reconnect = False
                        log.info("[ASR/stream] sentence done, reconnecting for next utterance...")
                        break
                    # 接近 55s 超时时主动断开重连
                    if time.time() - t_start > self.SESSION_MAX_SEC:
                        log.info("[ASR/stream] session timeout, reconnecting...")
                        break

                    chunk = None
                    if len(self._buf) >= self.CHUNK_SIZE:
                        chunk = bytes(self._buf[:self.CHUNK_SIZE])
                        del self._buf[:self.CHUNK_SIZE]
                    else:
                        chunk = self._silence_frame()

                    status = 0 if first else 1
                    if first:
                        frame = {
                            "common": {"app_id": XUNFEI_APPID},
                            "business": {
                                "language": "zh_cn", "domain": "iat",
                                "accent": "mandarin", "vad_eos": 500,
                            },
                            "data": {
                                "status": status,
                                "format": "audio/L16;rate=16000",
                                "encoding": "raw",
                                "audio": base64.b64encode(chunk).decode(),
                            },
                        }
                        first = False
                    else:
                        frame = {
                            "data": {
                                "status": status,
                                "format": "audio/L16;rate=16000",
                                "encoding": "raw",
                                "audio": base64.b64encode(chunk).decode(),
                            }
                        }
                    await ws.send(json.dumps(frame))
                    await asyncio.sleep(0.04)

                recv_task.cancel()
                try:
                    await recv_task
                except Exception:
                    pass
                await self._flush_pending()  # 重连前 flush，防止跨 session 丢句子
                try:
                    await ws.close()
                except Exception:
                    pass

            except asyncio.CancelledError:
                if ws:
                    try:
                        await ws.close()
                    except Exception:
                        pass
                raise
            except Exception as e:
                log.warning(f"[ASR/stream] connection error: {e}, reconnecting in 0.3s...")
                if ws:
                    try:
                        await ws.close()
                    except Exception:
                        pass
                await asyncio.sleep(0.3)

    async def _recv_loop(self, ws):
        """接收讯飞识别结果，正确处理 pgs/sn/ls，只在句子最终确认后回调。"""
        try:
            async for msg in ws:
                data = json.loads(msg)
                code = data.get("code", -1)
                if code != 0:
                    log.warning(f"[ASR/stream] code={code} msg={data.get('message')}")
                    continue
                result = data.get("data", {}).get("result")
                if not result:
                    continue

                sn  = result.get("sn", 0)
                pgs = result.get("pgs", "apd")  # "apd"=追加 "rpl"=替换本句
                ls  = result.get("ls", False)    # True=本句已最终确认
                ws_list = result.get("ws", [])
                text_chunk = "".join(
                    cw.get("w", "")
                    for w in ws_list
                    for cw in w.get("cw", [])
                )

                if pgs == "rpl":
                    self._sentence_buf[sn] = text_chunk
                else:
                    self._sentence_buf[sn] = self._sentence_buf.get(sn, "") + text_chunk

                log.info(f"[ASR/stream] sn={sn} pgs={pgs} ls={ls} → {self._sentence_buf[sn]!r}")

                # 实时推送中间结果，让 iOS 端即时看到正在识别的文字
                if not ls and self._on_interim:
                    current = self._sentence_buf.get(sn, "").strip()
                    if current:
                        asyncio.create_task(self._on_interim(current))

                if ls:
                    # 先把比当前 sn 更早的孤立句子全部输出
                    for orphan_sn in sorted(k for k in self._sentence_buf if k < sn):
                        orphan_text = self._sentence_buf.pop(orphan_sn, "").strip()
                        if orphan_text:
                            asyncio.create_task(self._on_text(orphan_text))
                    full_text = self._sentence_buf.pop(sn, "").strip()
                    if full_text:
                        asyncio.create_task(self._on_text(full_text))
                    # 句子已全部确认，主动触发重连（不等讯飞 10s 超时）
                    if not self._sentence_buf:
                        self._needs_reconnect = True
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning(f"[ASR/stream] recv error: {e}")


# ─────────────────────────────────────────────────────────────
#  统一 transcribe 入口（批处理模式，local/api）
# ─────────────────────────────────────────────────────────────
async def transcribe(pcm: bytes, http_session: aiohttp.ClientSession) -> str:
    if ASR_PROVIDER == "api":
        return await transcribe_api(pcm, http_session)
    return await transcribe_local(pcm)

# ─────────────────────────────────────────────────────────────
#  LLM 分析
# ─────────────────────────────────────────────────────────────
async def analyze(session: aiohttp.ClientSession, context: str, new_text: str) -> dict | None:
    user_content = f"对话片段：\n{new_text}"
    if context:
        user_content = f"历史上文（供参考）：\n{context}\n\n{user_content}"
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_content},
        ],
        "temperature": 0.1,
        "max_tokens": 256,
    }
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    try:
        async with session.post(
            f"{LLM_BASE_URL}/chat/completions",
            json=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=45),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                log.info(f"[LLM] raw: {content!r}")
                if content.strip().lower() in ("null", "none", ""):
                    return None
                s = content.find("{")
                e = content.rfind("}") + 1
                if s >= 0 and e > s:
                    return json.loads(content[s:e])
                return None
            body = await resp.text()
            log.warning(f"[LLM] HTTP {resp.status}: {body[:200]}")
            return None
    except Exception as ex:
        log.error(f"[LLM] Error: {ex}")
        return None

# ─────────────────────────────────────────────────────────────
#  管理员 Webhook 推送
# ─────────────────────────────────────────────────────────────
async def push_admin(session: aiohttp.ClientSession, alert: dict):
    if not ADMIN_WEBHOOK_URL:
        return
    try:
        if "api.day.app" in ADMIN_WEBHOOK_URL:
            title = {"high": "高危预警", "medium": "警告", "low": "提示"}.get(alert["level"], "预警")
            body  = f"[{alert['keyword']}] {alert['text']}"
            url   = f"{ADMIN_WEBHOOK_URL.rstrip('/')}/{title}/{body}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                log.info(f"[Webhook/Bark] {r.status}")
        else:
            async with session.post(ADMIN_WEBHOOK_URL, json=alert,
                                    timeout=aiohttp.ClientTimeout(total=10)) as r:
                log.info(f"[Webhook] {r.status}")
    except Exception as e:
        log.warning(f"[Webhook] failed: {e}")

# ─────────────────────────────────────────────────────────────
#  处理一段文本（LLM 分析 + 推送）
# ─────────────────────────────────────────────────────────────
async def process_text(
    http_session: aiohttp.ClientSession,
    websocket,
    text: str,
    context_buf: deque,
    llm_sem: asyncio.Semaphore,
    session_id: str,
    db,
    send_transcript: bool = True,
):
    if not text or len(text) < MIN_TEXT_LEN:
        return

    if send_transcript:
        try:
            await websocket.send(json.dumps({"type": "transcript", "text": text}, ensure_ascii=False))
        except Exception:
            pass

    if db:
        try:
            await db.execute(
                "INSERT INTO transcripts VALUES (?,?,?,?)",
                (str(uuid.uuid4()), session_id, text, time.time())
            )
            await db.commit()
        except Exception:
            pass

    context = "".join(context_buf)
    if len(context) > CONTEXT_MAX_CHARS:
        context = context[-CONTEXT_MAX_CHARS:]

    async with llm_sem:
        alert_data = await analyze(http_session, context, text)

    context_buf.append(text)
    total = sum(len(s) for s in context_buf)
    while total > CONTEXT_MAX_CHARS and context_buf:
        removed = context_buf.popleft()
        total -= len(removed)

    if not alert_data:
        return

    alert = {
        "type":       "alert",
        "id":         str(uuid.uuid4()),
        "level":      alert_data.get("level", "low"),
        "keyword":    alert_data.get("keyword", ""),
        "text":       text,
        "suggestion": alert_data.get("suggestion", ""),
        "timestamp":  time.time(),
    }
    log.warning(f"[ALERT] level={alert['level']} kw={alert['keyword']!r} text={text!r}")

    try:
        await websocket.send(json.dumps(alert, ensure_ascii=False))
    except Exception:
        pass
    asyncio.create_task(broadcast_admin({**alert, "session_id": session_id}))

    if db:
        try:
            await db.execute(
                "INSERT INTO alerts VALUES (?,?,?,?,?,?,?)",
                (alert["id"], session_id, alert["level"], alert["keyword"],
                 alert["text"], alert["suggestion"], alert["timestamp"])
            )
            await db.commit()
        except Exception:
            pass

    if alert["level"] == "high":
        asyncio.create_task(push_admin(http_session, alert))

# ─────────────────────────────────────────────────────────────
#  批处理模式 process_window（local/api 用）
# ─────────────────────────────────────────────────────────────
async def process_window(
    http_session: aiohttp.ClientSession,
    websocket,
    pcm: bytes,
    context_buf: deque,
    llm_sem: asyncio.Semaphore,
    session_id: str,
    db,
):
    text = await transcribe(pcm, http_session)
    await process_text(http_session, websocket, text, context_buf, llm_sem, session_id, db)

# ─────────────────────────────────────────────────────────────
#  流式连接处理（ASR_PROVIDER=xunfei）
# ─────────────────────────────────────────────────────────────
async def handle_stream(websocket, db):
    """讯飞持久流式模式：音频持续推送，边说边出字，低延迟。"""
    client      = websocket.remote_address
    session_id  = str(uuid.uuid4())
    context_buf: deque[str] = deque()
    llm_sem     = asyncio.Semaphore(LLM_CONCURRENCY)
    recording   = False
    xf_session  = None
    pcm_file    = None

    _ssl_ctx = ssl.create_default_context()
    _ssl_ctx.check_hostname = False
    _ssl_ctx.verify_mode = ssl.CERT_NONE
    _conn = aiohttp.TCPConnector(ssl=_ssl_ctx)
    async with aiohttp.ClientSession(connector=_conn) as http_session:

        async def on_text(sentence: str):
            try:
                await websocket.send(json.dumps({"type": "transcript", "text": sentence}, ensure_ascii=False))
            except Exception:
                pass
            asyncio.create_task(broadcast_admin({
                "type": "transcript", "session_id": session_id, "text": sentence
            }))
            # 直接 await 而非 create_task，确保 stop()/断线时 _flush_pending 触发的最终句子
            # 在 http_session 关闭前完成 LLM 分析，防止最后几句话漏报预警。
            await process_text(http_session, websocket, sentence,
                               context_buf, llm_sem, session_id, db,
                               send_transcript=False)

        async def on_interim(text: str):
            try:
                await websocket.send(json.dumps({"type": "interim", "text": text}, ensure_ascii=False))
            except Exception:
                pass
            asyncio.create_task(broadcast_admin({
                "type": "interim", "session_id": session_id, "text": text
            }))

        try:
            async for message in websocket:
                if isinstance(message, str):
                    cmd = message.strip().upper()
                    if cmd == "START":
                        if xf_session:
                            await xf_session.stop()
                        if pcm_file:
                            pcm_file.close()
                            pcm_file = None
                        recording  = True
                        session_id = str(uuid.uuid4())
                        context_buf.clear()
                        # Open PCM file for saving audio
                        pcm_path = None
                        if AUDIO_SAVE_DIR:
                            try:
                                os.makedirs(AUDIO_SAVE_DIR, exist_ok=True)
                                pcm_path = os.path.join(AUDIO_SAVE_DIR, f"{session_id}.pcm")
                                pcm_file = open(pcm_path, "wb")
                            except Exception as e:
                                log.warning(f"[Audio] failed to open pcm file: {e}")
                        active_sessions[session_id] = {
                            "session_id": session_id,
                            "client_ip":  str(client),
                            "started_at": time.time(),
                            "pcm_path":   pcm_path,
                        }
                        asyncio.create_task(broadcast_admin({
                            "type":       "session_start",
                            "session_id": session_id,
                            "client_ip":  str(client),
                            "started_at": active_sessions[session_id]["started_at"],
                        }))
                        xf_session = XunfeiStreamSession(on_text, on_interim=on_interim)
                        await xf_session.start()
                        await websocket.send("ACK:START")
                        log.info(f"[WS] {client} START (stream) session={session_id[:8]}")
                    elif cmd == "STOP":
                        recording = False
                        if xf_session:
                            await xf_session.stop()
                            xf_session = None
                        if pcm_file:
                            pcm_file.close()
                            pcm_file = None
                        if session_id in active_sessions:
                            asyncio.create_task(broadcast_admin({
                                "type": "session_end", "session_id": session_id
                            }))
                            del active_sessions[session_id]
                        await websocket.send("ACK:STOP")
                        log.info(f"[WS] {client} STOP (stream)")
                    continue

                if not recording or not isinstance(message, bytes):
                    continue

                if xf_session:
                    await xf_session.feed(message)
                if pcm_file:
                    pcm_file.write(message)

        except websockets.exceptions.ConnectionClosed:
            log.info(f"[WS] Disconnected: {client}")
        except Exception as e:
            log.error(f"[WS] Error: {e}\n{traceback.format_exc()}")
        finally:
            if xf_session:
                await xf_session.stop()
            if pcm_file:
                try:
                    pcm_file.close()
                except Exception:
                    pass
            if session_id in active_sessions:
                asyncio.create_task(broadcast_admin({
                    "type": "session_end", "session_id": session_id
                }))
                del active_sessions[session_id]

# ─────────────────────────────────────────────────────────────
#  批处理连接处理（local/api 模式）
# ─────────────────────────────────────────────────────────────
async def handle_batch(websocket, db):
    """每 WINDOW_SEC 秒处理一批音频。"""
    client     = websocket.remote_address
    audio_buf  = bytearray()
    recording  = False
    session_id = str(uuid.uuid4())
    context_buf: deque[str] = deque()
    llm_sem    = asyncio.Semaphore(LLM_CONCURRENCY)

    _ssl_ctx2 = ssl.create_default_context()
    _ssl_ctx2.check_hostname = False
    _ssl_ctx2.verify_mode = ssl.CERT_NONE
    _conn2 = aiohttp.TCPConnector(ssl=_ssl_ctx2)
    async with aiohttp.ClientSession(connector=_conn2) as http_session:
        try:
            async for message in websocket:
                if isinstance(message, str):
                    cmd = message.strip().upper()
                    if cmd == "START":
                        recording  = True
                        session_id = str(uuid.uuid4())
                        audio_buf.clear()
                        context_buf.clear()
                        await websocket.send("ACK:START")
                        log.info(f"[WS] {client} START session={session_id[:8]}")
                    elif cmd == "STOP":
                        recording = False
                        await websocket.send("ACK:STOP")
                        log.info(f"[WS] {client} STOP")
                        if len(audio_buf) > SAMPLE_RATE * SAMPLE_WIDTH // 4:
                            asyncio.create_task(
                                process_window(http_session, websocket,
                                               bytes(audio_buf), context_buf,
                                               llm_sem, session_id, db)
                            )
                        audio_buf.clear()
                    continue

                if not recording or not isinstance(message, bytes):
                    continue

                audio_buf.extend(message)
                if len(audio_buf) >= WINDOW_BYTES:
                    chunk = bytes(audio_buf[:WINDOW_BYTES])
                    audio_buf = bytearray(audio_buf[WINDOW_BYTES:])
                    asyncio.create_task(
                        process_window(http_session, websocket, chunk,
                                       context_buf, llm_sem, session_id, db)
                    )

        except websockets.exceptions.ConnectionClosed:
            log.info(f"[WS] Disconnected: {client}")
        except Exception as e:
            log.error(f"[WS] Error: {e}\n{traceback.format_exc()}")

# ─────────────────────────────────────────────────────────────
#  管理员 WebSocket（/admin 路径）
# ─────────────────────────────────────────────────────────────
async def handle_admin(websocket):
    admin_connections.add(websocket)
    log.info(f"[Admin] connected: {websocket.remote_address}")
    try:
        await websocket.send(json.dumps({
            "type":     "session_list",
            "sessions": list(active_sessions.values()),
        }, ensure_ascii=False))
        async for _ in websocket:
            pass
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        admin_connections.discard(websocket)
        log.info(f"[Admin] disconnected: {websocket.remote_address}")

# ─────────────────────────────────────────────────────────────
#  WebSocket 入口路由
# ─────────────────────────────────────────────────────────────
async def handle(websocket, db):
    try:
        path = websocket.request.path
    except AttributeError:
        path = getattr(websocket, "path", "/")
    if path == "/admin":
        await handle_admin(websocket)
        return
    log.info(f"[WS] Connected: {websocket.remote_address}")
    if ASR_PROVIDER == "xunfei":
        await handle_stream(websocket, db)
    else:
        await handle_batch(websocket, db)

# ─────────────────────────────────────────────────────────────
#  HTTP 服务（录音下载 + 会话列表）
# ─────────────────────────────────────────────────────────────
async def _http_sessions(request):
    recordings = []
    if AUDIO_SAVE_DIR and os.path.isdir(AUDIO_SAVE_DIR):
        for fname in sorted(os.listdir(AUDIO_SAVE_DIR)):
            if fname.endswith(".pcm"):
                fpath = os.path.join(AUDIO_SAVE_DIR, fname)
                size = os.path.getsize(fpath)
                recordings.append({
                    "session_id":   fname[:-4],
                    "size_bytes":   size,
                    "duration_sec": round(size / (SAMPLE_RATE * SAMPLE_WIDTH), 1),
                })
    return aiohttp.web.Response(
        text=json.dumps(recordings, ensure_ascii=False),
        content_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )

async def _http_download(request):
    sid = request.match_info.get("session_id", "")
    if not sid or not all(c.isalnum() or c == "-" for c in sid):
        return aiohttp.web.Response(status=400, text="Invalid session ID")
    if not AUDIO_SAVE_DIR:
        return aiohttp.web.Response(status=503, text="Audio saving disabled")
    fpath = os.path.join(AUDIO_SAVE_DIR, f"{sid}.pcm")
    if not os.path.exists(fpath):
        return aiohttp.web.Response(status=404, text="Recording not found")
    with open(fpath, "rb") as f:
        pcm_data = f.read()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_data)
    return aiohttp.web.Response(
        body=buf.getvalue(),
        content_type="audio/wav",
        headers={
            "Content-Disposition": f'attachment; filename="{sid}.wav"',
            "Access-Control-Allow-Origin": "*",
        },
    )

async def start_http_server():
    app = aiohttp.web.Application()
    app.router.add_get("/sessions", _http_sessions)
    app.router.add_get("/recording/{session_id}", _http_download)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    await aiohttp.web.TCPSite(runner, "0.0.0.0", HTTP_PORT).start()
    log.info(f"[HTTP] Recording download server on port {HTTP_PORT}")

# ─────────────────────────────────────────────────────────────
#  HTTP GET /recording/{sid} on the same WS port（不依赖 8098）
# ─────────────────────────────────────────────────────────────
async def _process_request(connection, request):
    """Intercept plain HTTP GET /recording/{sid} on port 8097."""
    if not request.path.startswith("/recording/"):
        return None  # proceed with WebSocket upgrade

    from http import HTTPStatus
    from websockets.http11 import Response as WsResponse
    from websockets.datastructures import Headers as WsHeaders

    sid = request.path[len("/recording/"):].strip("/")
    if not sid or not all(c.isalnum() or c == "-" for c in sid):
        return connection.respond(HTTPStatus.BAD_REQUEST, "Invalid session ID\n")
    if not AUDIO_SAVE_DIR:
        return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "Audio saving disabled\n")
    fpath = os.path.join(AUDIO_SAVE_DIR, f"{sid}.pcm")
    if not os.path.exists(fpath):
        return connection.respond(HTTPStatus.NOT_FOUND, "Recording not found\n")

    with open(fpath, "rb") as f:
        pcm_data = f.read()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_data)
    body = buf.getvalue()
    return WsResponse(200, "OK", WsHeaders([
        ("Content-Type", "audio/wav"),
        ("Content-Disposition", f'attachment; filename="{sid}.wav"'),
        ("Content-Length", str(len(body))),
        ("Access-Control-Allow-Origin", "*"),
    ]), body)


# ─────────────────────────────────────────────────────────────
#  启动
# ─────────────────────────────────────────────────────────────
async def main():
    log.info(f"psy-guard starting on port {PORT}")
    log.info(f"ASR provider: {ASR_PROVIDER}")
    if ASR_PROVIDER == "xunfei":
        log.info(f"ASR xunfei STREAM: appid={XUNFEI_APPID or '(not set)'}")
        log.info(f"LLM trigger: per finalized sentence (MIN_TEXT_LEN={MIN_TEXT_LEN})")
    elif ASR_PROVIDER == "api":
        log.info(f"ASR API: {ASR_API_URL}  model={ASR_MODEL}")
    else:
        log.info(f"FunASR WS: {FUNASR_WS_URL}")
    log.info(f"LLM: {LLM_BASE_URL}  model={LLM_MODEL}")
    log.info(f"Admin webhook: {ADMIN_WEBHOOK_URL or '(disabled)'}")
    log.info(f"DB: {DB_PATH or '(disabled)'}")

    db = await init_db()
    await start_http_server()

    async def _handle(ws):
        await handle(ws, db)

    async with websockets.serve(_handle, "0.0.0.0", PORT, max_size=2**20, process_request=_process_request):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())

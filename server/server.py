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
import time
import traceback
import uuid
import wave
from collections import deque
from email.utils import formatdate
from urllib.parse import urlencode

import aiohttp
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

MIN_TEXT_LEN     = int(os.getenv("MIN_TEXT_LEN", "4"))
CONTEXT_MAX_CHARS= int(os.getenv("CONTEXT_MAX_CHARS", "300"))
LLM_CONCURRENCY  = int(os.getenv("LLM_CONCURRENCY", "1"))

# 流式模式触发 LLM 的文字积累阈值
STREAM_LLM_CHARS = int(os.getenv("STREAM_LLM_CHARS", "10"))

DB_PATH          = os.getenv("DB_PATH", "/data/psy-guard.db")
ADMIN_WEBHOOK_URL= os.getenv("ADMIN_WEBHOOK_URL", "")

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
    音频通过 feed() 写入，识别结果通过 on_text 回调实时返回。
    讯飞单次会话最长约 60s，自动在 55s 时重连。
    """
    CHUNK_SIZE      = 1280   # 40ms @ 16kHz 16bit mono
    SESSION_MAX_SEC = 55     # 接近讯飞 60s 限制前重连

    def __init__(self, on_text):
        # on_text(text: str) — 新识别到的文字片段（async callable）
        self._on_text   = on_text
        self._ws        = None
        self._buf       = bytearray()
        self._running   = False
        self._first     = True
        self._send_task = None
        self._recv_task = None
        self._t_start   = 0.0
        self._reconnecting = False

    async def start(self):
        self._running = True
        await self._connect()

    async def _connect(self):
        url = _xunfei_auth_url()
        self._ws      = await websockets.connect(url, max_size=10*1024*1024, open_timeout=10)
        self._first   = True
        self._t_start = time.time()
        self._send_task = asyncio.create_task(self._send_loop())
        self._recv_task = asyncio.create_task(self._recv_loop())
        log.info("[ASR/stream] iFlytek session opened")

    async def feed(self, pcm: bytes):
        """收到客户端音频，写入发送缓冲。"""
        if not self._running:
            return
        self._buf.extend(pcm)
        # 接近会话超时时自动重连
        if not self._reconnecting and time.time() - self._t_start > self.SESSION_MAX_SEC:
            asyncio.create_task(self._reconnect())

    async def _reconnect(self):
        self._reconnecting = True
        log.info("[ASR/stream] session near timeout, reconnecting...")
        await self._teardown(send_final=True)
        self._reconnecting = False  # clear before _connect so new _send_loop can run
        try:
            await self._connect()
        except Exception as e:
            log.error(f"[ASR/stream] reconnect failed: {e}")

    async def _reconnect_on_error(self):
        """连接意外断开时重连（不发 status=2 结束帧）。"""
        log.info("[ASR/stream] connection dropped, reconnecting...")
        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            try:
                await asyncio.wait_for(self._recv_task, timeout=1)
            except Exception:
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._buf.clear()
        self._reconnecting = False
        if self._running:
            try:
                await self._connect()
            except Exception as e:
                log.error(f"[ASR/stream] reconnect failed: {e}")

    async def _teardown(self, send_final=False):
        if self._send_task:
            self._send_task.cancel()
            try:
                await self._send_task
            except asyncio.CancelledError:
                pass
        if send_final and self._ws and self._ws.close_code is None:
            try:
                frame = {"data": {"status": 2, "format": "audio/L16;rate=16000",
                                  "encoding": "raw", "audio": ""}}
                await asyncio.wait_for(self._ws.send(json.dumps(frame)), timeout=2)
            except Exception:
                pass
        if self._recv_task:
            try:
                await asyncio.wait_for(self._recv_task, timeout=3)
            except Exception:
                pass
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _send_loop(self):
        """以实时速度（40ms/块）向讯飞发送 PCM 音频。"""
        while self._running and not self._reconnecting:
            if len(self._buf) >= self.CHUNK_SIZE:
                chunk = bytes(self._buf[:self.CHUNK_SIZE])
                del self._buf[:self.CHUNK_SIZE]
                status = 0 if self._first else 1
                if self._first:
                    frame = {
                        "common": {"app_id": XUNFEI_APPID},
                        "business": {
                            "language": "zh_cn", "domain": "iat",
                            "accent": "mandarin", "vad_eos": 1500,#（此处减小数值会使得实时转写语音更快）
                        },
                        "data": {
                            "status": status,
                            "format": "audio/L16;rate=16000",
                            "encoding": "raw",
                            "audio": base64.b64encode(chunk).decode(),
                        },
                    }
                    self._first = False
                else:
                    frame = {
                        "data": {
                            "status": status,
                            "format": "audio/L16;rate=16000",
                            "encoding": "raw",
                            "audio": base64.b64encode(chunk).decode(),
                        }
                    }
                try:
                    await self._ws.send(json.dumps(frame))
                except Exception as e:
                    log.warning(f"[ASR/stream] send error: {e}")
                    if self._running and not self._reconnecting:
                        self._reconnecting = True
                        asyncio.create_task(self._reconnect_on_error())
                    break
            await asyncio.sleep(0.04)

    async def _recv_loop(self):
        """接收讯飞识别结果，实时回调。"""
        try:
            async for msg in self._ws:
                data = json.loads(msg)
                code = data.get("code", -1)
                if code != 0:
                    log.warning(f"[ASR/stream] code={code} msg={data.get('message')}")
                    continue
                ws_list = data.get("data", {}).get("result", {}).get("ws", [])
                text_chunk = "".join(
                    cw.get("w", "")
                    for w in ws_list
                    for cw in w.get("cw", [])
                )
                if text_chunk:
                    log.info(f"[ASR/stream] chunk: {text_chunk!r}")
                    asyncio.create_task(self._on_text(text_chunk))
        except Exception as e:
            if self._running and not self._reconnecting:
                log.warning(f"[ASR/stream] recv error: {e}")
                self._reconnecting = True
                asyncio.create_task(self._reconnect_on_error())

    async def stop(self):
        """停止会话，等待剩余结果返回。"""
        self._running = False
        if self._send_task:
            self._send_task.cancel()
            try:
                await self._send_task
            except asyncio.CancelledError:
                pass
        # 发送结束帧
        if self._ws and self._ws.close_code is None:
            try:
                frame = {"data": {"status": 2, "format": "audio/L16;rate=16000",
                                  "encoding": "raw", "audio": ""}}
                await asyncio.wait_for(self._ws.send(json.dumps(frame)), timeout=3)
            except Exception:
                pass
            if self._recv_task:
                try:
                    await asyncio.wait_for(self._recv_task, timeout=5)
                except Exception:
                    pass
            try:
                await self._ws.close()
            except Exception:
                pass
        log.info("[ASR/stream] session closed")


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
):
    if not text or len(text) < MIN_TEXT_LEN:
        return

    # 转写结果推回客户端
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
    """讯飞持久流式 ASR：音频到了就转写，文字到了就分析。"""
    client     = websocket.remote_address
    session_id = str(uuid.uuid4())
    context_buf: deque[str] = deque()
    llm_sem    = asyncio.Semaphore(LLM_CONCURRENCY)

    # 文字积累缓冲：收到足够文字才触发 LLM
    pending    = ""
    asr_sess   = None

    async with aiohttp.ClientSession() as http_session:

        async def on_text(chunk: str):
            nonlocal pending
            pending += chunk
            # 句尾标点或积累字数达阈值时触发分析
            sentence_end = any(c in pending for c in "。！？!?")
            if len(pending) >= STREAM_LLM_CHARS or sentence_end:
                text_to_analyze = pending
                pending = ""
                asyncio.create_task(
                    process_text(http_session, websocket, text_to_analyze,
                                 context_buf, llm_sem, session_id, db)
                )

        try:
            async for message in websocket:
                if isinstance(message, str):
                    cmd = message.strip().upper()
                    if cmd == "START":
                        if asr_sess:
                            await asr_sess.stop()
                        asr_sess   = XunfeiStreamSession(on_text)
                        pending    = ""
                        context_buf.clear()
                        session_id = str(uuid.uuid4())
                        try:
                            await asr_sess.start()
                            await websocket.send("ACK:START")
                            log.info(f"[WS] {client} START (stream) session={session_id[:8]}")
                        except Exception as e:
                            log.error(f"[WS] Failed to open iFlytek session: {e}")
                            await websocket.send("ACK:START")  # 仍然 ACK，允许客户端继续
                    elif cmd == "STOP":
                        if asr_sess:
                            await asr_sess.stop()
                            asr_sess = None
                        # 分析剩余积累文字
                        if pending and len(pending) >= MIN_TEXT_LEN:
                            asyncio.create_task(
                                process_text(http_session, websocket, pending,
                                             context_buf, llm_sem, session_id, db)
                            )
                            pending = ""
                        await websocket.send("ACK:STOP")
                        log.info(f"[WS] {client} STOP (stream)")
                    continue

                if asr_sess and isinstance(message, bytes):
                    await asr_sess.feed(message)

        except websockets.exceptions.ConnectionClosed:
            log.info(f"[WS] Disconnected: {client}")
        except Exception as e:
            log.error(f"[WS] Error: {e}\n{traceback.format_exc()}")
        finally:
            if asr_sess:
                try:
                    await asr_sess.stop()
                except Exception:
                    pass

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

    async with aiohttp.ClientSession() as http_session:
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
#  WebSocket 入口路由
# ─────────────────────────────────────────────────────────────
async def handle(websocket, db):
    log.info(f"[WS] Connected: {websocket.remote_address}")
    if ASR_PROVIDER == "xunfei":
        await handle_stream(websocket, db)
    else:
        await handle_batch(websocket, db)

# ─────────────────────────────────────────────────────────────
#  启动
# ─────────────────────────────────────────────────────────────
async def main():
    log.info(f"psy-guard starting on port {PORT}")
    log.info(f"ASR provider: {ASR_PROVIDER}")
    if ASR_PROVIDER == "xunfei":
        log.info(f"ASR xunfei STREAM: appid={XUNFEI_APPID or '(not set)'}")
        log.info(f"LLM trigger: every {STREAM_LLM_CHARS} chars or sentence-end punct")
    elif ASR_PROVIDER == "api":
        log.info(f"ASR API: {ASR_API_URL}  model={ASR_MODEL}")
    else:
        log.info(f"FunASR WS: {FUNASR_WS_URL}")
    log.info(f"LLM: {LLM_BASE_URL}  model={LLM_MODEL}")
    log.info(f"Admin webhook: {ADMIN_WEBHOOK_URL or '(disabled)'}")
    log.info(f"DB: {DB_PATH or '(disabled)'}")

    db = await init_db()

    async def _handle(ws):
        await handle(ws, db)

    async with websockets.serve(_handle, "0.0.0.0", PORT, max_size=2**20):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())

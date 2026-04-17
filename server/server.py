#!/usr/bin/env python3
"""
psy-guard WebSocket 服务
支持两种模式（ASR_PROVIDER 环境变量切换）：

  local  (默认) — FunASR WebSocket + 本地 LLM（OpenAI-compatible）
  api           — 云端 Whisper-compatible STT + 云端 LLM API

通用 LLM 配置（两种模式均支持 OpenAI-compatible API）：
  LLM_BASE_URL  e.g. http://localhost:8086/v1              (本地)
                     https://dashscope.aliyuncs.com/compatible-mode/v1  (通义)
                     https://ark.cn-beijing.volces.com/api/v3            (豆包)
  LLM_MODEL     模型名
  LLM_API_KEY   API Key（本地填 none）

API 模式 STT 配置：
  ASR_API_URL   Whisper-compatible 端点，e.g. https://api.openai.com/v1
  ASR_API_KEY   API Key
  ASR_MODEL     默认 whisper-1

持久化：
  DB_PATH       SQLite 文件路径，默认 /data/psy-guard.db
                设为 "" 禁用持久化

管理员推送（可选）：
  ADMIN_WEBHOOK_URL  收到高危预警时 POST 的 Webhook URL
                     支持 Bark: https://api.day.app/<key>
                     支持 钉钉/飞书 自定义机器人
                     支持任意 POST endpoint
"""

import asyncio
import io
import json
import logging
import os
import time
import traceback
import uuid
import wave
from collections import deque

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

# ASR 模式：local | api
ASR_PROVIDER     = os.getenv("ASR_PROVIDER", "local").lower()

# 本地模式 FunASR
FUNASR_WS_URL    = os.getenv("FUNASR_WS_URL", "ws://localhost:10095")

# API 模式 STT
ASR_API_URL      = os.getenv("ASR_API_URL", "https://api.openai.com/v1")
ASR_API_KEY      = os.getenv("ASR_API_KEY", "")
ASR_MODEL        = os.getenv("ASR_MODEL", "whisper-1")

# LLM（两种模式通用）
LLM_BASE_URL     = os.getenv("LLM_BASE_URL", "http://localhost:8086/v1")
LLM_MODEL        = os.getenv("LLM_MODEL", "gemma-4-E4B-it-Q4_K_M.gguf")
LLM_API_KEY      = os.getenv("LLM_API_KEY", "none")

# 音频参数
SAMPLE_RATE      = 16000
SAMPLE_WIDTH     = 2
CHANNELS         = 1

# 分析窗口
WINDOW_SEC       = float(os.getenv("WINDOW_SEC", "5"))
WINDOW_BYTES     = int(WINDOW_SEC * SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)
MIN_TEXT_LEN     = int(os.getenv("MIN_TEXT_LEN", "4"))
CONTEXT_MAX_CHARS= int(os.getenv("CONTEXT_MAX_CHARS", "300"))
LLM_CONCURRENCY  = int(os.getenv("LLM_CONCURRENCY", "1"))

# 持久化
DB_PATH          = os.getenv("DB_PATH", "/data/psy-guard.db")

# 管理员 Webhook
ADMIN_WEBHOOK_URL= os.getenv("ADMIN_WEBHOOK_URL", "")

# ─────────────────────────────────────────────────────────────
#  System Prompt（分角色语义判断）
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
                id          TEXT PRIMARY KEY,
                session_id  TEXT,
                level       TEXT,
                keyword     TEXT,
                text        TEXT,
                suggestion  TEXT,
                timestamp   REAL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS transcripts (
                id          TEXT PRIMARY KEY,
                session_id  TEXT,
                text        TEXT,
                timestamp   REAL
            )
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
        async with websockets.connect(FUNASR_WS_URL, max_size=10 * 1024 * 1024,
                                      open_timeout=10) as ws:
            config = {
                "mode": "2pass",
                "wav_name": "audio",
                "wav_format": "pcm",
                "is_speaking": True,
                "itn": True,
                "audio_fs": SAMPLE_RATE,
                "chunk_size": [5, 10, 5],
                "chunk_interval": 10,
            }
            await ws.send(json.dumps(config))
            chunk = 960 * SAMPLE_WIDTH
            for i in range(0, len(pcm), chunk):
                await ws.send(pcm[i:i + chunk])
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
#  兼容：OpenAI / 讯飞 / 阿里云 / 任意 Whisper-compatible 端点
# ─────────────────────────────────────────────────────────────
async def transcribe_api(pcm: bytes, session: aiohttp.ClientSession) -> str:
    log.info(f"[ASR/api] {len(pcm)} bytes ({len(pcm)/SAMPLE_RATE/SAMPLE_WIDTH:.1f}s)")
    try:
        # PCM → WAV（内存中转换，不落盘）
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
#  统一 transcribe 入口
# ─────────────────────────────────────────────────────────────
async def transcribe(pcm: bytes, http_session: aiohttp.ClientSession) -> str:
    if ASR_PROVIDER == "api":
        return await transcribe_api(pcm, http_session)
    return await transcribe_local(pcm)

# ─────────────────────────────────────────────────────────────
#  LLM 分析（两种模式通用 OpenAI-compatible）
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
        # 尝试 Bark 格式（iOS Bark push）
        if "api.day.app" in ADMIN_WEBHOOK_URL:
            title = {"high": "高危预警", "medium": "警告", "low": "提示"}.get(alert["level"], "预警")
            body  = f"[{alert['keyword']}] {alert['text']}"
            url   = f"{ADMIN_WEBHOOK_URL.rstrip('/')}/{title}/{body}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                log.info(f"[Webhook/Bark] {r.status}")
        else:
            # 通用 POST JSON
            async with session.post(
                ADMIN_WEBHOOK_URL, json=alert,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                log.info(f"[Webhook] {r.status}")
    except Exception as e:
        log.warning(f"[Webhook] failed: {e}")

# ─────────────────────────────────────────────────────────────
#  处理一个音频窗口
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
    if not text or len(text) < MIN_TEXT_LEN:
        if text:
            log.info(f"[SKIP] too short: {text!r}")
        return

    # 转写文本实时推回手机
    try:
        await websocket.send(json.dumps({"type": "transcript", "text": text}, ensure_ascii=False))
    except Exception:
        pass

    # 持久化转写文本
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

    # 推回手机
    try:
        await websocket.send(json.dumps(alert, ensure_ascii=False))
    except Exception:
        pass

    # 持久化预警
    if db:
        try:
            await db.execute(
                "INSERT INTO alerts VALUES (?,?,?,?,?,?,?)",
                (alert["id"], session_id, alert["level"],
                 alert["keyword"], alert["text"], alert["suggestion"], alert["timestamp"])
            )
            await db.commit()
        except Exception:
            pass

    # 高危 → 推管理员
    if alert["level"] == "high":
        asyncio.create_task(push_admin(http_session, alert))

# ─────────────────────────────────────────────────────────────
#  每个连接的处理逻辑
# ─────────────────────────────────────────────────────────────
async def handle(websocket, db):
    client = websocket.remote_address
    log.info(f"[WS] Connected: {client}")

    audio_buf  = bytearray()
    recording  = False
    session_id = str(uuid.uuid4())
    context_buf: deque[str] = deque()
    llm_sem = asyncio.Semaphore(LLM_CONCURRENCY)

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
#  启动
# ─────────────────────────────────────────────────────────────
async def main():
    log.info(f"psy-guard starting on port {PORT}")
    log.info(f"ASR provider: {ASR_PROVIDER}")
    if ASR_PROVIDER == "api":
        log.info(f"ASR API: {ASR_API_URL}  model={ASR_MODEL}")
    else:
        log.info(f"FunASR WS: {FUNASR_WS_URL}")
    log.info(f"LLM: {LLM_BASE_URL}  model={LLM_MODEL}")
    log.info(f"Window: {WINDOW_SEC}s  min_text={MIN_TEXT_LEN}  ctx={CONTEXT_MAX_CHARS}chars")
    log.info(f"Admin webhook: {ADMIN_WEBHOOK_URL or '(disabled)'}")
    log.info(f"DB: {DB_PATH or '(disabled)'}")

    db = await init_db()

    async def _handle(ws):
        await handle(ws, db)

    async with websockets.serve(_handle, "0.0.0.0", PORT, max_size=2**20):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())

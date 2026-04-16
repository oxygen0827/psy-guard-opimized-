#!/usr/bin/env python3
"""
psy-guard WebSocket 服务
接收 iPhone BLE 中继的 PCM 音频 -> FunASR (WebSocket) 转写 -> LLM 分析 -> 预警推送

iPhone -> 服务器:
  - 二进制帧：PCM 16bit LE, 单声道, 16000Hz
  - 文本帧 "START"：开始录音
  - 文本帧 "STOP"：停止录音

服务器 -> iPhone:
  - JSON: {"type":"alert","level":"high|medium|low","keyword":"...","text":"..."}
  - 文本: "ACK:START" / "ACK:STOP"
"""

import asyncio
import json
import logging
import os
import time
import traceback
import uuid
import wave
from io import BytesIO

import aiohttp
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
PORT            = int(os.getenv("PORT", "8097"))
FUNASR_WS_URL   = os.getenv("FUNASR_WS_URL", "ws://localhost:10095")
LLM_BASE_URL    = os.getenv("LLM_BASE_URL", "http://localhost:8086/v1")
LLM_MODEL       = os.getenv("LLM_MODEL", "gemma-4-E4B-it-Q4_K_M.gguf")
LLM_API_KEY     = os.getenv("LLM_API_KEY", "none")

SAMPLE_RATE     = 16000
SAMPLE_WIDTH    = 2
CHANNELS        = 1

WINDOW_SEC      = float(os.getenv("WINDOW_SEC", "6"))
WINDOW_BYTES    = int(WINDOW_SEC * SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)

# ─────────────────────────────────────────────────────────────
#  LLM 系统提示
# ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """你是一名心理危机干预辅助系统。分析心理咨询对话片段，识别潜在危机信号。

判断标准：
- high（紧急）：明确的自杀/自伤意图、暴力威胁、急性崩溃
- medium（警示）：绝望感、无价值感、孤立、隐晦求救信号
- low（关注）：情绪低落、轻微负面词汇、需持续观察

无危机信号返回 null。检测到信号必须返回如下 JSON（不含其他内容）：
{"level":"high|medium|low","keyword":"触发词","suggestion":"给咨询师的一句话建议"}"""

# ─────────────────────────────────────────────────────────────
#  WAV 工具
# ─────────────────────────────────────────────────────────────
def pcm_to_wav_bytes(pcm: bytes) -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    return buf.getvalue()

# ─────────────────────────────────────────────────────────────
#  FunASR 转写（WebSocket 协议）
# ─────────────────────────────────────────────────────────────
async def transcribe(pcm: bytes) -> str:
    log.info(f"[ASR] transcribing {len(pcm)} bytes of PCM...")
    try:
        async with websockets.connect(FUNASR_WS_URL, max_size=10 * 1024 * 1024,
                                      open_timeout=10) as ws:
            # 1. 配置帧：2pass 模式，裸 PCM
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

            # 2. 发送裸 PCM（分块 960 字节，约 30ms/块）
            chunk_size = 960 * SAMPLE_WIDTH  # 1920 bytes
            for i in range(0, len(pcm), chunk_size):
                await ws.send(pcm[i:i + chunk_size])

            # 3. 结束帧
            await ws.send(json.dumps({"is_speaking": False}))

            # 4. 收集结果，取 2pass-offline 那条（is_final=True）
            text = ""
            async with asyncio.timeout(30):
                async for message in ws:
                    data = json.loads(message)
                    log.info(f"[ASR] raw response: {data}")
                    mode = data.get("mode", "")
                    is_final = data.get("is_final", False)
                    t = data.get("text", "").strip()
                    # 2pass-offline 是最终离线结果，取它
                    if is_final and mode == "2pass-offline" and t:
                        text = t
                        break
                    # 兜底：is_final=True 且没有 mode 字段（静音段）
                    if is_final and not mode:
                        break

            log.info(f"[ASR] result: {text!r}")
            return text
    except Exception as e:
        log.error(f"[ASR] Error: {e}\n{traceback.format_exc()}")
        return ""

# ─────────────────────────────────────────────────────────────
#  LLM 分析
# ─────────────────────────────────────────────────────────────
async def analyze(session: aiohttp.ClientSession, text: str) -> dict | None:
    if not text:
        return None
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"对话片段：\n{text}"},
        ],
        "temperature": 0.1,
        "max_tokens": 256,
    }
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    try:
        async with session.post(
            f"{LLM_BASE_URL}/chat/completions",
            json=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                log.info(f"[LLM] raw: {content!r}")
                start = content.find("{")
                end = content.rfind("}") + 1
                if start >= 0 and end > start:
                    return json.loads(content[start:end])
                return None
            else:
                body = await resp.text()
                log.warning(f"[LLM] HTTP {resp.status}: {body[:200]}")
                return None
    except Exception as e:
        log.error(f"[LLM] Error: {e}")
        return None

# ─────────────────────────────────────────────────────────────
#  处理一个音频窗口
# ─────────────────────────────────────────────────────────────
async def process_window(session: aiohttp.ClientSession, websocket, pcm: bytes):
    text = await transcribe(pcm)
    if not text:
        return

    alert_data = await analyze(session, text)
    if not alert_data:
        return

    alert = {
        "type": "alert",
        "id": str(uuid.uuid4()),
        "level": alert_data.get("level", "low"),
        "keyword": alert_data.get("keyword", ""),
        "text": text,
        "suggestion": alert_data.get("suggestion", ""),
        "timestamp": time.time(),
    }
    log.warning(f"[ALERT] level={alert['level']} kw={alert['keyword']!r} text={text!r}")
    try:
        await websocket.send(json.dumps(alert, ensure_ascii=False))
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
#  每个连接的处理逻辑
# ─────────────────────────────────────────────────────────────
async def handle(websocket):
    client = websocket.remote_address
    log.info(f"[WS] Connected: {client}")

    audio_buf = bytearray()
    recording = False

    async with aiohttp.ClientSession() as session:
        try:
            async for message in websocket:
                if isinstance(message, str):
                    cmd = message.strip().upper()
                    if cmd == "START":
                        recording = True
                        audio_buf.clear()
                        await websocket.send("ACK:START")
                        log.info(f"[WS] {client} START")
                    elif cmd == "STOP":
                        recording = False
                        await websocket.send("ACK:STOP")
                        log.info(f"[WS] {client} STOP")
                        if len(audio_buf) > SAMPLE_RATE * SAMPLE_WIDTH // 4:  # 至少 0.25 秒
                            asyncio.create_task(
                                process_window(session, websocket, bytes(audio_buf))
                            )
                        audio_buf.clear()
                    continue

                if not recording or not isinstance(message, bytes):
                    continue

                audio_buf.extend(message)

                if len(audio_buf) >= WINDOW_BYTES:
                    chunk = bytes(audio_buf[:WINDOW_BYTES])
                    audio_buf = bytearray(audio_buf[WINDOW_BYTES:])
                    asyncio.create_task(process_window(session, websocket, chunk))

        except websockets.exceptions.ConnectionClosed:
            log.info(f"[WS] Disconnected: {client}")
        except Exception as e:
            log.error(f"[WS] Error: {e}")

# ─────────────────────────────────────────────────────────────
#  启动
# ─────────────────────────────────────────────────────────────
async def main():
    log.info(f"psy-guard starting on port {PORT}")
    log.info(f"FunASR WS: {FUNASR_WS_URL}")
    log.info(f"LLM:       {LLM_BASE_URL}  model={LLM_MODEL}")
    log.info(f"Window:    {WINDOW_SEC}s ({WINDOW_BYTES} bytes)")

    async with websockets.serve(handle, "0.0.0.0", PORT, max_size=2**20):
        await asyncio.Future()

if __name__ == "__main__":
    asyncio.run(main())

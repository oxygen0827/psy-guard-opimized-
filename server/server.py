#!/usr/bin/env python3
"""
psy-guard WebSocket 服务
接收 iPhone BLE 中继的 PCM 音频 -> FunASR 转写 -> LLM 敏感内容分析 -> 预警推送

协议：
  iPhone -> 服务器:
    - 二进制帧：PCM 16bit LE, 单声道, 16000Hz 音频数据
    - 文本帧 "START"：开始录音
    - 文本帧 "STOP"：停止录音（触发最后一次分析）

  服务器 -> iPhone:
    - JSON 文本帧：AlertMessage（见下方结构）
    - 文本帧 "ACK:START" / "ACK:STOP"
"""

import asyncio
import json
import logging
import os
import struct
import tempfile
import time
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
#  配置（通过环境变量覆盖）
# ─────────────────────────────────────────────────────────────
PORT           = int(os.getenv("PORT", "8097"))
FUNASR_URL     = os.getenv("FUNASR_URL", "http://localhost:8094/transcribe")
LLM_BASE_URL   = os.getenv("LLM_BASE_URL", "http://localhost:8081/v1")
LLM_MODEL      = os.getenv("LLM_MODEL", "Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf")
LLM_API_KEY    = os.getenv("LLM_API_KEY", "none")

SAMPLE_RATE    = 16000   # Hz
SAMPLE_WIDTH   = 2       # bytes (16-bit)
CHANNELS       = 1

# 每次触发转写的音频窗口大小（秒）
WINDOW_SEC     = float(os.getenv("WINDOW_SEC", "6"))
WINDOW_BYTES   = int(WINDOW_SEC * SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)

# ─────────────────────────────────────────────────────────────
#  LLM 系统提示
# ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """你是一名心理危机干预辅助系统。你的任务是分析心理咨询对话片段，识别潜在的危机信号。

判断标准：
- high（紧急）：明确的自杀/自伤意图、暴力威胁、急性崩溃
- medium（警示）：绝望感、无价值感、孤立、隐晦的求救信号
- low（关注）：情绪明显低落、轻微负面词汇、需要持续观察

如果文本没有任何危机信号，返回 null。

只要检测到信号，必须返回如下 JSON（不含其他内容）：
{
  "level": "high|medium|low",
  "keyword": "触发词或关键短语",
  "suggestion": "给咨询师的简短干预建议（一句话）"
}
"""

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
#  FunASR 转写
# ─────────────────────────────────────────────────────────────
async def transcribe(session: aiohttp.ClientSession, pcm: bytes) -> str:
    wav_bytes = pcm_to_wav_bytes(pcm)
    data = aiohttp.FormData()
    data.add_field("file", wav_bytes, filename="audio.wav", content_type="audio/wav")
    try:
        async with session.post(FUNASR_URL, data=data, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status == 200:
                result = await resp.json()
                text = result.get("text", "").strip()
                log.info(f"[ASR] {text!r}")
                return text
            else:
                log.warning(f"[ASR] HTTP {resp.status}")
                return ""
    except Exception as e:
        log.error(f"[ASR] Error: {e}")
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
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                log.info(f"[LLM] raw: {content!r}")
                # 提取 JSON
                start = content.find("{")
                end = content.rfind("}") + 1
                if start >= 0 and end > start:
                    return json.loads(content[start:end])
                # LLM 返回 null 表示无风险
                return None
            else:
                log.warning(f"[LLM] HTTP {resp.status}")
                return None
    except Exception as e:
        log.error(f"[LLM] Error: {e}")
        return None

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

                # ── 控制指令 ──────────────────────────────────
                if isinstance(message, str):
                    cmd = message.strip().upper()
                    if cmd == "START":
                        recording = True
                        audio_buf.clear()
                        await websocket.send("ACK:START")
                        log.info(f"[WS] {client} START recording")
                    elif cmd == "STOP":
                        recording = False
                        await websocket.send("ACK:STOP")
                        log.info(f"[WS] {client} STOP recording")
                        # flush 剩余音频
                        if len(audio_buf) > SAMPLE_RATE * SAMPLE_WIDTH:
                            await process_window(session, websocket, bytes(audio_buf))
                            audio_buf.clear()
                    continue

                # ── 音频数据 ──────────────────────────────────
                if not recording or not isinstance(message, bytes):
                    continue

                audio_buf.extend(message)

                # 达到窗口大小则触发一次转写+分析
                if len(audio_buf) >= WINDOW_BYTES:
                    chunk = bytes(audio_buf[:WINDOW_BYTES])
                    audio_buf = audio_buf[WINDOW_BYTES:]
                    # 不等待分析完成，直接异步处理（不阻塞接收）
                    asyncio.create_task(process_window(session, websocket, chunk))

        except websockets.exceptions.ConnectionClosed:
            log.info(f"[WS] Disconnected: {client}")
        except Exception as e:
            log.error(f"[WS] Error: {e}")

async def process_window(
    session: aiohttp.ClientSession,
    websocket,
    pcm: bytes,
):
    """转写 + 分析 + 推送预警（一次窗口的完整流程）"""
    text = await transcribe(session, pcm)
    if not text:
        return

    alert_data = await analyze(session, text)
    if not alert_data:
        return

    alert = {
        "id": str(uuid.uuid4()),
        "level": alert_data.get("level", "low"),
        "keyword": alert_data.get("keyword"),
        "text": text,
        "suggestion": alert_data.get("suggestion", ""),
        "timestamp": time.time(),
    }
    log.warning(f"[ALERT] level={alert['level']} kw={alert['keyword']!r}")
    try:
        await websocket.send(json.dumps(alert, ensure_ascii=False))
    except Exception:
        pass

# ─────────────────────────────────────────────────────────────
#  启动
# ─────────────────────────────────────────────────────────────
async def main():
    log.info(f"psy-guard starting on port {PORT}")
    log.info(f"FunASR: {FUNASR_URL}")
    log.info(f"LLM:    {LLM_BASE_URL}  model={LLM_MODEL}")
    log.info(f"Window: {WINDOW_SEC}s ({WINDOW_BYTES} bytes)")

    async with websockets.serve(handle, "0.0.0.0", PORT, max_size=2**20):
        await asyncio.Future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())

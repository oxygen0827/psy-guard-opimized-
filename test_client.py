#!/usr/bin/env python3
"""
psy-guard 本地测试客户端

用法：
  pip install sounddevice websockets numpy
  python test_client.py                          # 连本机服务器
  python test_client.py ws://150.158.146.192:6146  # 连 Spark2

说话即可，Ctrl+C 停止。
"""

import argparse
import asyncio
import json
import sys
import threading
import queue

try:
    import sounddevice as sd
    import numpy as np
    import websockets
except ImportError:
    print("请先安装依赖: pip install sounddevice websockets numpy")
    sys.exit(1)

SAMPLE_RATE = 16000
CHANNELS    = 1
CHUNK_MS    = 100          # 每 100ms 发一次音频
CHUNK_FRAMES= int(SAMPLE_RATE * CHUNK_MS / 1000)  # 1600 frames = 3200 bytes

audio_q: queue.Queue = queue.Queue()
stop_flag = threading.Event()


def mic_callback(indata, frames, time_info, status):
    if status:
        print(f"[MIC] {status}", flush=True)
    pcm = (indata[:, 0] * 32767).astype(np.int16).tobytes()
    audio_q.put(pcm)


async def run(server_url: str):
    print(f"连接服务器: {server_url}")
    print("说话测试（Ctrl+C 退出）...\n")

    async with websockets.connect(server_url, max_size=2**20, open_timeout=10) as ws:
        ack = await ws.send("START") or await asyncio.wait_for(ws.recv(), timeout=5)
        print(f"[服务器] {ack}")

        async def receive_loop():
            async for msg in ws:
                try:
                    data = json.loads(msg)
                    t = data.get("type", "")
                    if t == "transcript":
                        print(f"\r[转写] {data['text']}", flush=True)
                    elif t == "alert":
                        level_label = {"high": "🔴 高危", "medium": "🟠 警告", "low": "🟡 关注"}.get(
                            data.get("level", ""), "⚪"
                        )
                        print(f"\n{'='*50}")
                        print(f"【预警 {level_label}】关键词: {data.get('keyword')}")
                        print(f"  触发文本: {data.get('text')}")
                        print(f"  建议: {data.get('suggestion', '')}")
                        print(f"{'='*50}\n", flush=True)
                except Exception:
                    print(f"[服务器] {msg}", flush=True)

        recv_task = asyncio.create_task(receive_loop())

        loop = asyncio.get_event_loop()

        def on_stop():
            stop_flag.wait()
            loop.call_soon_threadsafe(recv_task.cancel)

        threading.Thread(target=on_stop, daemon=True).start()

        with sd.InputStream(samplerate=SAMPLE_RATE, channels=CHANNELS,
                            dtype="float32", blocksize=CHUNK_FRAMES,
                            callback=mic_callback):
            print("[麦克风] 已开启，请说话...\n")
            try:
                while not stop_flag.is_set():
                    try:
                        pcm = audio_q.get(timeout=0.2)
                        await ws.send(pcm)
                    except queue.Empty:
                        pass
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass

        await ws.send("STOP")
        print("\n[停止录音]")
        try:
            await asyncio.wait_for(recv_task, timeout=3)
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url", nargs="?", default="ws://localhost:8097",
                        help="服务器地址，默认 ws://localhost:8097")
    args = parser.parse_args()

    url = args.url
    # 支持简写 "remote" 直接连 Spark2
    if url == "remote":
        url = "ws://150.158.146.192:6146"

    try:
        asyncio.run(run(url))
    except KeyboardInterrupt:
        stop_flag.set()
        print("\n退出。")


if __name__ == "__main__":
    main()

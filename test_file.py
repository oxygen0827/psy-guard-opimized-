#!/usr/bin/env python3
"""
用音频文件测试 psy-guard 服务器（替代麦克风）

用法：
  python3 test_file.py <音频文件> [服务器地址]

示例：
  python3 test_file.py test.m4a ws://150.158.146.192:8097

支持格式：WAV / M4A / MP3 / 任何 ffmpeg 支持的格式
依赖：pip3 install websockets numpy
      brew install ffmpeg  （转换格式用）
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile

try:
    import numpy as np
    import websockets
except ImportError:
    print("请先安装依赖: pip3 install websockets numpy")
    sys.exit(1)

SAMPLE_RATE    = 16000
CHUNK_BYTES    = 1600   # ~50ms @ 16kHz 16bit，模拟 iOS 缓冲
PLAYBACK_SPEED = 1.0    # 1.0 = 实时速率发送（模拟真实录制）


def convert_to_pcm(input_path: str) -> bytes:
    """用 macOS afconvert 把任意音频转成 16kHz 16bit mono PCM（经由 CAF 中间格式）"""
    with tempfile.NamedTemporaryFile(suffix=".caf", delete=False) as f:
        tmp_caf = f.name
    try:
        result = subprocess.run([
            "afconvert",
            "-f", "caff",
            "-d", f"LEI16@{SAMPLE_RATE}",  # @rate 才会真正重采样
            "-c", "1",
            input_path, tmp_caf
        ], capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[afconvert 错误]\n{result.stderr}")
            sys.exit(1)
        return _read_caf_pcm(tmp_caf)
    finally:
        if os.path.exists(tmp_caf):
            os.unlink(tmp_caf)


def _read_caf_pcm(caf_path: str) -> bytes:
    """从 CAF 文件中提取原始 PCM 数据（跳过 CAF 头和 data chunk 的 edit count）"""
    import struct
    with open(caf_path, "rb") as f:
        # CAF 文件头：4字节magic + 2字节version + 2字节flags
        magic = f.read(4)
        if magic != b"caff":
            raise ValueError(f"不是 CAF 文件: {magic}")
        f.read(4)  # version + flags
        # 遍历 chunks，找 data chunk
        while True:
            chunk_type = f.read(4)
            if len(chunk_type) < 4:
                break
            size_bytes = f.read(8)
            if len(size_bytes) < 8:
                break
            chunk_size = struct.unpack(">q", size_bytes)[0]
            if chunk_type == b"data":
                f.read(4)  # edit count (4 bytes)
                return f.read() if chunk_size == -1 else f.read(chunk_size - 4)
            if chunk_size == -1:
                break
            f.seek(chunk_size, 1)
    raise ValueError("CAF 文件中未找到 data chunk")


async def run(audio_file: str, server_url: str):
    print(f"音频文件: {audio_file}")
    print(f"转换为 16kHz PCM...")
    pcm = convert_to_pcm(audio_file)
    duration = len(pcm) / (SAMPLE_RATE * 2)
    print(f"音频时长: {duration:.1f} 秒，PCM 大小: {len(pcm)} 字节")
    print(f"\n连接服务器: {server_url}\n")

    for v in ("ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        os.environ.pop(v, None)

    async with websockets.connect(server_url, max_size=2**20, open_timeout=10) as ws:
        await ws.send("START")
        ack = await asyncio.wait_for(ws.recv(), timeout=5)
        print(f"[服务器] {ack}")
        print("[开始发送音频，等待转写结果...]\n")

        async def receive_loop():
            async for msg in ws:
                try:
                    data = json.loads(msg)
                    t = data.get("type", "")
                    if t == "transcript":
                        print(f"[转写] {data['text']}", flush=True)
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

        # 按实时速率分块发送，模拟真实录制流
        chunk_duration = CHUNK_BYTES / (SAMPLE_RATE * 2)
        for i in range(0, len(pcm), CHUNK_BYTES):
            chunk = pcm[i:i+CHUNK_BYTES]
            await ws.send(chunk)
            await asyncio.sleep(chunk_duration / PLAYBACK_SPEED)

        # 补发1.5秒静音，让讯飞 VAD 检测到句尾并输出最后一句
        silence = bytes(CHUNK_BYTES)
        for _ in range(int(1.5 / chunk_duration)):
            await ws.send(silence)
            await asyncio.sleep(chunk_duration)

        await ws.send("STOP")
        print("\n[音频发送完毕，等待最终结果...]")
        try:
            await asyncio.wait_for(recv_task, timeout=10)
        except asyncio.TimeoutError:
            print("[超时，结束]")
        except Exception:
            pass

    print("\n完成。")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    audio_file  = sys.argv[1]
    server_url  = sys.argv[2] if len(sys.argv) > 2 else "ws://150.158.146.192:8097"
    if not os.path.exists(audio_file):
        print(f"文件不存在: {audio_file}")
        sys.exit(1)
    asyncio.run(run(audio_file, server_url))


if __name__ == "__main__":
    main()

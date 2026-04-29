#!/usr/bin/env python3
"""
PDM 麦克风测试脚本
用法：python3 test_pdm.py
需要先把 PDMTest.ino 烧录到 XIAO，然后运行本脚本
"""

import glob
import math
import os
import struct
import subprocess
import sys
import time
import wave

SAMPLE_RATE  = 16000
DURATION_SEC = 5
TOTAL_BYTES  = SAMPLE_RATE * 2 * DURATION_SEC  # 16-bit mono


def find_xiao_port():
    candidates = glob.glob("/dev/cu.usbmodem*")
    if not candidates:
        print("❌ 找不到 XIAO 串口，请确认 XIAO 已通过 USB 连接到电脑")
        sys.exit(1)
    if len(candidates) > 1:
        print("找到多个串口，选第一个：", candidates)
    return candidates[0]


def record(port):
    import serial
    print(f"串口: {port}")
    ser = serial.Serial(port, 115200, timeout=2)
    time.sleep(1.5)  # 等 XIAO 重置

    # 读掉 "READY\n" 行
    line = ser.readline().decode(errors="ignore").strip()
    print(f"XIAO: {line}")
    if "READY" not in line:
        print("⚠️  没收到 READY，继续尝试...")

    print(f"\n✅ 开始录音 {DURATION_SEC} 秒，请贴近 XIAO 麦克风说话...")
    data = bytearray()
    t0 = time.time()
    while len(data) < TOTAL_BYTES:
        chunk = ser.read(min(1024, TOTAL_BYTES - len(data)))
        data.extend(chunk)
        elapsed = time.time() - t0
        pct = len(data) / TOTAL_BYTES * 100
        print(f"\r  已录 {pct:.0f}%  ({elapsed:.1f}s)", end="", flush=True)
    ser.close()
    print("\n录音完成")
    return bytes(data)


def analyze(pcm: bytes):
    samples = struct.unpack(f"<{len(pcm)//2}h", pcm[:len(pcm)//2*2])
    n = len(samples)
    rms     = math.sqrt(sum(s*s for s in samples) / n)
    max_amp = max(abs(s) for s in samples)
    silent  = sum(1 for s in samples if abs(s) < 200) / n * 100
    clipped = sum(1 for s in samples if abs(s) > 30000) / n * 100

    print("\n─── 音频分析 ───")
    print(f"  RMS 振幅  : {rms:.0f}   (手机麦克风正常值 ~260)")
    print(f"  最大振幅  : {max_amp}  (32768 = 削波)")
    print(f"  静音占比  : {silent:.1f}%")
    print(f"  削波占比  : {clipped:.2f}%")

    if rms < 50:
        print("\n⚠️  信号极弱，麦克风可能方向反了或 PDM 未初始化")
    elif rms > 1000 and clipped > 0.5:
        print("\n❌ 噪声太大+削波，可能是电磁干扰或增益过高")
    elif rms > 800:
        print("\n⚠️  振幅偏高，可能含噪声，听录音判断")
    else:
        print("\n✅ 振幅正常，音频质量可能OK")

    # 每秒 RMS
    print("\n─── 每秒振幅（判断是否有语音出现）───")
    chunk = SAMPLE_RATE
    for i in range(0, n, chunk):
        seg = samples[i:i+chunk]
        r = math.sqrt(sum(s*s for s in seg)/len(seg))
        bar = "█" * int(r / 150)
        print(f"  {i//chunk+1}s: {r:5.0f} {bar}")


def save_wav(pcm: bytes, path: str):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm)
    print(f"\n录音已保存到: {path}")


if __name__ == "__main__":
    try:
        import serial
    except ImportError:
        print("安装 pyserial：pip3 install pyserial")
        sys.exit(1)

    port = find_xiao_port()
    pcm  = record(port)

    wav_path = "/tmp/pdm_test.wav"
    save_wav(pcm, wav_path)
    analyze(pcm)

    print("\n▶  正在播放录音，听听是否清晰...")
    subprocess.run(["afplay", wav_path])
    print("播放完毕。")
    print("\n结论：")
    print("  如果录音清晰  → PDM 麦克风正常，问题在 BLE 传输路径")
    print("  如果噪声很大  → PDM 麦克风本身有问题（硬件/板子问题）")

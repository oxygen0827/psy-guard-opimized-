#!/usr/bin/env python3
"""
BLE 直连录音
Mac 直接通过 BLE 连接 XIAO，接收 PDM 音频，保存 WAV 到桌面
用法：python3 ble_record.py
按 Ctrl+C 随时停止，录音自动保存到 ~/Desktop/xiao_recordings/
"""

import asyncio
import math
import signal
import struct
import subprocess
import sys
import wave
from datetime import datetime
from pathlib import Path

TX_CHAR = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"
RX_CHAR = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"

SAMPLE_RATE = 8000
OUTPUT_DIR  = Path.home() / "Desktop" / "xiao_recordings"

audio_buf    = bytearray()
is_recording = False
stop_event   = asyncio.Event()
t_start      = 0.0


def on_notify(sender, data: bytearray):
    if is_recording:
        audio_buf.extend(data)


def save_wav(elapsed_sec: float):
    if len(audio_buf) < 4:
        print("\n没有收到音频数据，文件未保存。")
        return

    # 根据实际收到的字节数和录制时长推算有效采样率
    # BLE 吞吐不足时，实际接收字节 < 理论值，直接用标称 16000 Hz 写入会导致语速加快
    actual_bytes_per_sec = len(audio_buf) / elapsed_sec
    effective_rate = int(actual_bytes_per_sec / 2)  # 除以 2（16-bit 每样本 2 字节）
    effective_rate = max(4000, min(effective_rate, 48000))  # 限制在合理范围

    throughput_ratio = actual_bytes_per_sec / (SAMPLE_RATE * 2) * 100

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    fname = datetime.now().strftime("%H%M%S") + ".wav"
    fpath = OUTPUT_DIR / fname

    with wave.open(str(fpath), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(effective_rate)
        wf.writeframes(bytes(audio_buf))

    duration = len(audio_buf) / (effective_rate * 2)
    print(f"\n已保存: {fpath}")
    print(f"录制时长: {elapsed_sec:.1f}s   WAV 时长: {duration:.1f}s   大小: {len(audio_buf)/1024:.0f} KB")
    print(f"BLE 吞吐: {actual_bytes_per_sec/1024:.1f} KB/s（理论 {SAMPLE_RATE*2/1024:.0f} KB/s，实际 {throughput_ratio:.0f}%）")
    print(f"有效采样率: {effective_rate} Hz（写入 WAV 头，保证播放速度正常）")

    if throughput_ratio < 60:
        print("⚠️  BLE 吞吐严重不足，建议固件改为 8kHz 采样率")
    elif throughput_ratio < 90:
        print("⚠️  BLE 吞吐偏低，部分音频丢失")
    else:
        print("✅ BLE 吞吐正常")

    samples = struct.unpack(f"<{len(audio_buf)//2}h",
                            bytes(audio_buf[:len(audio_buf)//2*2]))
    rms     = math.sqrt(sum(s*s for s in samples) / len(samples))
    max_amp = max(abs(s) for s in samples)
    silent  = sum(1 for s in samples if abs(s) < 200) / len(samples) * 100
    print(f"RMS: {rms:.0f}   Max: {max_amp}   静音: {silent:.0f}%")
    if rms < 30:
        print("⚠️  信号极弱，可能没收到音频")
    elif rms > 1500:
        print("⚠️  振幅偏高，可能含噪声")
    else:
        print("✅ 振幅正常")


async def main():
    global is_recording, audio_buf

    try:
        from bleak import BleakScanner, BleakClient
    except ImportError:
        print("请先安装 bleak：pip3 install bleak")
        sys.exit(1)

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, stop_event.set)

    print("正在扫描 XIAO (PsyGuard)...")
    device = None
    devices = await BleakScanner.discover(timeout=10.0)
    for d in devices:
        name = d.name or ""
        if any(k in name for k in ["XIAO", "Sense", "Psy", "Arduino", "PsyGuard"]):
            device = d
            print(f"找到设备: {name}  ({d.address})")
            break

    if not device:
        print("❌ 未找到 XIAO，确认固件已烧录且蓝灯亮起")
        sys.exit(1)

    t_end = None
    try:
        async with BleakClient(device.address, timeout=15) as client:
            print("BLE 已连接 ✅")
            print("\n开始录音，按 Ctrl+C 停止并保存\n")

            await client.start_notify(TX_CHAR, on_notify)

            audio_buf    = bytearray()
            is_recording = True
            t_start      = asyncio.get_event_loop().time()
            try:
                await client.write_gatt_char(RX_CHAR, bytes([0x01]), response=False)
            except Exception:
                await client.write_gatt_char(RX_CHAR, bytes([0x01]), response=True)

            # 每秒打印进度，直到 Ctrl+C
            elapsed = 0
            while not stop_event.is_set():
                await asyncio.sleep(1)
                elapsed += 1
                kb  = len(audio_buf) / 1024
                bar = "▓" * int(kb / 4)
                print(f"  {elapsed:3d}s  {kb:6.1f} KB  {bar}", flush=True)

            t_end        = asyncio.get_event_loop().time()
            is_recording = False
            try:
                await client.write_gatt_char(RX_CHAR, bytes([0x00]), response=False)
            except Exception:
                pass
            try:
                await client.stop_notify(TX_CHAR)
            except Exception:
                pass
    except Exception as e:
        if not stop_event.is_set():
            print(f"\nBLE 错误: {e}")
    finally:
        is_recording = False
        elapsed_sec  = (t_end or asyncio.get_event_loop().time()) - t_start
        save_wav(elapsed_sec)


asyncio.run(main())

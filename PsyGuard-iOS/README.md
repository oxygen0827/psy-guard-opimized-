# PsyGuard iOS

XIAO nRF52840 Sense 心理咨询预警系统 - 手机端

## 文件说明

| 文件 | 职责 |
|---|---|
| `BLEManager.swift` | CoreBluetooth，扫描/连接/接收音频 |
| `ServerRelay.swift` | WebSocket，转发音频到服务器，接收预警/字幕/声纹结果 |
| `AppViewModel.swift` | 业务逻辑，连接 BLE、服务器和手机麦克风 |
| `ContentView.swift` | SwiftUI UI，状态/录音按钮/声纹按钮/预警列表 |

## 使用的 BLE UUIDs（Nordic UART Service）

```
Service:  6E400001-B5A3-F393-E0A9-E50E24DCCA9E
TX(设备→手机 notify): 6E400003-B5A3-F393-E0A9-E50E24DCCA9E
RX(手机→设备 write):  6E400002-B5A3-F393-E0A9-E50E24DCCA9E
```

Arduino 端需使用相同 UUID。

## 服务器

WebSocket 地址：`ws://150.158.146.192:8097`

在 `ServerRelay.swift` 第 11 行修改。

## Xcode 配置

`Info.plist` 需要添加：
- `NSBluetoothAlwaysUsageDescription` — 蓝牙权限
- `NSBluetoothPeripheralUsageDescription` — 后台蓝牙（可选）

## 数据流

```
XIAO BLE notify (PCM chunks)
    -> BLEManager.bleDidReceiveAudio()
    -> ServerRelay.sendAudioChunk()  (~100ms/1600字节缓冲后发送)
    -> WebSocket -> 服务器
    -> 服务器返回 JSON transcript / alert / voiceprint_result
    -> ServerRelay 解析 -> AppViewModel.alerts
    -> ContentView 展示预警
```

## 声纹认证

App 提供“录入声纹”和“身份确认”按钮，默认 `speaker_id` 为 `counselor_default`。录入/确认期间会复用当前音频来源：BLE 模式用 XIAO，手机麦克风调试模式用 iPhone 麦克风。

服务器返回示例：

```json
{"type":"voiceprint_result","stage":"verify","provider":"tencent","verified":true,"score":86.5}
```

## 服务器 Alert JSON 格式

```json
{
  "type": "alert",
  "level": "high",
  "keyword": "不想活了",
  "text": "转写原文片段"
}
```

# psy-guard

心理咨询对话危机干预系统

随身携带的硬件设备（XIAO nRF52840 Sense）持续采集咨询室音频，通过蓝牙实时传输至手机，手机中继到服务器进行语音识别和危机内容分析，检测到高风险内容时立即在咨询师手机上触发预警。

---

## 系统架构

```
[XIAO nRF52840 Sense]
  PDM 麦克风采集 PCM 音频
       │ BLE (244字节/包)
       ▼
[iPhone App (Swift)]
  BLE 接收 → WebSocket 中继
       │ ws://server:port
       ▼
[psy-guard 服务器 (Docker)]
  音频缓冲 (6秒窗口)
       │ HTTP multipart
       ▼
[FunASR] → 中文语音转文字
       │ text
       ▼
[LLM (Qwen/OpenAI)] → 危机内容分析
       │ JSON alert
       ▼
[iPhone App] → 预警展示 + 震动提醒
```

---

## 目录结构

```
psy-guard/
├── arduino/
│   └── xiao_audio_ble.ino      # XIAO nRF52840 Sense 固件
├── ios/
│   ├── BLEAudioManager.swift   # CoreBluetooth BLE 管理
│   ├── ServerRelay.swift       # WebSocket 服务器中继
│   └── ContentView.swift       # SwiftUI 主界面
├── server/
│   ├── server.py               # WebSocket 服务主程序
│   ├── Dockerfile
│   └── docker-compose.yml
└── README.md
```

---

## 硬件

**Seeed Studio XIAO nRF52840 Sense**

- Nordic nRF52840，ARM Cortex-M4 @ 64 MHz
- 板载 PDM 麦克风（MSM261D3526H1CPM）
- BLE 5.0
- 配合锂电池可随身使用

**Arduino 开发环境**

Board Package：`Seeed nRF52 mbed-enabled Boards`（非普通版）

依赖库：
- `ArduinoBLE`
- `PDM`（mbed 版内置）

**BLE 协议**

| UUID 后缀 | 方向 | 说明 |
|---|---|---|
| `...0001` | — | 服务 |
| `...0003` | 开发板 → 手机 | PCM 音频数据（notify，244字节/包） |
| `...0002` | 手机 → 开发板 | 控制指令（0x00=停止，0x01=开始） |

音频格式：PCM 16bit LE，单声道，16000 Hz

---

## iOS App

**依赖框架**：CoreBluetooth、SwiftUI、Combine、Foundation（URLSessionWebSocketTask）

**Info.plist 权限**

```xml
<key>NSBluetoothAlwaysUsageDescription</key>
<string>需要连接 XIAO 设备采集咨询音频</string>
```

**修改服务器地址**

`ContentView.swift`：
```swift
private let serverURL = "wss://your-server:port"
```

**预警 JSON 格式**（服务器下发）

```json
{
  "id": "uuid",
  "level": "high",
  "keyword": "触发词",
  "text": "原始转写片段",
  "suggestion": "给咨询师的干预建议",
  "timestamp": 1713000000.0
}
```

`level` 取值：`low`（关注）、`medium`（警示）、`high`（紧急）

---

## 服务器部署

### 前置条件

服务器需已运行：

| 服务 | 地址 | 说明 |
|---|---|---|
| FunASR HTTP Bridge | `localhost:8094` | 语音转文字 |
| LLM (OpenAI 兼容) | `localhost:8081` | 危机内容分析 |

FunASR 可使用官方 Docker 镜像：
```
registry.cn-hangzhou.aliyuncs.com/funasr_repo/funasr:funasr-runtime-sdk-online-cpu-0.1.12
```

### 启动

```bash
cd server
docker compose up -d
```

### 配置（docker-compose.yml environment）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `8097` | WebSocket 监听端口 |
| `FUNASR_URL` | `http://localhost:8094/transcribe` | FunASR 地址 |
| `LLM_BASE_URL` | `http://localhost:8081/v1` | LLM API 地址（OpenAI 兼容） |
| `LLM_MODEL` | `Qwen2.5-VL-3B-Instruct-Q4_K_M.gguf` | 模型名 |
| `LLM_API_KEY` | `none` | API Key（本地模型填 none） |
| `WINDOW_SEC` | `6` | 音频分析窗口（秒） |

使用外部 LLM（如 OpenAI）：
```yaml
environment:
  - LLM_BASE_URL=https://api.openai.com/v1
  - LLM_MODEL=gpt-4o-mini
  - LLM_API_KEY=sk-xxx
```

### 查看日志

```bash
docker logs -f psy-guard
```

---

## 数据流时序

```
iPhone          psy-guard       FunASR          LLM
  │──START──────────▶│
  │                  │
  │──[PCM chunk]────▶│
  │──[PCM chunk]────▶│  (缓冲累积)
  │──[PCM chunk]────▶│
  │                  │──POST /transcribe──▶│
  │                  │◀──{"text":"..."}────│
  │                  │──chat/completions──────────▶│
  │                  │◀──{"level":"high",...}───────│
  │◀──[Alert JSON]───│
  │  (震动+预警展示)
```

---

## 注意事项

- 本系统仅作为辅助工具，不替代专业人员判断
- 部署前确保符合当地隐私法规，咨询双方需知情同意
- 建议在生产环境使用 WSS（TLS）加密传输

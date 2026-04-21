# psy-guard

心理咨询对话危机干预系统

随身携带的硬件设备（XIAO nRF52840 Sense）持续采集咨询室音频，通过 BLE 实时传输至手机，手机中继到服务器进行语音识别和危机内容分析，检测到高风险内容时立即在咨询师手机上触发预警通知。

---

## 系统架构

```
[XIAO nRF52840 Sense]
  板载 PDM 麦克风，16kHz/16bit PCM 采集
       │ BLE 5.0 Nordic UART Service（244字节/包）
       ▼
[iPhone App（Swift / SwiftUI）]
  CoreBluetooth 接收 → 4KB 缓冲 → WebSocket 中继
  本地通知 + 实时字幕 + 预警确认
       │ ws://server:port
       ▼
[psy-guard 服务器（Docker）]
       │
       ├─ ASR_PROVIDER=xunfei → 讯飞流式 IAT（推荐，边说边出字，延迟 2-4s）
       ├─ ASR_PROVIDER=local  → FunASR WebSocket（本机，5秒窗口批量）
       └─ ASR_PROVIDER=api    → Whisper-compatible 云端 API（5秒窗口批量）
       │ 文字
       ▼
  LLM 语义分析（OpenAI-compatible，本地/云端均可）
  + SQLite 持久化
  + 高危 Webhook 推送
       │ JSON alert
       ▼
[iPhone App] → 系统通知（锁屏可见）+ 预警列表 + 标记处理
```

---

## 目录结构

```
psy-guard/
├── PsyGuard-Arduino/
│   └── PsyGuard/
│       └── PsyGuard.ino        # XIAO nRF52840 Sense 固件
├── PsyGuard-iOS/
│   ├── PsyGuard.xcodeproj/     # Xcode 工程
│   ├── BLEManager.swift        # CoreBluetooth BLE 管理
│   ├── ServerRelay.swift       # WebSocket 中继 + 预警解析
│   ├── AppViewModel.swift      # 业务逻辑（通知/会话/确认）
│   ├── ContentView.swift       # SwiftUI 主界面
│   └── PsyGuardApp.swift       # App 入口（通知权限申请）
├── server/
│   ├── server.py               # WebSocket 服务（双模式 ASR + LLM）
│   ├── Dockerfile
│   └── docker-compose.yml
├── architecture.html           # 系统架构图（浏览器打开）
└── README.md
```

---

## 硬件

**Seeed Studio XIAO nRF52840 Sense**

- Nordic nRF52840，ARM Cortex-M4 @ 64 MHz，BLE 5.0
- 板载 PDM 麦克风（MSM261D3526H1CPM），16kHz/16bit
- 配合锂电池可随身使用

**Arduino 开发环境**

开发板包：`Seeed nRF52 mbed-enabled Boards`（注意：必须是 mbed-enabled 版）

依赖库：`ArduinoBLE`（库管理器安装）、`PDM`（mbed 版内置）

**LED 状态**

| LED | 状态 |
|---|---|
| 蓝灯 | BLE 广播中，等待连接 |
| 绿灯 | 手机已连接 |
| 红灯 | 正在录制音频 |

**BLE 协议（Nordic UART Service）**

| UUID 后缀 | 方向 | 说明 |
|---|---|---|
| `...0001` | — | 服务 UUID |
| `...0003` | 开发板 → 手机 | PCM 音频数据（Notify，244字节/包） |
| `...0002` | 手机 → 开发板 | 控制指令（`0x01`=开始，`0x00`=停止） |

音频格式：PCM 16bit LE，单声道，16000 Hz

---

## iOS App

**Xcode 工程搭建**

1. 打开 `PsyGuard-iOS/PsyGuard.xcodeproj`
2. `Info.plist` 确认已有以下权限：
   - `NSBluetoothAlwaysUsageDescription`
   - `NSBluetoothPeripheralUsageDescription`
3. Signing & Capabilities → Background Modes → 勾选 `Uses Bluetooth LE accessories`
4. 修改服务器地址（`ServerRelay.swift` 第 35 行）：
   ```swift
   private let serverURL = URL(string: "ws://your-server:port")!
   ```
5. 真机运行（BLE 不支持模拟器）

**功能说明**

- 连接 XIAO 设备后点击麦克风按钮开始监听
- 实时字幕显示转写内容，状态栏显示会话计时
- 检测到危机内容时：高危触发系统通知 + 振动（锁屏可见），中危静默通知，低危仅 App 内显示
- 每条预警显示关键词、原文、干预建议，可标记"已处理"

**服务器推送的预警 JSON 格式**

```json
{
  "type": "alert",
  "id": "uuid",
  "level": "high",
  "keyword": "触发词",
  "text": "原始转写片段",
  "suggestion": "建议咨询师立即进行自杀风险评估",
  "timestamp": 1713000000.0
}
```

`level`：`high`（高危，红）/ `medium`（警示，橙）/ `low`（关注，黄）

---

## 服务器部署

### 前置条件（本地模式）

| 服务 | 地址 | 说明 |
|---|---|---|
| FunASR WebSocket | `localhost:10095` | 语音转文字（中文） |
| LLM（OpenAI-compatible） | `localhost:8086/v1` | 危机内容分析 |

FunASR Docker 启动参考：
```bash
docker run -d --name funasr \
  -p 10095:10095 \
  registry.cn-hangzhou.aliyuncs.com/funasr_repo/funasr:funasr-runtime-sdk-online-cpu-0.1.12
```

### 启动

```bash
cd server
docker compose up -d --build
```

### 配置（docker-compose.yml）

**ASR 模式切换**（核心配置，三选一）：

```yaml
# 讯飞流式模式（推荐）—— 边说边出字，端到端延迟 2-4s
ASR_PROVIDER=xunfei
XUNFEI_APPID=your_appid
XUNFEI_APISECRET=your_apisecret
XUNFEI_APIKEY=your_apikey

# 本地模式（默认）—— FunASR WebSocket，5秒窗口批量识别
ASR_PROVIDER=local
FUNASR_WS_URL=ws://localhost:10095

# 云端 API 模式 —— Whisper-compatible 端点，5秒窗口批量识别
# 兼容：OpenAI / 阿里云 / 本地 Whisper 服务
ASR_PROVIDER=api
ASR_API_URL=https://api.openai.com/v1
ASR_API_KEY=sk-xxx
ASR_MODEL=whisper-1
```

**LLM 配置**（两种模式通用，支持任意 OpenAI-compatible API）：

```yaml
# 本地 LLM（默认）
LLM_BASE_URL=http://localhost:8086/v1
LLM_MODEL=gemma-4-E4B-it-Q4_K_M.gguf
LLM_API_KEY=none

# 通义千问
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-turbo
LLM_API_KEY=sk-xxx

# 豆包
LLM_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
LLM_MODEL=ep-xxx
LLM_API_KEY=xxx
```

**其他配置**：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `8097` | WebSocket 监听端口 |
| `STREAM_LLM_CHARS` | `10` | 流式模式：积累多少字触发 LLM（xunfei 模式） |
| `WINDOW_SEC` | `5` | 批量模式：音频分析窗口（秒，local/api 模式） |
| `MIN_TEXT_LEN` | `4` | 过短文本跳过 LLM（字符数） |
| `CONTEXT_MAX_CHARS` | `300` | 滚动上下文历史长度 |
| `DB_PATH` | `/data/psy-guard.db` | SQLite 路径，留空禁用持久化 |
| `ADMIN_WEBHOOK_URL` | （空） | 高危预警推送 Webhook |

**管理员 Webhook 示例**：

```yaml
# Bark iOS 推送
ADMIN_WEBHOOK_URL=https://api.day.app/your-key

# 钉钉机器人
ADMIN_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=xxx

# 飞书机器人
ADMIN_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxx
```

### 查看日志

```bash
docker logs -f psy-guard
```

### 查看持久化数据

```bash
docker exec psy-guard sqlite3 /data/psy-guard.db \
  "SELECT datetime(timestamp,'unixepoch','localtime'), level, keyword, text FROM alerts ORDER BY timestamp DESC LIMIT 20;"
```

---

## 数据流时序

**讯飞流式模式（推荐）**

```
iPhone             psy-guard          讯飞 IAT         LLM
  │──START──────────▶│──[建立持久连接]──▶│
  │──[PCM chunk]────▶│──[40ms 音频]─────▶│
  │──[PCM chunk]────▶│◀──[文字片段]───────│
  │◀──transcript─────│  (实时字幕)        │──chat/completions──▶│
  │──[PCM chunk]────▶│◀──[文字片段]───────│◀──{"level":"high"}──│
  │◀──alert JSON─────│  (高危/中危预警)
  │  系统通知+震动
```

**批量模式（local/api）**

```
iPhone             psy-guard          ASR              LLM
  │──START──────────▶│
  │──[PCM chunk]────▶│
  │──[PCM chunk]────▶│  (缓冲 5 秒)
  │                  │──[local] FunASR WS──▶│
  │                  │   或                  │
  │                  │──[api] Whisper POST──▶│
  │                  │◀──{"text":"..."}──────│
  │                  │──chat/completions──────────────▶│
  │                  │◀──{"level":"high",...}───────────│
  │◀──transcript─────│  (实时字幕)
  │◀──alert JSON─────│  (高危/中危预警)
  │  系统通知+震动
```

---

## 实现状态

| 模块 | 功能 | 状态 |
|---|---|---|
| 硬件 | PDM 采集 + BLE 传输 | 完成 |
| iOS | BLE 连接 + 中继 + 预警展示 | 完成 |
| iOS | 系统本地通知（锁屏可见） | 完成 |
| iOS | 实时字幕 + 会话计时 + 预警确认 | 完成 |
| 服务器 | FunASR 本地模式 | 完成 |
| 服务器 | Whisper API 云端模式 | 完成 |
| 服务器 | SQLite 持久化 | 完成 |
| 服务器 | 管理员 Webhook 推送 | 完成 |
| 硬件 | 振动马达预警反馈 | 规划中 |
| 服务器 | REST API 查询历史 | 规划中 |
| 管理层 | Web 管理后台 | 待开发 |

---

## 注意事项

- 本系统仅作为辅助工具，不替代专业人员判断
- 部署前确保符合当地隐私法规，咨询双方需知情同意
- 建议在生产环境使用 WSS（TLS）加密传输
- 音频数据不落盘，仅内存处理；SQLite 只存转写文本和预警记录

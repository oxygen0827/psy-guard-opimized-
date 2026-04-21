# psy-guard

心理咨询室随身危机预警系统

咨询师佩戴 XIAO nRF52840 Sense，持续采集咨询室音频，通过 BLE 实时传至手机，手机中继到服务器进行流式语音识别和危机内容分析，检测到高风险内容时立即在咨询师手机上推送预警通知。

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
  ├─ ASR_PROVIDER=xunfei  → 讯飞流式 IAT（推荐，边说边出字）
  ├─ ASR_PROVIDER=local   → FunASR WebSocket（本机部署）
  └─ ASR_PROVIDER=api     → Whisper-compatible 云端 API
       │ 文字（流式实时）
       ▼
  LLM 语义分析（OpenAI-compatible，本地/云端均可）
  + SQLite 持久化
  + 高危 Webhook 推送
       │ JSON alert
       ▼
[iPhone App] → 系统通知（锁屏可见）+ 预警列表 + 标记处理
```

---

## 工作流程

```
1. 咨询师佩戴设备，打开 iOS App，点击麦克风按钮
2. App 通过 BLE 连接 XIAO，发送 0x01 指令开始录制
3. XIAO 持续采集 PCM，BLE 分包推给手机（244字节/包）
4. 手机缓冲 4KB 后通过 WebSocket 推给服务器
5. 服务器将音频实时转发给讯飞 IAT，边说边收到转写文字
6. 积累到 10 字或出现句尾标点，触发 LLM 语义分析
7. LLM 检测到危机信号 → 服务器推送 alert JSON 给手机
8. 手机触发系统通知（高危振动+声音，锁屏可见）
9. 咨询师查看预警详情，点击"已处理"确认
```

**端到端延迟**：说话 → 预警通知，约 2-4 秒（讯飞流式模式）

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
│   ├── server.py               # WebSocket 服务（三模式 ASR + LLM）
│   ├── run.ps1                 # Windows 本地启动脚本（PowerShell）
│   ├── Dockerfile
│   └── docker-compose.yml
├── test_client.py              # 电脑麦克风测试客户端
└── README.md
```

---

## 硬件

**Seeed Studio XIAO nRF52840 Sense**

- Nordic nRF52840，ARM Cortex-M4 @ 64 MHz，BLE 5.0
- 板载 PDM 麦克风（MSM261D3526H1CPM），16kHz/16bit
- 配合锂电池可随身使用

**Arduino 开发环境**

开发板包：`Seeed nRF52 mbed-enabled Boards`（必须是 mbed-enabled 版，否则 PDM.h 不可用）

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
2. `Info.plist` 确认已有权限：`NSBluetoothAlwaysUsageDescription`、`NSBluetoothPeripheralUsageDescription`
3. Signing & Capabilities → Background Modes → 勾选 `Uses Bluetooth LE accessories`
4. 修改服务器地址（`ServerRelay.swift` 第 35 行）：
   ```swift
   private let serverURL = URL(string: "ws://your-server:port")!
   ```
5. 真机运行（BLE 不支持模拟器）

**预警等级**

| 等级 | 触发条件 | 通知方式 |
|---|---|---|
| `high` 高危 | 明确自杀/自伤/伤人意图 | 系统通知 + 振动声音（锁屏可见） |
| `medium` 警示 | 强烈绝望感、咨询师疑似违规 | 静默通知 |
| `low` 关注 | 持续负面情绪、需观察 | 仅 App 内显示 |

**服务器推送的预警 JSON**

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

---

## 服务器部署

### 推荐方式：讯飞流式 ASR + 阿里百炼 LLM

编辑 `server/docker-compose.yml`，填入密钥：

```yaml
- ASR_PROVIDER=xunfei
- XUNFEI_APPID=your_appid
- XUNFEI_APISECRET=your_apisecret
- XUNFEI_APIKEY=your_apikey
- LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
- LLM_MODEL=qwen-flash
- LLM_API_KEY=sk-xxx
```

启动：

```bash
cd server
docker compose up -d --build
docker logs -f psy-guard
```

### 备用方式：本地 FunASR

```yaml
- ASR_PROVIDER=local
- FUNASR_WS_URL=ws://localhost:10095
```

FunASR Docker 启动：
```bash
docker run -d --name funasr -p 10095:10095 \
  registry.cn-hangzhou.aliyuncs.com/funasr_repo/funasr:funasr-runtime-sdk-online-cpu-0.1.12
```

### 关键配置参数

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `8097` | WebSocket 监听端口 |
| `STREAM_LLM_CHARS` | `10` | 积累多少字触发 LLM（流式模式） |
| `MIN_TEXT_LEN` | `4` | 过短文本跳过分析 |
| `CONTEXT_MAX_CHARS` | `300` | 滚动上下文历史长度 |
| `DB_PATH` | `/data/psy-guard.db` | SQLite 路径，留空禁用持久化 |
| `ADMIN_WEBHOOK_URL` | （空） | 高危预警管理员推送 |

**Webhook 示例**（高危时额外推送）：
```yaml
ADMIN_WEBHOOK_URL=https://api.day.app/your-bark-key   # Bark iOS
ADMIN_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=xxx  # 钉钉
```

---

## 数据流时序（讯飞流式模式）

```
iPhone             psy-guard          讯飞 IAT          LLM
  │──START──────────▶│
  │──[PCM chunk]────▶│──[40ms 音频]───▶│
  │──[PCM chunk]────▶│                  │──[中间结果]──▶│（积累中）
  │──[PCM chunk]────▶│◀──[文字片段]─────│
  │◀──transcript─────│                  │
  │──[PCM chunk]────▶│◀──[文字片段]─────│──chat/completions──▶│
  │                  │◀──{"level":"high",...}──────────────────│
  │◀──alert JSON─────│
  │  系统通知+震动
```

---

## 本地测试（无硬件）

用电脑麦克风模拟音频输入：

**Windows（PowerShell）**：
```powershell
# 启动服务器
cd server
.\run.ps1

# 另开窗口运行测试客户端
cd ..
python test_client.py          # 连本地
python test_client.py ws://150.158.146.192:6146  # 连 Spark2
```

依赖安装：`pip install sounddevice websockets numpy`

说出"我想死了"等关键词，观察是否触发预警。

---

## 实现状态

| 模块 | 功能 | 状态 |
|---|---|---|
| 硬件 | PDM 采集 + BLE 传输 | 完成 |
| iOS | BLE 连接 + 中继 + 预警展示 | 完成 |
| iOS | 系统本地通知（锁屏可见） | 完成 |
| iOS | 实时字幕 + 会话计时 + 预警确认 | 完成 |
| 服务器 | 讯飞流式 ASR（推荐） | 完成 |
| 服务器 | FunASR 本地模式 | 完成 |
| 服务器 | Whisper API 云端模式 | 完成 |
| 服务器 | SQLite 持久化 | 完成 |
| 服务器 | 管理员 Webhook 推送 | 完成 |
| 服务器 | 断线自动重连 | 完成 |
| 端到端 | 硬件联调验证 | 待完成 |
| iOS | BLE 后台保持连接 | 待验证 |

---

## 注意事项

- 本系统仅作为辅助工具，不替代专业人员判断
- 部署前确保符合当地隐私法规，咨询双方需知情同意
- 建议在生产环境使用 WSS（TLS）加密传输
- 音频数据不落盘，仅内存处理；SQLite 只存转写文本和预警记录

> **可在serve.py的第335行修改vad_eos的值来调整语音转文字的延迟，建议在500到2000之间调节，过低会影响效果，目前设置为1500.**

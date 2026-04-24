# psy-guard

心理咨询室随身危机预警系统

咨询师佩戴 XIAO nRF52840 Sense，持续采集咨询室音频，通过 BLE 实时传至手机，手机中继到服务器进行流式语音识别和危机内容分析，检测到高风险内容时立即在咨询师手机上推送预警通知。

---

## 系统架构

```
[XIAO nRF52840 Sense]
  板载 PDM 麦克风，16kHz/16bit PCM 采集（增益 30）
       │ BLE 5.0 Nordic UART Service（244字节/包）
       ▼
[iPhone App（Swift / SwiftUI）]
  CoreBluetooth 接收 → ~50ms 缓冲 → WebSocket 中继
  实时字幕（中间结果橙色显示）+ 预警通知 + 预警确认
  （调试）手机麦克风模式，绕过 XIAO 固件直接采集
       │ ws://server:8097
       ▼
[psy-guard 服务器（Docker）]
  ├─ ASR_PROVIDER=xunfei  → 讯飞流式 IAT（推荐）
  ├─ ASR_PROVIDER=local   → FunASR WebSocket（本机部署）
  └─ ASR_PROVIDER=api     → Whisper-compatible 云端 API
       │ 每句确认后立刻重连讯飞（消除句间死区）
       ▼
  LLM 语义分析（OpenAI-compatible）
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
2. App 先通知服务器 START，再通过 BLE 让 XIAO 开始录制
3. XIAO 持续采集 PCM，BLE 分包推给手机（244字节/包）
4. 手机缓冲约 50ms（1600字节）后通过 WebSocket 推给服务器
5. 服务器将音频实时推给讯飞 IAT
   - 识别过程中：推送中间结果（type=interim）→ iOS 橙色实时显示
   - 句子确认后（ls=True）：推送最终文本（type=transcript）→ 立刻重连讯飞
6. 每个完整句子触发 LLM 语义分析（MIN_TEXT_LEN=2）
7. LLM 检测到危机信号 → 服务器推送 alert JSON 给手机
8. 手机触发系统通知（高危振动+声音，锁屏可见）
9. 咨询师查看预警详情，点击"已处理"确认
```

**端到端延迟**：说话停顿 → 预警通知，约 1-3 秒（讯飞流式模式，句间间隔 ~1s）

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
│   ├── ServerRelay.swift       # WebSocket 中继 + 预警/字幕解析
│   ├── AppViewModel.swift      # 业务逻辑（通知/会话/手机麦克风模式）
│   ├── ContentView.swift       # SwiftUI 主界面
│   ├── MicCapture.swift        # 手机麦克风采集（调试用）
│   └── PsyGuardApp.swift       # App 入口（通知权限申请）
├── server/
│   ├── server.py               # WebSocket 服务（三模式 ASR + LLM）
│   ├── Dockerfile
│   └── docker-compose.yml
├── web/
│   ├── client.html             # 网页版客户端（测试 ASR + 预警）
│   └── admin.html              # 管理后台（会话监控 + 录音下载）
├── test_file.py                # 用音频文件测试服务器（绕过麦克风）
└── README.md
```

---

## 硬件

**Seeed Studio XIAO nRF52840 Sense**

- Nordic nRF52840，ARM Cortex-M4 @ 64 MHz，BLE 5.0
- 板载 PDM 麦克风（MSM261D3526H1CPM），16kHz/16bit，增益 30
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
2. `Info.plist` 已包含权限：`NSBluetoothAlwaysUsageDescription`、`NSBluetoothPeripheralUsageDescription`、`NSMicrophoneUsageDescription`
3. Signing & Capabilities → Background Modes → 勾选 `Uses Bluetooth LE accessories`
4. 修改服务器地址（`ServerRelay.swift` 第 46 行）：
   ```swift
   private let serverURL = URL(string: "ws://your-server:port")!
   ```
5. 真机运行（BLE 不支持模拟器）

**调试模式：手机麦克风**

录音按钮上方有"手机麦克风（调试）"开关，打开后：
- 不需要 XIAO 设备连接
- 直接用 iPhone 麦克风采集音频
- 格式与 XIAO 完全一致（16kHz 16bit PCM mono）
- 可验证服务器端识别是否正常，独立于固件问题

**实时字幕**

- 橙色：讯飞中间识别结果（实时滚动）
- 黑色：最终确认句子（ls=True）

**预警等级**

| 等级 | 触发条件 | 通知方式 |
|---|---|---|
| `high` 高危 | 明确自杀/自伤/伤人意图 | 系统通知 + 振动声音（锁屏可见） |
| `medium` 警示 | 强烈绝望感、咨询师疑似违规 | 静默通知 |
| `low` 关注 | 持续负面情绪、需观察 | 仅 App 内显示 |

**服务器推送的消息格式**

```json
// 中间字幕（实时更新，不触发LLM）
{"type": "interim", "text": "识别中的文字..."}

// 最终字幕
{"type": "transcript", "text": "完整句子"}

// 预警
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

> **注意**：当前服务器上的 `server.py` 通过 `docker cp` 注入，不在镜像内。
> 每次重启容器后需重新注入：
> ```bash
> docker cp ~/psy-guard/server.py psy-guard:/app/server.py && docker restart psy-guard
> ```

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
| `MIN_TEXT_LEN` | `2` | 过短文本跳过分析（≥2 保留"想死""救命"等短句） |
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
  │                  │                  │──[ls=False]→ interim
  │◀──interim────────│◀─────────────────│  （橙色实时显示）
  │──[PCM chunk]────▶│                  │──[ls=True 完整句]──────▶│
  │◀──transcript─────│◀─────────────────│◀──{"level":"high",...}──│
  │◀──alert JSON─────│                  │
  │  系统通知+震动    │ 立即重连讯飞 ──▶│（新 session，消除死区）
```

---

## Web 调试界面

`web/` 目录下有两个无需安装的 HTML 调试工具，**本地双击打开即可使用**：

| 文件 | 用途 |
|---|---|
| `client.html` | 网页版客户端：发送音频/文本，查看实时字幕和预警 |
| `admin.html` | 管理后台：实时监控所有会话，下载录音 |

**使用说明**：
- `ws://` 是 WebSocket 协议，无法在浏览器地址栏直接打开
- 在本地双击打开 HTML 文件，页面内的地址框已预填好服务器地址，点"连接"即可
- 分享给他人：直接发送 HTML 文件，对方本地打开，不需要服务器托管

**端口状态**：
- `:8097` — 公网可访问，同时承担 WebSocket 和录音下载（`GET /recording/{id}`）两个功能，无需额外端口
- `:8098` — 备用 HTTP 端口，防火墙默认拦截，可忽略

---

## 本地测试（无硬件）

用录音文件直接推给服务器：

```bash
python3 test_file.py 录音.m4a ws://150.158.146.192:8097
```

> **重要**：Mac 上 `afconvert` 重采样必须用 `-d 'LEI16@16000'` 格式，不能用 `-r 16000`（后者只改标头，不实际重采样）。

---

## 实现状态

| 模块 | 功能 | 状态 |
|---|---|---|
| 硬件 | PDM 采集（增益30）+ BLE 传输 | 代码完成，**需重新烧录** |
| iOS | BLE 连接 + 中继 + 预警展示 | 代码完成，**需重新安装** |
| iOS | 手机麦克风调试模式 | 代码完成，**需重新安装** |
| iOS | 实时中间字幕（橙色） | 代码完成，**需重新安装** |
| iOS | 系统本地通知（锁屏可见） | 完成 |
| iOS | 录音顺序/断线恢复/重连控制 | 完成 |
| 服务器 | 讯飞流式 ASR（pgs/sn/ls 正确） | **已部署** |
| 服务器 | 实时中间字幕推送（interim） | **已部署** |
| 服务器 | 句子完成后主动重连（消除死区） | **已部署** |
| 服务器 | flush_pending 防丢句子 | **已部署** |
| 服务器 | vad_eos=500ms | **已部署** |
| 服务器 | 管理员实时监控（broadcast_admin） | **已部署（修复 UnboundLocalError）** |
| 服务器 | 录音下载（8097 端口，无需开放 8098） | **已部署** |
| 服务器 | ASR 延时累积修复（MAX_BUF_BYTES=160ms） | **已部署** |
| 服务器 | on_text await process_text 防止最后几句漏报预警 | **已部署** |
| 服务器 | _process_request legacy API 兼容修复（原版致所有连接 500） | **已部署** |
| 网页 | client.html downsample 线性插值（改善 ASR 识别率） | **已更新** |
| iOS | MicCapture AVAudioConverter nil 检查 | 代码完成，**需重新安装** |
| iOS | BLE 掉线不打断手机麦克风录音 | 代码完成，**需重新安装** |
| 服务器 | FunASR 本地模式 | 完成 |
| 服务器 | Whisper API 云端模式 | 完成 |
| 服务器 | SQLite 持久化 | 完成 |
| 服务器 | 管理员 Webhook 推送 | 完成 |
| 端到端 | iOS→服务器连接验证 | ✅ 已验证 |
| 端到端 | 语音识别+LLM预警验证 | ✅ 已验证（手机麦克风） |
| 端到端 | 管理员监控+录音下载验证 | ✅ 已验证（web admin） |
| 端到端 | XIAO固件+完整链路 | 待重烧固件后验证 |
| iOS | BLE 后台保持连接 | 待验证 |

---

## 注意事项

- 本系统仅作为辅助工具，不替代专业人员判断
- 部署前确保符合当地隐私法规，咨询双方需知情同意
- 建议在生产环境使用 WSS（TLS）加密传输
- 音频数据不落盘，仅内存处理；SQLite 只存转写文本和预警记录

# PsyGuard 项目当前状态（2026-04-24，最后更新）

> 给下一个 Claude 实例快速上手用。本文件优先于 CLAUDE.md 中的旧信息。

---

## 项目是什么

心理咨询室随身预警系统。咨询师佩戴 XIAO nRF52840 Sense 录音，BLE 传到 iPhone，iPhone 通过 WebSocket 转发到云端服务器，服务器做语音识别 + LLM 分析，检测到危机内容（自伤、轻生等）立即推预警到手机。

---

## 三端架构

```
[XIAO nRF52840 Sense]
  PDM麦克风 16kHz PCM → BLE Nordic UART Service (244字节/包)
         ↓ BLE
[iPhone App (Swift)]
  CoreBluetooth 接收 → 1600字节缓冲(~50ms) → WebSocket 推服务器
  接收预警 JSON → SwiftUI 展示 + 系统通知
  （调试模式）手机麦克风直接采集，绕过 XIAO 固件
         ↓ WebSocket
[云端服务器 150.158.146.192]
  Docker 容器 psy-guard → 讯飞流式 ASR (pgs/sn/ls 正确处理) → 阿里百炼 qwen-flash → 推回预警
```

---

## 云端服务器信息

| 项目 | 值 |
|---|---|
| IP | 150.158.146.192 |
| 用户名 | ubuntu |
| 密码 | @Nchu1234 |
| SSH | `ssh ubuntu@150.158.146.192`（标准 22 端口）|
| 服务器型号 | Ubuntu 24.04 LTS，4核，3.5GB RAM，59GB磁盘 |
| Docker | 27.5.1，Docker Compose v2.32.4 |

**服务器上还跑着别人的项目**（capyai-kb 知识库系统），不要影响它。

**frps 已在运行**：监听 7000/7500（管理），预留端口 6000-6205（供 frp 客户端接入）。

---

## 当前部署状态

### 已完成 ✅

- **Arduino 固件**（代码已改，尚未重烧）：
  - PDM 采集 + BLE 流式传输，BLE 连接 iOS 正常
  - `PDM.setGain(30)` 已写入代码 **→ 需重烧才生效（这是实际设备识别差的根因）**

- **iOS App**（代码已改，尚未重装）：
  - BLE 管理、WebSocket 中继、SwiftUI 预警界面，Xcode 工程完整
  - `MicCapture.swift`：手机麦克风调试模式（绕过 XIAO 固件）
  - 实时中间字幕（橙色）：讯飞每次识别中间结果即时显示，不等 `ls=True`
  - 缓冲 1600 字节（~50ms），`flushAndStop()` 停录保证完整
  - BLE 断线自动重置录音状态
  - UI：录音按钮上方有"手机麦克风（调试）"开关，打开后无需 XIAO 即可测试

- **云端 Docker 部署（已热更新，无需重启）**：
  - 镜像运行中，监听 `0.0.0.0:8097`
  - ASR：讯飞持久流式 IAT（`XunfeiStreamSession`）
  - LLM：阿里百炼 qwen-flash，`MIN_TEXT_LEN=2`

- **ASR 修复（全部已部署）**：
  1. pgs/sn/ls 修复：仅在 `ls=True` 输出完整句子
  2. 孤立句子修复：`ls=True` 时先 flush 所有更早的 sn
  3. 静音帧改为全零 PCM
  4. `on_text` 每句直接触发 LLM
  5. START 命令防旧 session 泄漏
  6. **实时中间字幕**：`ls=False` 时推 `{"type":"interim","text":"..."}` 给 iOS
  7. **`sentence_buf` 改为实例变量**，`stop()` 和重连前均 `_flush_pending()`，防丢句子
  8. **`vad_eos` 从 1000ms 降到 500ms**（句子边界检测更快）
  9. **主动重连**：每句 `ls=True` 后立即重连讯飞（`_needs_reconnect` 标志），消除原来 ~10s 死区
  10. 错误重连延迟从 1s 降到 0.3s
  11. **延时累积修复**：新 session 建立时若 `_buf` 超 160ms 则丢弃最旧积压，防止重连开销导致延时线性增长（根因：每次重连 ~1s，期间 ~32KB 音频堆积，30 句后延时达 15-20s）

- **端到端验证（已确认）**：
  - iOS → 服务器 WebSocket 连接正常 ✅
  - 讯飞识别出真实语音内容 ✅（日志可见转写文字）
  - LLM 分析正常，预警触发 ✅（"崩溃" → medium 预警）
  - 原 ~11s 识别死区已修复，句间间隔降至 ~1s ✅

### 待完成 ⬜

- [ ] **重新烧录 Arduino 固件**（`PDM.setGain(30)`）—— 这是 XIAO 麦克风音量太低的根因
- [ ] **重新安装 iOS App**（含手机麦克风调试模式 + 实时中间字幕）
- [ ] 长时间录制稳定性测试（BLE 掉包/重连）

---

## 部署注意事项

**当前服务器上的 server.py 是通过 `docker cp` 注入的，不在镜像内。**
每次 `docker compose down && docker compose up -d` 后必须重新 cp：

```bash
cd ~/psy-guard
docker compose down && docker compose up -d
docker cp ~/psy-guard/server.py psy-guard:/app/server.py
docker restart psy-guard
```

**SSH 建议用 paramiko**（Mac 无 sshpass）：
```python
import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('150.158.146.192', username='ubuntu', password='@Nchu1234')
```

---

## 关键文件改动汇总

| 文件 | 改动 |
|---|---|
| `server/server.py` | pgs/sn/ls修复、孤立句修复、静音帧、MIN_TEXT_LEN=2、START防泄漏、interim推送、sentence_buf实例化、_flush_pending、vad_eos=500、_needs_reconnect主动重连、错误重连0.3s；**broadcast_admin UnboundLocalError修复**（`-=` → `.difference_update()`）；**_process_request：GET /recording/{sid} 在8097端口直接下载**；**MAX_BUF_BYTES=160ms 防延时累积** |
| `server/docker-compose.yml` | MIN_TEXT_LEN=4 → 2 |
| `server/Dockerfile` | pip 改用清华镜像 |
| `PsyGuard-iOS/ServerRelay.swift` | bufferThreshold 4096→1600，flushAndStop()，stopped标志防重连，relayDidReceiveInterim，parseAlert处理interim类型 |
| `PsyGuard-iOS/AppViewModel.swift` | toggleRecording顺序修正，BLE断线重置，通知音default，usePhoneMic开关，MicCapture集成，currentSentence，relayDidReceiveInterim |
| `PsyGuard-iOS/ContentView.swift` | 录音按钮双重检查，手机麦克风调试Toggle，transcriptBox分层显示（黑=已确认/橙=识别中） |
| `PsyGuard-iOS/MicCapture.swift` | 新增：AVAudioEngine采集16kHz 16bit PCM mono，AVAudioConverter重采样 |
| `PsyGuard-iOS/Info.plist` | 新增 NSMicrophoneUsageDescription |
| `PsyGuard-iOS/PsyGuard.xcodeproj/project.pbxproj` | 新增 MicCapture.swift 编译引用 |
| `PsyGuard-Arduino/PsyGuard.ino` | PDM.setGain(30)，PDM buffer overflow 保护 |
| `test_file.py` | 用音频文件测试服务器（afconvert需用 `-d 'LEI16@16000'`） |

---

## 服务器 WebSocket 消息协议

### 客户端 → 服务器
| 消息 | 类型 | 说明 |
|---|---|---|
| `"START"` | string | 开始录制，服务器创建讯飞 session |
| `"STOP"` | string | 停止录制，服务器 flush 并关闭 session |
| 二进制数据 | bytes | 16kHz 16bit PCM mono 音频块 |

### 服务器 → 客户端
| 消息 | 说明 |
|---|---|
| `"ACK:START"` | START 已确认 |
| `"ACK:STOP"` | STOP 已确认 |
| `{"type":"interim","text":"..."}` | 讯飞中间识别结果（实时，不触发LLM） |
| `{"type":"transcript","text":"..."}` | 讯飞最终确认句子（触发LLM） |
| `{"type":"alert","level":"high/medium/low","keyword":"...","text":"...","suggestion":"...","timestamp":...}` | LLM 预警 |

---

## 配置速查

### docker-compose.yml 关键环境变量

```yaml
ASR_PROVIDER: xunfei
PORT: 8097
MIN_TEXT_LEN: 2
XUNFEI_APPID: baf02ba0
XUNFEI_APISECRET: YjUwNzk0N2U5MWZlNzIzMGNmMjBlYTA2
XUNFEI_APIKEY: 2da75cd52e84fc0c09471cb5660532d7
LLM_BASE_URL: https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL: qwen-flash
LLM_API_KEY: sk-216e0b0a6b3d4551a4983761b7fb4e1f
```

### BLE UUIDs（Nordic UART Service，三端一致）

```
Service:  6E400001-B5A3-F393-E0A9-E50E24DCCA9E
TX（设备→手机）: 6E400003-B5A3-F393-E0A9-E50E24DCCA9E
RX（手机→设备）: 6E400002-B5A3-F393-E0A9-E50E24DCCA9E
```

BLE 设备广播名：`PsyGuard`（iOS 过滤条件：name 含 XIAO/Sense/Psy/Arduino）

### 音频格式

- 单声道，16kHz，16-bit PCM，小端序
- BLE 分包 244 字节/包
- 手机缓冲 ~50ms（1600字节）后发服务器

---

## Web 调试界面

`web/` 目录下有两个本地 HTML 文件：

| 文件 | 用途 | 连接地址 |
|---|---|---|
| `client.html` | 网页版客户端，测试 ASR + 预警 | `ws://150.158.146.192:8097` |
| `admin.html` | 管理后台，查看会话/录音下载 | WS: `ws://150.158.146.192:8097/admin`，下载: `http://150.158.146.192:8097` |

**重要**：
- `ws://` 不能在浏览器地址栏直接打开，必须**本地双击打开 HTML 文件**，页面内的输入框填好地址后点连接
- 端口 **8097 已同时处理 WebSocket 和录音下载**（`GET /recording/{sid}` 通过 `process_request` 钩子在同一端口服务），无需开放 8098
- 要分享给别人使用：直接发 HTML 文件，对方本地打开即可，**不要发 ws:// URL**

---

## 验证步骤

```bash
# 1. 测试连通性
npx wscat -c ws://150.158.146.192:8097
# 发送: START → 预期: ACK:START

# 2. 用录音文件端到端测试（推荐）
python3 test_file.py 录音.m4a ws://150.158.146.192:8097

# 3. 查服务器日志
python3 -c "
import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('150.158.146.192', username='ubuntu', password='@Nchu1234')
_, o, _ = ssh.exec_command('docker logs --tail 50 psy-guard 2>&1')
print(o.read().decode())
"
```

---

## iOS App 结构速查

| 文件 | 职责 |
|---|---|
| `BLEManager.swift` | CoreBluetooth 扫描/连接/接收音频数据 |
| `ServerRelay.swift` | WebSocket 连接服务器，缓冲发送，解析预警/字幕/中间结果 |
| `AppViewModel.swift` | 业务逻辑，连接 BLE 和 Server 两层，发系统通知，手机麦克风模式 |
| `ContentView.swift` | SwiftUI：状态栏、录音按钮、麦克风调试开关、实时字幕、预警列表 |
| `MicCapture.swift` | AVAudioEngine 采集手机麦克风，输出与 XIAO 相同的 16kHz PCM 格式 |
| `PsyGuardApp.swift` | App 入口，申请通知权限 |

服务器 URL 在 `ServerRelay.swift` 第 46 行修改。

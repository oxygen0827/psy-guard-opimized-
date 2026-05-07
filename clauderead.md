# PsyGuard 项目当前状态（2026-05-07，第六次更新）

> 给下一个 Claude 实例快速上手用。本文件优先于 CLAUDE.md 中的旧信息。

---

## 项目是什么

心理咨询室随身预警系统。咨询师佩戴 XIAO nRF52840 Sense 录音，BLE 传到 iPhone，iPhone 通过 WebSocket 转发到云端服务器，服务器做语音识别 + LLM 分析，检测到危机内容（自伤、轻生等）立即推预警到手机。

---

## 三端架构

```
[XIAO nRF52840 Sense]
  PDM麦克风 16kHz 采集 → 软件降采样到 8kHz → BLE Nordic UART Service (244字节/包)
         ↓ BLE
[iPhone App (Swift)]
  CoreBluetooth 接收 → 1600字节缓冲(~100ms) → WebSocket 推服务器
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
| 密码 | @Nchu152535 |
| SSH | `ssh ubuntu@150.158.146.192`（标准 22 端口）|
| 服务器型号 | Ubuntu 24.04 LTS，4核，3.5GB RAM，59GB磁盘 |
| Docker | 27.5.1，Docker Compose v2.32.4 |

**服务器上还跑着别人的项目**（capyai-kb 知识库系统），不要影响它。

**frps 已在运行**：监听 7000/7500（管理），预留端口 6000-6205（供 frp 客户端接入）。

---

## 当前部署状态

### 已完成 ✅

- **XIAO 硬件音频输入已完全解决**：
  - PDM 硬件本身正常（PDMTest 无 BLE 时 RMS=137，干净）
  - BLE 射频干扰问题已通过降低增益+软件降采样解决
  - 当前固件：gain=35，PDM 16kHz 采集 → 3点均值滤波 → 取偶数样本降到 8kHz → BLE 传输
  - BLE 请求 15ms 连接间隔（`BLE.setConnectionInterval(12, 12)`）
  - 音频质量已通过 `ble_record.py` 验证 OK

- **iOS App（手机麦克风模式）**：
  - 手机麦克风 → 服务器 → 讯飞识别 → LLM 预警 **完整链路已验证**
  - 代码含所有修复（sendControl 写类型检测、BLE 诊断回调、BLE 断线不停麦克风录音）
  - **尚未重装**（旧版 App 在手机上，新代码已在 Xcode 工程里）

- **云端 Docker 部署（已热更新，最新 server.py 已注入）**：
  - 监听 `0.0.0.0:8097`
  - ASR：讯飞持久流式 IAT（`XunfeiStreamSession`）
  - LLM：阿里百炼 qwen-flash，`MIN_TEXT_LEN=2`
  - **已更新为 8kHz**（`SAMPLE_RATE=8000`，讯飞 format 字符串 `rate=8000`，已重启容器）

- **所有 server.py Bug 修复（全部已部署）**：
  1. pgs/sn/ls 修复 + 孤立句子修复
  2. 静音帧改为全零 PCM
  3. START 命令防旧 session 泄漏
  4. 实时中间字幕（`ls=False` 时推 `interim`）
  5. `sentence_buf` 实例变量 + `_flush_pending`
  6. `vad_eos=500ms`（更快的句子边界）
  7. 主动重连（`_needs_reconnect`，消除 ~10s 死区）
  8. 错误重连 0.3s
  9. `MAX_BUF_BYTES=160ms`（防延时累积）
  10. `on_text` 改为 `await process_text()`（防断线漏报）
  11. `broadcast_admin` UnboundLocalError（Python 3.11 问题，显式 `global` 声明修复）
  12. `_process_request` legacy API 兼容（签名改为 `(path, headers)`，返回三元组）
  13. `_recv_loop` on_text 任务追踪（`_text_tasks` set + `stop()` 等待完成，防断线漏最后几句）

- **语料库集成（2026-05-07）**：
  - 新增 `server/corpus.json`（150条，4类：政治/严重危害行为/违背伦理/技术不当）
  - `server.py` 启动时自动加载语料库，动态构建 System Prompt
  - 语料库每类取6条典型句式注入 LLM 上下文，提升专业违规检测精度
  - 级别映射：一级(政治)→high；二级/危害行为→high；二级/违背伦理→medium；三级/技术不当→low
  - 同时保留原有来访者危机信号检测逻辑

- **Web 客户端 VAD 修复（2026-05-07）**：
  - `web/client.html` 加入客户端声音活动检测（VAD_THRESHOLD=0.015）
  - 低于阈值时发零帧，避免环境底噪被识别为"嗯嗯嗯"
  - `getUserMedia` 强制 `autoGainControl: false`（关键！AGC 会把底噪放大到阈值以上）
  - 同时关闭 `noiseSuppression`、`echoCancellation`，保持原始信号供讯飞 ASR 处理
  - 加入实时音量条 + 橙色阈值标记线（页面加载即初始化位置）

### 待完成 ⬜

- [x] ~~**更新服务器 SAMPLE_RATE 为 8000**~~ ✅ 已完成（server.py + 本地副本同步）
- [x] ~~**Web 端"嗯嗯嗯"误识别修复**~~ ✅ 已完成（客户端 VAD + 禁用 AGC）
- [ ] **重新安装 iOS App**（含 sendControl 自动检测 + BLE 诊断回调 + 掉线不停麦克风）
- [ ] 端到端 XIAO 完整联调（服务器更新 + iOS App 重装后）
- [ ] 长时间录制稳定性测试

---

## 固件关键参数（当前烧录版本）

| 参数 | 值 | 说明 |
|---|---|---|
| PDM 采样率 | 16000 Hz | 硬件固定，软件降采样到 8kHz |
| PDM 增益 | 35 | gain=20 信号太弱(RMS~100)，35 约 RMS~180-950 |
| 降采样方式 | 3点均值滤波 + 取偶数样本 | 消除混叠，保证音色正常 |
| BLE 输出率 | 8000 Hz equivalent | 传给 iOS/Mac 的有效采样率 |
| BLE 连接间隔 | 请求 15ms（12×1.25ms） | 提升吞吐，减少数据丢失 |
| sampleBuffer | 1024 样本 | 64ms/包，降低 BLE 射频干扰频率 |

---

## BLE 直连录音测试工具（Mac 端验证）

- `ble_record.py`：Mac 通过 bleak 直连 XIAO，接收 BLE 音频，保存 WAV 到桌面
- **安装 bleak**（必须用 python3.11，Xcode 自带 python3.9 不兼容）：
  ```bash
  /Users/hushaohong/.local/bin/python3.11 -m pip install bleak --break-system-packages
  ```
- **启动**（按 Ctrl+C 一次停止，自动保存）：
  ```bash
  /Users/hushaohong/.local/bin/python3.11 /Users/hushaohong/vibe-coding/psy-guard/ble_record.py
  ```
- **录音位置**：`~/Desktop/xiao_recordings/`

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
ssh.connect('150.158.146.192', username='ubuntu', password='@Nchu152535')
```

---

## 关键文件改动汇总

| 文件 | 改动 |
|---|---|
| `server/server.py` | pgs/sn/ls修复、孤立句修复、静音帧、MIN_TEXT_LEN=2、START防泄漏、interim推送、sentence_buf实例化、_flush_pending、vad_eos=500、_needs_reconnect主动重连、错误重连0.3s；broadcast_admin global声明修复；_process_request legacy API兼容；MAX_BUF_BYTES=160ms；on_text改await防漏报；_text_tasks任务追踪防断线漏最后几句 |
| `server/docker-compose.yml` | MIN_TEXT_LEN=4 → 2 |
| `server/Dockerfile` | pip 改用清华镜像 |
| `PsyGuard-iOS/ServerRelay.swift` | bufferThreshold 4096→1600，flushAndStop()，stopped标志防重连，relayDidReceiveInterim，parseAlert处理interim |
| `PsyGuard-iOS/AppViewModel.swift` | toggleRecording顺序，BLE断线重置，usePhoneMic开关，MicCapture集成，currentSentence；bleStateChanged(.idle)判断usePhoneMic防止误停麦克风 |
| `PsyGuard-iOS/ContentView.swift` | 麦克风调试Toggle，transcriptBox分层（黑=确认/橙=识别中） |
| `PsyGuard-iOS/MicCapture.swift` | AVAudioEngine 8kHz PCM mono（targetFormat sampleRate=8000）；startEngine增加converter nil检查 |
| `PsyGuard-iOS/BLEManager.swift` | sendControl写类型自动检测；新增 didUpdateNotificationStateFor / didWriteValueFor 诊断回调 |
| `web/client.html` | downsample改线性插值（原最近邻，影响识别率）；客户端VAD（VAD_THRESHOLD=0.015）；getUserMedia禁用autoGainControl/noiseSuppression/echoCancellation；实时音量条+阈值橙线 |
| `server/corpus.json` | 新建：AI心理督导语料库，150条，4类，来源Excel |
| `server/server.py` | （续）_load_corpus()/_build_system_prompt()动态构建LLM提示词；语料库6条/类注入；System Prompt扩展咨询师违规检测；移除多余 import json as _json |
| `PsyGuard-iOS/Info.plist` | 新增 NSMicrophoneUsageDescription |
| `PsyGuard-iOS/PsyGuard.xcodeproj/project.pbxproj` | 新增 MicCapture.swift 编译引用 |
| `PsyGuard-Arduino/PsyGuard/PsyGuard.ino` | BLE.poll()、BLEWrite+BLEWriteWithoutResponse、**gain=35**、**16kHz→8kHz软件降采样+3点均值滤波**、**BLE.setConnectionInterval(12,12)** |
| `PsyGuard-Arduino/PDMTest/PDMTest.ino` | 新建：PDM 独立测试草图，不启动 BLE，录音经串口传电脑 |
| `test_pdm.py` | 新建：串口录音分析脚本，保存WAV + 振幅分析 + 播放 |
| `ble_record.py` | 新建：BLE直连Mac录音工具，自动计算有效采样率，按Ctrl+C停止保存 |
| `test_file.py` | 用音频文件测试服务器（afconvert需用 `-d 'LEI16@16000'`） |

---

## 服务器 WebSocket 消息协议

### 客户端 → 服务器
| 消息 | 类型 | 说明 |
|---|---|---|
| `"START"` | string | 开始录制，服务器创建讯飞 session |
| `"STOP"` | string | 停止录制，服务器 flush 并关闭 session |
| 二进制数据 | bytes | 8kHz 16bit PCM mono 音频块（XIAO路径）|

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

- 单声道，**8kHz**，16-bit PCM，小端序（XIAO 固件软件降采样后）
- BLE 分包 244 字节/包
- 手机缓冲 ~100ms（1600字节）后发服务器

---

## Web 调试界面

`web/` 目录下有两个本地 HTML 文件：

| 文件 | 用途 | 连接地址 |
|---|---|---|
| `client.html` | 网页版客户端，测试 ASR + 预警 | `ws://150.158.146.192:8097` |
| `admin.html` | 管理后台，查看会话/录音下载 | WS: `ws://150.158.146.192:8097/admin` |

**重要**：必须**本地双击打开 HTML 文件**使用，不能在浏览器地址栏输入 ws:// 地址。

---

## 验证步骤

```bash
# 1. 测试服务器连通性
npx wscat -c ws://150.158.146.192:8097
# 发送: START → 预期: ACK:START

# 2. 用录音文件端到端测试（推荐）
python3 test_file.py 录音.m4a ws://150.158.146.192:8097

# 3. BLE 直连录音测试（验证 XIAO 音频质量）
/Users/hushaohong/.local/bin/python3.11 ble_record.py
# 按 Ctrl+C 停止，录音保存到 ~/Desktop/xiao_recordings/

# 4. PDM 独立测试（排查硬件噪声，不启动 BLE）
# 先烧录 PsyGuard-Arduino/PDMTest/PDMTest.ino
python3 test_pdm.py

# 5. 查服务器日志
python3 -c "
import paramiko
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('150.158.146.192', username='ubuntu', password='@Nchu152535')
_, o, _ = ssh.exec_command('docker logs --tail 50 psy-guard 2>&1')
print(o.read().decode())
"
```

---

## iOS App 结构速查

| 文件 | 职责 |
|---|---|
| `BLEManager.swift` | CoreBluetooth 扫描/连接/接收音频数据，写类型自动检测 |
| `ServerRelay.swift` | WebSocket 连接服务器，缓冲发送，解析预警/字幕/中间结果 |
| `AppViewModel.swift` | 业务逻辑，连接 BLE 和 Server 两层，发系统通知，手机麦克风模式 |
| `ContentView.swift` | SwiftUI：状态栏、录音按钮、麦克风调试开关、实时字幕、预警列表 |
| `MicCapture.swift` | AVAudioEngine 采集手机麦克风，输出 8kHz 16bit PCM mono，格式与 XIAO 一致 |
| `PsyGuardApp.swift` | App 入口，申请通知权限 |

服务器 URL 在 `ServerRelay.swift` 第 47 行修改。

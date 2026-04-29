/*
  PsyGuard - XIAO nRF52840 Sense
  PDM 麦克风采集 + BLE 流式传输到手机

  基于官方 PDM 示例修改，PDM 部分基本不动，加了 BLE Nordic UART Service
  手机端用 CoreBluetooth 接收，转发到服务器做语音识别和预警

  依赖库：
  - PDM（Seeed nRF52 mbed-enabled 板包内置）
  - ArduinoBLE（Seeed nRF52 mbed-enabled 板包内置）
*/

#include <PDM.h>
#include <ArduinoBLE.h>

// ── BLE Nordic UART Service UUIDs（和 Swift 端一致）─────────────────────────
#define NUS_SERVICE_UUID  "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
#define NUS_TX_UUID       "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"  // 设备→手机
#define NUS_RX_UUID       "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  // 手机→设备

BLEService         nusService(NUS_SERVICE_UUID);
BLECharacteristic  txChar(NUS_TX_UUID, BLERead | BLENotify, 244);   // 音频数据
BLECharacteristic  rxChar(NUS_RX_UUID, BLERead | BLEWrite | BLEWriteWithoutResponse, 1); // 控制命令

// ── PDM ──────────────────────────────────────────────────────────────────────
// PDM 硬件固定 16kHz，软件每隔一样本取一个降到 8kHz 后发 BLE
// 8kHz 数据量 16 KB/s，BLE 吞吐上限约 10-20 KB/s，可完整传输
short sampleBuffer[1024];
volatile int samplesRead;

// ── 应用状态 ─────────────────────────────────────────────────────────────────
bool isRecording = false;

// ── PDM 回调（官方示例原样）──────────────────────────────────────────────────
void onPDMdata() {
  int bytesAvailable = PDM.available();
  int toRead = min(bytesAvailable, (int)sizeof(sampleBuffer));
  PDM.read(sampleBuffer, toRead);
  samplesRead = toRead / 2;
}

// ── setup ────────────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(9600);

  // LED 初始化（XIAO 上低电平点亮）
  pinMode(LED_RED,   OUTPUT); digitalWrite(LED_RED,   HIGH);
  pinMode(LED_GREEN, OUTPUT); digitalWrite(LED_GREEN, HIGH);
  pinMode(LED_BLUE,  OUTPUT); digitalWrite(LED_BLUE,  HIGH);

  // BLE 初始化
  if (!BLE.begin()) {
    Serial.println("BLE 初始化失败");
    while (1) yield();
  }

  BLE.setLocalName("PsyGuard");
  BLE.setAdvertisedService(nusService);
  nusService.addCharacteristic(txChar);
  nusService.addCharacteristic(rxChar);
  BLE.addService(nusService);
  BLE.setConnectionInterval(12, 12);  // 请求 15ms 连接间隔（12 × 1.25ms）

  rxChar.writeValue((byte)0);   // 默认停止
  BLE.advertise();
  Serial.println("BLE 广播中，等待手机连接...");
  digitalWrite(LED_BLUE, LOW);  // 蓝灯 = 广播中

  // PDM 初始化（官方示例原样）
  PDM.onReceive(onPDMdata);
  PDM.setGain(35);  // gain=20 信号太弱(RMS~100)，35 约提升 1.75x 到 RMS~180
  if (!PDM.begin(1, 16000)) {
    Serial.println("PDM 初始化失败");
    while (1) yield();
  }

  Serial.println("PDM 就绪，16kHz 采集→软件降采样到 8kHz 发 BLE");
}

// ── loop ─────────────────────────────────────────────────────────────────────
void loop() {
  BLEDevice central = BLE.central();

  if (central) {
    Serial.print("手机已连接：");
    Serial.println(central.address());
    digitalWrite(LED_BLUE,  HIGH);
    digitalWrite(LED_GREEN, LOW);   // 绿灯 = 已连接

    while (central.connected()) {
      BLE.poll();  // 确保 BLE 事件被处理

      // 检查手机发来的控制命令（1=开始，0=停止）
      if (rxChar.written()) {
        byte cmd = rxChar.value()[0];
        isRecording = (cmd == 1);
        Serial.print("录制状态：");
        Serial.println(isRecording ? "开始" : "停止");
        digitalWrite(LED_RED, isRecording ? LOW : HIGH);  // 红灯 = 录制中
      }

      // 有新的 PDM 数据 且 处于录制状态 -> 降采样后发送给手机
      if (isRecording && samplesRead > 0) {
        int count = samplesRead;
        samplesRead = 0;  // 先清零，减少和回调的竞态窗口

        // 16kHz → 8kHz：先低通滤波（3点均值消除混叠），再取偶数样本
        int downCount = count / 2;
        for (int i = 0; i < downCount; i++) {
          int idx = i * 2;
          int32_t v = sampleBuffer[idx];
          if (idx > 0)   v += sampleBuffer[idx - 1];
          if (idx + 1 < count) v += sampleBuffer[idx + 1];
          sampleBuffer[i] = (short)(v / 3);
        }

        // sampleBuffer 是 short（2字节），转成 byte* 发送
        uint8_t* raw  = (uint8_t*)sampleBuffer;
        int      total = downCount * 2;

        // BLE 单包最大 244 字节，分包发送
        for (int offset = 0; offset < total; offset += 244) {
          int chunkSize = min(244, total - offset);
          txChar.writeValue(raw + offset, chunkSize);
        }
      }
    }

    // 断开连接
    isRecording = false;
    samplesRead = 0;
    Serial.println("手机已断开，重新广播...");
    digitalWrite(LED_GREEN, HIGH);
    digitalWrite(LED_RED,   HIGH);
    digitalWrite(LED_BLUE,  LOW);   // 回到蓝灯广播状态
  }
}

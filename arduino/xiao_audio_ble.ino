/**
 * XIAO nRF52840 Sense - PDM 麦克风 + BLE 音频流
 *
 * 依赖库（Arduino IDE 库管理器安装）：
 *   - Seeed nRF52 mbed-enabled Boards（Board Package）
 *   - ArduinoBLE
 *
 * BLE 协议：
 *   Service UUID : 6E400001-B5A3-F393-E0A9-E50E24DCCA9E
 *   TX Char      : 6E400003-...  notify -> 手机（音频数据）
 *   Control Char : 6E400002-...  write  <- 手机（0=停止, 1=开始）
 *
 * 音频格式：PCM 16bit, 单声道, 16000 Hz
 * 每包 244 字节（BLE 5.0 最大 MTU - 3 overhead）
 */

#include <PDM.h>
#include <ArduinoBLE.h>

// ─────────────────────────────────────────────────────────────
//  BLE 配置
// ─────────────────────────────────────────────────────────────
#define SERVICE_UUID   "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
#define TX_CHAR_UUID   "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"
#define CTRL_CHAR_UUID "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"
#define BLE_PACKET_SIZE 244   // 字节

BLEService         audioService(SERVICE_UUID);
BLECharacteristic  audioTxChar(TX_CHAR_UUID,   BLERead | BLENotify, BLE_PACKET_SIZE);
BLEByteCharacteristic controlChar(CTRL_CHAR_UUID, BLERead | BLEWrite);

// ─────────────────────────────────────────────────────────────
//  PDM / 音频缓冲
// ─────────────────────────────────────────────────────────────
#define SAMPLE_RATE     16000   // Hz
#define PDM_BUF_SAMPLES 512     // PDM 单次回调最大采样数

// 环形缓冲区（存放待发送的 PCM 字节）
#define RING_SIZE 8192          // 字节，约 256ms 的音频
static uint8_t  ringBuf[RING_SIZE];
static volatile uint16_t ringHead = 0;  // 写指针
static volatile uint16_t ringTail = 0;  // 读指针

static short pdmSamples[PDM_BUF_SAMPLES];

// 录音状态
static volatile bool recording = false;

// ─────────────────────────────────────────────────────────────
//  环形缓冲区工具函数
// ─────────────────────────────────────────────────────────────
inline uint16_t ringAvailable() {
  return (ringHead - ringTail + RING_SIZE) % RING_SIZE;
}

inline void ringWrite(uint8_t byte) {
  uint16_t next = (ringHead + 1) % RING_SIZE;
  if (next != ringTail) {  // 未满才写入
    ringBuf[ringHead] = byte;
    ringHead = next;
  }
  // 若已满则丢弃（防止 PDM 回调阻塞）
}

inline uint8_t ringRead() {
  uint8_t val = ringBuf[ringTail];
  ringTail = (ringTail + 1) % RING_SIZE;
  return val;
}

// ─────────────────────────────────────────────────────────────
//  PDM 回调（中断上下文）
// ─────────────────────────────────────────────────────────────
void onPDMData() {
  int bytes = PDM.available();
  PDM.read(pdmSamples, bytes);

  if (!recording) return;

  int sampleCount = bytes / 2;
  for (int i = 0; i < sampleCount; i++) {
    ringWrite(pdmSamples[i] & 0xFF);
    ringWrite((pdmSamples[i] >> 8) & 0xFF);
  }
}

// ─────────────────────────────────────────────────────────────
//  LED 工具（XIAO 上 LOW = 亮）
// ─────────────────────────────────────────────────────────────
void setLed(bool r, bool g, bool b) {
  digitalWrite(LED_RED,   r ? LOW : HIGH);
  digitalWrite(LED_GREEN, g ? LOW : HIGH);
  digitalWrite(LED_BLUE,  b ? LOW : HIGH);
}

// ─────────────────────────────────────────────────────────────
//  setup
// ─────────────────────────────────────────────────────────────
void setup() {
  Serial.begin(115200);

  pinMode(LED_RED,   OUTPUT);
  pinMode(LED_GREEN, OUTPUT);
  pinMode(LED_BLUE,  OUTPUT);
  setLed(false, false, false);

  // ── BLE 初始化 ──
  if (!BLE.begin()) {
    Serial.println("[ERR] BLE init failed");
    setLed(true, false, false);  // 红灯：错误
    while (1);
  }

  BLE.setLocalName("XIAO-Sense");
  BLE.setDeviceName("XIAO-Sense");
  BLE.setAdvertisedService(audioService);

  audioService.addCharacteristic(audioTxChar);
  audioService.addCharacteristic(controlChar);
  BLE.addService(audioService);

  controlChar.writeValue(0);

  BLE.advertise();
  Serial.println("[BLE] Advertising...");
  setLed(false, false, true);  // 蓝灯：广播中

  // ── PDM 初始化 ──
  PDM.onReceive(onPDMData);
  PDM.setGain(30);  // 0~80，可根据实际调整
  if (!PDM.begin(1, SAMPLE_RATE)) {
    Serial.println("[ERR] PDM init failed");
    setLed(true, false, false);
    while (1);
  }
  Serial.println("[PDM] Ready, 16kHz mono");
}

// ─────────────────────────────────────────────────────────────
//  发送一包音频数据
// ─────────────────────────────────────────────────────────────
void sendAudioChunk() {
  uint16_t avail = ringAvailable();
  if (avail == 0) return;

  uint16_t len = min((uint16_t)BLE_PACKET_SIZE, avail);
  uint8_t  packet[BLE_PACKET_SIZE];

  for (uint16_t i = 0; i < len; i++) {
    packet[i] = ringRead();
  }

  audioTxChar.writeValue(packet, len);
}

// ─────────────────────────────────────────────────────────────
//  loop
// ─────────────────────────────────────────────────────────────
void loop() {
  BLEDevice central = BLE.central();

  if (!central) return;

  Serial.print("[BLE] Connected: ");
  Serial.println(central.address());
  setLed(false, true, false);  // 绿灯：已连接

  while (central.connected()) {

    // 处理手机发来的控制指令
    if (controlChar.written()) {
      uint8_t cmd = controlChar.value();
      recording = (cmd == 1);
      Serial.print("[CMD] Recording: ");
      Serial.println(recording ? "START" : "STOP");
      setLed(recording, true, false);  // 录音中：红+绿；待机：绿
      if (!recording) {
        // 清空缓冲区，避免发送残留数据
        ringHead = ringTail = 0;
      }
    }

    // 发送音频数据
    if (recording) {
      sendAudioChunk();
    }

    // 适当让出 CPU，避免 BLE 栈饥饿
    // 不要加 delay()，会导致音频丢包
    // delay(1) 可选，视实际丢包情况调整
  }

  recording = false;
  ringHead = ringTail = 0;
  Serial.println("[BLE] Disconnected");
  setLed(false, false, true);  // 蓝灯：重新广播
}

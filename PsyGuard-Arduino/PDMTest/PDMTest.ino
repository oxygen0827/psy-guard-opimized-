/*
  PDMTest - 纯麦克风测试，不启动 BLE
  录音通过 USB 串口传到电脑，用来判断 PDM 麦克风本身是否干净
*/

#include <PDM.h>

short sampleBuffer[512];
volatile int samplesRead;

void onPDMdata() {
    int bytesAvailable = PDM.available();
    int toRead = min(bytesAvailable, (int)sizeof(sampleBuffer));
    PDM.read(sampleBuffer, toRead);
    samplesRead = toRead / 2;
}

void setup() {
    Serial.begin(115200);
    while (!Serial);  // 等待串口就绪

    pinMode(LED_GREEN, OUTPUT); digitalWrite(LED_GREEN, HIGH);
    pinMode(LED_RED,   OUTPUT); digitalWrite(LED_RED,   HIGH);

    PDM.onReceive(onPDMdata);
    PDM.setGain(20);
    if (!PDM.begin(1, 16000)) {
        digitalWrite(LED_RED, LOW);  // 红灯 = 初始化失败
        while (1) yield();
    }

    digitalWrite(LED_GREEN, LOW);  // 绿灯 = 准备好了，可以说话
    Serial.println("READY");       // 告诉电脑已就绪
}

void loop() {
    if (samplesRead > 0) {
        int count = samplesRead;
        samplesRead = 0;
        // 直接把 PCM 字节流发到串口
        Serial.write((uint8_t*)sampleBuffer, count * 2);
    }
}

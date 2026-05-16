import Foundation
import Combine
import UserNotifications

final class AppViewModel: ObservableObject, BLEManagerDelegate, ServerRelayDelegate {

    enum VoiceprintCaptureMode: Equatable {
        case enroll
        case verify
    }

    // MARK: - Published

    @Published var bleStatus: String = "未连接"
    @Published var serverStatus: String = "未连接"
    @Published var isRecording: Bool = false
    @Published var alerts: [AlertMessage] = []
    @Published var bleConnected: Bool = false
    @Published var serverConnected: Bool = false
    @Published var transcript: String = ""
    @Published var currentSentence: String = ""  // 讯飞实时中间结果
    @Published var voiceprintStatus: String = "声纹未验证"
    @Published var voiceprintVerified: Bool = false
    @Published var voiceprintBusy: Bool = false
    @Published var voiceprintCaptureMode: VoiceprintCaptureMode?

    // 会话计时
    @Published var sessionDurationText: String = ""

    // 调试开关：用手机麦克风替代 XIAO 固件
    @Published var usePhoneMic: Bool = false

    // MARK: - Private

    private let bleManager  = BLEManager()
    private let relay       = ServerRelay()
    private let micCapture  = MicCapture()
    private let defaultSpeakerId = "counselor_default"
    private let defaultSpeakerName = "咨询师"
    private var sessionStart: Date?
    private var sessionTimer: Timer?

    init() {
        bleManager.delegate = self
        relay.delegate = self
        relay.connect()
        #if targetEnvironment(simulator)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            self?.bleStatus = "模拟器模式"
            self?.bleConnected = true
        }
        #endif
    }

    // MARK: - User Actions

    func toggleRecording() {
        guard voiceprintCaptureMode == nil, !voiceprintBusy else { return }
        isRecording.toggle()
        if isRecording {
            relay.sendStart()
            if usePhoneMic {
                startMicCapture()
            } else {
                bleManager.sendControl(true)
            }
            startSession()
        } else {
            if usePhoneMic {
                micCapture.stop()
            } else {
                bleManager.sendControl(false)
            }
            relay.flushAndStop()
            endSession()
        }
    }

    private func startMicCapture() {
        micCapture.onChunk = { [weak self] data in
            self?.relay.sendAudioChunk(data)
        }
        micCapture.requestAndStart { [weak self] granted in
            if !granted {
                self?.isRecording = false
                self?.relay.sendStop()
                self?.endSession()
                self?.bleStatus = "麦克风权限被拒绝"
            }
        }
    }

    func toggleVoiceprintEnroll() {
        if voiceprintCaptureMode == .enroll {
            stopVoiceprintCapture()
        } else {
            startVoiceprintCapture(.enroll)
        }
    }

    func toggleVoiceprintVerify() {
        if voiceprintCaptureMode == .verify {
            stopVoiceprintCapture()
        } else {
            startVoiceprintCapture(.verify)
        }
    }

    private var audioSourceReady: Bool {
        usePhoneMic ? serverConnected : (bleConnected && serverConnected)
    }

    private func startVoiceprintCapture(_ mode: VoiceprintCaptureMode) {
        guard !isRecording, audioSourceReady, voiceprintCaptureMode == nil, !voiceprintBusy else { return }
        voiceprintCaptureMode = mode
        voiceprintBusy = true
        switch mode {
        case .enroll:
            voiceprintStatus = "正在录入声纹..."
            relay.sendVoiceprintEnrollStart(speakerId: defaultSpeakerId, speakerName: defaultSpeakerName)
        case .verify:
            voiceprintStatus = "正在确认身份..."
            relay.sendVoiceprintVerifyStart(speakerId: defaultSpeakerId)
        }
        startVoiceprintAudioSource()
    }

    private func stopVoiceprintCapture() {
        guard let mode = voiceprintCaptureMode else { return }
        if usePhoneMic {
            micCapture.stop()
        } else {
            bleManager.sendControl(false)
        }
        switch mode {
        case .enroll:
            voiceprintStatus = "正在提交声纹..."
            relay.flushAndStopVoiceprintEnroll()
        case .verify:
            voiceprintStatus = "正在比对声纹..."
            relay.flushAndStopVoiceprintVerify()
        }
        voiceprintCaptureMode = nil
    }

    private func startVoiceprintAudioSource() {
        if usePhoneMic {
            micCapture.onChunk = { [weak self] data in
                self?.relay.sendAudioChunk(data)
            }
            micCapture.requestAndStart { [weak self] granted in
                if !granted {
                    self?.cancelVoiceprintCapture(message: "麦克风权限被拒绝")
                }
            }
        } else {
            bleManager.sendControl(true)
        }
    }

    private func cancelVoiceprintCapture(message: String) {
        guard let mode = voiceprintCaptureMode else { return }
        if usePhoneMic {
            micCapture.stop()
        } else {
            bleManager.sendControl(false)
        }
        switch mode {
        case .enroll:
            relay.flushAndStopVoiceprintEnroll()
        case .verify:
            relay.flushAndStopVoiceprintVerify()
        }
        voiceprintCaptureMode = nil
        voiceprintBusy = false
        voiceprintStatus = message
    }

    func clearAlerts() {
        alerts.removeAll()
    }

    func acknowledgeAlert(id: UUID) {
        if let idx = alerts.firstIndex(where: { $0.id == id }) {
            alerts[idx].isAcknowledged = true
        }
    }

    // MARK: - Session

    private func startSession() {
        sessionStart = Date()
        transcript = ""
        currentSentence = ""
        sessionDurationText = "00:00"
        sessionTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            guard let self, let start = self.sessionStart else { return }
            let elapsed = Int(Date().timeIntervalSince(start))
            let m = elapsed / 60
            let s = elapsed % 60
            self.sessionDurationText = String(format: "%02d:%02d", m, s)
        }
    }

    private func endSession() {
        sessionTimer?.invalidate()
        sessionTimer = nil
        sessionStart = nil
    }

    // MARK: - Local Notification

    private func scheduleNotification(for alert: AlertMessage) {
        let content = UNMutableNotificationContent()

        switch alert.level {
        case .high:
            content.title = "高危预警"
            content.sound = .default
        case .medium:
            content.title = "警告"
            content.sound = .default
        case .low:
            return  // 低级预警不推系统通知，只展示 App 内列表
        }

        var body = alert.text
        if !alert.keyword.isEmpty { body = "[\(alert.keyword)] \(body)" }
        content.body = body
        if !alert.suggestion.isEmpty {
            content.subtitle = alert.suggestion
        }

        let request = UNNotificationRequest(
            identifier: alert.id.uuidString,
            content: content,
            trigger: nil  // 立即触发
        )
        UNUserNotificationCenter.current().add(request, withCompletionHandler: nil)
    }

    // MARK: - BLEManagerDelegate

    func bleStateChanged(_ state: BLEState) {
        DispatchQueue.main.async { [weak self] in
            switch state {
            case .idle:
                self?.bleStatus = "未连接"
                self?.bleConnected = false
                // 仅在 BLE 模式下录音时才因 BLE 掉线停录；手机麦克风模式不依赖 BLE
                if self?.isRecording == true && self?.usePhoneMic == false {
                    self?.isRecording = false
                    self?.relay.flushAndStop()
                    self?.endSession()
                }
                if self?.voiceprintCaptureMode != nil && self?.usePhoneMic == false {
                    self?.cancelVoiceprintCapture(message: "设备断开，声纹采集已停止")
                }
            case .scanning:
                self?.bleStatus = "扫描中..."
                self?.bleConnected = false
            case .connected:
                self?.bleStatus = "已连接 \(self?.bleManager.deviceName ?? "")"
                self?.bleConnected = true
            case .streaming:
                self?.bleStatus = "就绪 - \(self?.bleManager.deviceName ?? "")"
                self?.bleConnected = true
            }
        }
    }

    func bleDidReceiveAudio(_ data: Data) {
        relay.sendAudioChunk(data)
    }

    func bleDidFailWithError(_ error: String) {
        DispatchQueue.main.async { [weak self] in
            self?.bleStatus = "错误: \(error)"
        }
    }

    // MARK: - ServerRelayDelegate

    func relayDidConnect() {
        DispatchQueue.main.async { [weak self] in
            self?.serverStatus = "服务器已连接"
            self?.serverConnected = true
        }
    }

    func relayDidDisconnect() {
        DispatchQueue.main.async { [weak self] in
            self?.serverStatus = "服务器断开，重连中..."
            self?.serverConnected = false
        }
    }

    func relayDidReceiveAlert(_ alert: AlertMessage) {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.alerts.insert(alert, at: 0)
            if self.alerts.count > 50 {
                self.alerts = Array(self.alerts.prefix(50))
            }
            self.scheduleNotification(for: alert)
        }
    }

    func relayDidReceiveTranscript(_ text: String) {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.currentSentence = ""  // 句子已确认，清除中间结果
            if !text.isEmpty {
                self.transcript += text
                if self.transcript.count > 500 {
                    self.transcript = String(self.transcript.suffix(500))
                }
            }
        }
    }

    func relayDidReceiveInterim(_ text: String) {
        DispatchQueue.main.async { [weak self] in
            self?.currentSentence = text
        }
    }

    func relayDidReceiveVoiceprintResult(_ result: VoiceprintResult) {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.voiceprintBusy = false
            self.voiceprintCaptureMode = nil
            if result.stage == "enroll" {
                self.voiceprintVerified = false
                self.voiceprintStatus = "声纹已录入"
                return
            }
            let scoreText: String
            if let score = result.score {
                scoreText = String(format: "%.1f", score)
            } else {
                scoreText = "-"
            }
            if result.verified == true {
                self.voiceprintVerified = true
                self.voiceprintStatus = "身份已确认（\(result.provider)，\(scoreText)）"
            } else {
                self.voiceprintVerified = false
                self.voiceprintStatus = "身份未通过（\(result.provider)，\(scoreText)）"
            }
        }
    }

    func relayDidReceiveVoiceprintError(stage: String, provider: String, message: String, detail: String?) {
        DispatchQueue.main.async { [weak self] in
            self?.voiceprintBusy = false
            self?.voiceprintCaptureMode = nil
            self?.voiceprintVerified = false
            self?.voiceprintStatus = "声纹错误: \(message)"
        }
    }
}

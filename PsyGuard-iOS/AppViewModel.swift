import Foundation
import Combine
import UserNotifications

final class AppViewModel: ObservableObject, BLEManagerDelegate, ServerRelayDelegate {

    // MARK: - Published

    @Published var bleStatus: String = "未连接"
    @Published var serverStatus: String = "未连接"
    @Published var isRecording: Bool = false
    @Published var alerts: [AlertMessage] = []
    @Published var bleConnected: Bool = false
    @Published var serverConnected: Bool = false
    @Published var transcript: String = ""

    // 会话计时
    @Published var sessionDurationText: String = ""

    // MARK: - Private

    private let bleManager = BLEManager()
    private let relay = ServerRelay()
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
        isRecording.toggle()
        bleManager.sendControl(isRecording)
        if isRecording {
            startSession()
            relay.sendStart()
        } else {
            endSession()
            relay.flushRemaining()
            relay.sendStop()
        }
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
            content.sound = .defaultCritical
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
                self?.bleStatus = "扫描中..."
                self?.bleConnected = false
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
            self.transcript += text
            if self.transcript.count > 500 {
                self.transcript = String(self.transcript.suffix(500))
            }
        }
    }
}

import Foundation
import Combine

final class AppViewModel: ObservableObject, BLEManagerDelegate, ServerRelayDelegate {

    // MARK: - Published

    @Published var bleStatus: String = "未连接"
    @Published var serverStatus: String = "未连接"
    @Published var isRecording: Bool = false
    @Published var alerts: [AlertMessage] = []
    @Published var bleConnected: Bool = false
    @Published var serverConnected: Bool = false

    // MARK: - Private

    private let bleManager = BLEManager()
    private let relay = ServerRelay()

    init() {
        bleManager.delegate = self
        relay.delegate = self
        relay.connect()
    }

    // MARK: - User Actions

    func toggleRecording() {
        isRecording.toggle()
        bleManager.sendControl(isRecording)
        if !isRecording {
            relay.flushRemaining()
        }
    }

    func clearAlerts() {
        alerts.removeAll()
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
        // BLE 音频块直接转发到服务器
        relay.sendAudioChunk(data)
    }

    func bleDidFailWithError(_ error: String) {
        DispatchQueue.main.async { [weak self] in
            self?.bleStatus = "错误: \(error)"
        }
    }

    // MARK: - ServerRelayDelegate

    func relayDidConnect() {
        serverStatus = "服务器已连接"
        serverConnected = true
    }

    func relayDidDisconnect() {
        serverStatus = "服务器断开，重连中..."
        serverConnected = false
    }

    func relayDidReceiveAlert(_ alert: AlertMessage) {
        alerts.insert(alert, at: 0)  // 最新的在最上面
        // 限制最多显示 50 条
        if alerts.count > 50 {
            alerts = Array(alerts.prefix(50))
        }
    }
}

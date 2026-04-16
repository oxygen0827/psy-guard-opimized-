import Foundation

// 服务器推回来的预警结构
struct AlertMessage: Identifiable {
    let id = UUID()
    let level: AlertLevel
    let keyword: String
    let text: String
    let time: Date

    enum AlertLevel: String {
        case high   = "high"
        case medium = "medium"
        case low    = "low"

        var color: String {
            switch self {
            case .high:   return "red"
            case .medium: return "orange"
            case .low:    return "yellow"
            }
        }
    }
}

protocol ServerRelayDelegate: AnyObject {
    func relayDidConnect()
    func relayDidDisconnect()
    func relayDidReceiveAlert(_ alert: AlertMessage)
}

final class ServerRelay: NSObject {

    private let serverURL = URL(string: "ws://150.158.146.192:6146")!
    private var webSocketTask: URLSessionWebSocketTask?
    private var urlSession: URLSession!
    private var reconnectTimer: Timer?

    weak var delegate: ServerRelayDelegate?
    private(set) var isConnected = false

    // 发送缓冲：BLE 包很小，积攒一定量再发，减少 WebSocket 帧数
    private var sendBuffer = Data()
    private let bufferThreshold = 4096  // 4KB 触发一次发送
    private let bufferQueue = DispatchQueue(label: "relay.buffer")

    override init() {
        super.init()
        urlSession = URLSession(configuration: .default, delegate: self, delegateQueue: nil)
    }

    // MARK: - Public API

    func connect() {
        guard !isConnected else { return }
        let request = URLRequest(url: serverURL)
        webSocketTask = urlSession.webSocketTask(with: request)
        webSocketTask?.resume()
        receiveLoop()
    }

    func disconnect() {
        reconnectTimer?.invalidate()
        webSocketTask?.cancel(with: .normalClosure, reason: nil)
        isConnected = false
    }

    /// BLE 收到音频块 -> 进缓冲区 -> 达到阈值后发给服务器
    func sendAudioChunk(_ data: Data) {
        bufferQueue.async { [weak self] in
            guard let self else { return }
            self.sendBuffer.append(data)
            if self.sendBuffer.count >= self.bufferThreshold {
                self.flushBuffer()
            }
        }
    }

    /// 强制刷新缓冲（停止录音时调用）
    func flushRemaining() {
        bufferQueue.async { [weak self] in
            self?.flushBuffer()
        }
    }

    // MARK: - Private

    private func flushBuffer() {
        guard isConnected, !sendBuffer.isEmpty else {
            sendBuffer.removeAll()
            return
        }
        let payload = sendBuffer
        sendBuffer.removeAll()
        let message = URLSessionWebSocketTask.Message.data(payload)
        webSocketTask?.send(message) { error in
            if let error {
                print("[Relay] send error: \(error)")
            }
        }
    }

    private func receiveLoop() {
        webSocketTask?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let message):
                self.handleMessage(message)
                self.receiveLoop()  // 继续监听
            case .failure(let error):
                print("[Relay] receive error: \(error)")
                self.handleDisconnect()
            }
        }
    }

    private func handleMessage(_ message: URLSessionWebSocketTask.Message) {
        switch message {
        case .string(let text):
            parseAlert(text)
        case .data(let data):
            if let text = String(data: data, encoding: .utf8) {
                parseAlert(text)
            }
        @unknown default:
            break
        }
    }

    private func parseAlert(_ json: String) {
        guard let data = json.data(using: .utf8),
              let dict = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              dict["type"] as? String == "alert" else { return }

        let level = AlertMessage.AlertLevel(rawValue: dict["level"] as? String ?? "low") ?? .low
        let keyword = dict["keyword"] as? String ?? ""
        let text = dict["text"] as? String ?? ""

        let alert = AlertMessage(level: level, keyword: keyword, text: text, time: Date())
        DispatchQueue.main.async {
            self.delegate?.relayDidReceiveAlert(alert)
        }
    }

    private func handleDisconnect() {
        isConnected = false
        DispatchQueue.main.async {
            self.delegate?.relayDidDisconnect()
        }
        // 5 秒后自动重连
        DispatchQueue.main.asyncAfter(deadline: .now() + 5) { [weak self] in
            self?.connect()
        }
    }
}

// MARK: - URLSessionWebSocketDelegate

extension ServerRelay: URLSessionWebSocketDelegate {

    func urlSession(_ session: URLSession,
                    webSocketTask: URLSessionWebSocketTask,
                    didOpenWithProtocol protocol: String?) {
        isConnected = true
        DispatchQueue.main.async {
            self.delegate?.relayDidConnect()
        }
        print("[Relay] connected to server")
    }

    func urlSession(_ session: URLSession,
                    webSocketTask: URLSessionWebSocketTask,
                    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode,
                    reason: Data?) {
        handleDisconnect()
    }
}

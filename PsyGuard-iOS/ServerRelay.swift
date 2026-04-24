import Foundation

// 服务器推回来的预警结构
struct AlertMessage: Identifiable {
    let id: UUID
    let level: AlertLevel
    let keyword: String
    let text: String
    let suggestion: String
    let time: Date
    var isAcknowledged: Bool = false

    init(level: AlertLevel, keyword: String, text: String, suggestion: String = "", time: Date = Date()) {
        self.id = UUID()
        self.level = level
        self.keyword = keyword
        self.text = text
        self.suggestion = suggestion
        self.time = time
    }

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
    func relayDidReceiveTranscript(_ text: String)
    func relayDidReceiveInterim(_ text: String)
}

final class ServerRelay: NSObject {

    private let serverURL = URL(string: "ws://150.158.146.192:8097")!
    private var webSocketTask: URLSessionWebSocketTask?
    private var urlSession: URLSession!

    weak var delegate: ServerRelayDelegate?
    private(set) var isConnected = false
    private var stopped = false   // 主动调用 disconnect() 时置 true，阻止自动重连

    // 发送缓冲：积攒约 50ms 的音频再发，平衡延迟和帧数
    private var sendBuffer = Data()
    private let bufferThreshold = 1600  // ~50ms @ 16kHz 16-bit mono
    private let bufferQueue = DispatchQueue(label: "relay.buffer")

    override init() {
        super.init()
        urlSession = URLSession(configuration: .default, delegate: self, delegateQueue: nil)
    }

    // MARK: - Public API

    func connect() {
        stopped = false
        guard !isConnected else { return }
        let request = URLRequest(url: serverURL)
        webSocketTask = urlSession.webSocketTask(with: request)
        webSocketTask?.resume()
        receiveLoop()
    }

    func disconnect() {
        stopped = true
        webSocketTask?.cancel(with: .normalClosure, reason: nil)
        isConnected = false
    }

    func sendStart() {
        webSocketTask?.send(.string("START")) { _ in }
    }

    func sendStop() {
        webSocketTask?.send(.string("STOP")) { _ in }
    }

    /// 先刷缓冲，再发 STOP，保证服务器在收到 STOP 前已处理最后一帧音频
    func flushAndStop() {
        bufferQueue.async { [weak self] in
            guard let self else { return }
            self.flushBuffer()
            self.webSocketTask?.send(.string("STOP")) { _ in }
        }
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



    // MARK: - Private

    private func flushBuffer() {
        guard isConnected, !sendBuffer.isEmpty else {
            sendBuffer.removeAll()
            return
        }
        let payload = sendBuffer
        sendBuffer.removeAll()
        let message = URLSessionWebSocketTask.Message.data(payload)
        webSocketTask?.send(message) { _ in }
    }

    private func receiveLoop() {
        webSocketTask?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let message):
                self.handleMessage(message)
                self.receiveLoop()  // 继续监听
            case .failure:
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
              let type = dict["type"] as? String else { return }

        if type == "transcript" {
            let text = dict["text"] as? String ?? ""
            DispatchQueue.main.async { self.delegate?.relayDidReceiveTranscript(text) }
            return
        }

        if type == "interim" {
            let text = dict["text"] as? String ?? ""
            DispatchQueue.main.async { self.delegate?.relayDidReceiveInterim(text) }
            return
        }

        guard type == "alert" else { return }
        let level = AlertMessage.AlertLevel(rawValue: dict["level"] as? String ?? "low") ?? .low
        let keyword    = dict["keyword"]    as? String ?? ""
        let text       = dict["text"]       as? String ?? ""
        let suggestion = dict["suggestion"] as? String ?? ""
        let alert = AlertMessage(level: level, keyword: keyword, text: text, suggestion: suggestion)
        DispatchQueue.main.async {
            self.delegate?.relayDidReceiveAlert(alert)
        }
    }

    private func handleDisconnect() {
        isConnected = false
        DispatchQueue.main.async {
            self.delegate?.relayDidDisconnect()
        }
        guard !stopped else { return }
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
    }

    func urlSession(_ session: URLSession,
                    webSocketTask: URLSessionWebSocketTask,
                    didCloseWith closeCode: URLSessionWebSocketTask.CloseCode,
                    reason: Data?) {
        handleDisconnect()
    }
}

import Foundation
import Combine

// ─────────────────────────────────────────────────────────────
//  ServerRelay
//  负责：将 BLE 收到的 PCM 音频块通过 WebSocket 实时转发到服务器
//  服务器收到后做 STT + LLM 分析，预警通过 WebSocket 推回来
// ─────────────────────────────────────────────────────────────
final class ServerRelay: ObservableObject {

    // 服务器推回来的预警
    @Published var latestAlert: AlertMessage?
    @Published var isConnected: Bool = false

    private var wsTask: URLSessionWebSocketTask?
    private let session = URLSession(configuration: .default)

    // 音频缓冲（积攒到一定量再发，减少 WebSocket 帧数）
    private var audioBuffer = Data()
    private let flushThreshold = 4096  // 字节，约 128ms

    // ── 连接 ───────────────────────────────────────────────────

    func connect(to urlString: String) {
        guard let url = URL(string: urlString) else { return }
        wsTask = session.webSocketTask(with: url)
        wsTask?.resume()
        isConnected = true
        listenForMessages()
    }

    func disconnect() {
        wsTask?.cancel(with: .normalClosure, reason: nil)
        wsTask = nil
        isConnected = false
        audioBuffer.removeAll()
    }

    // ── 发送音频数据 ───────────────────────────────────────────

    /// 每次收到 BLE 音频包时调用
    func feed(audioChunk: Data) {
        audioBuffer.append(audioChunk)
        if audioBuffer.count >= flushThreshold {
            flush()
        }
    }

    /// 停止录音时手动 flush 剩余数据
    func flushIfNeeded() {
        if !audioBuffer.isEmpty { flush() }
    }

    private func flush() {
        guard isConnected, let ws = wsTask else {
            audioBuffer.removeAll()
            return
        }
        let payload = audioBuffer
        audioBuffer.removeAll()

        // 二进制帧直接发 PCM，服务器按帧拼接
        ws.send(.data(payload)) { [weak self] error in
            if let error = error {
                print("[WS] Send error: \(error.localizedDescription)")
                self?.isConnected = false
            }
        }
    }

    // ── 接收服务器消息（预警推送）──────────────────────────────

    private func listenForMessages() {
        wsTask?.receive { [weak self] result in
            guard let self else { return }
            switch result {
            case .success(let message):
                if case .string(let text) = message {
                    self.handleServerMessage(text)
                }
                // 继续监听
                self.listenForMessages()
            case .failure(let error):
                print("[WS] Receive error: \(error.localizedDescription)")
                DispatchQueue.main.async { self.isConnected = false }
            }
        }
    }

    private func handleServerMessage(_ json: String) {
        guard let data = json.data(using: .utf8),
              let alert = try? JSONDecoder().decode(AlertMessage.self, from: data) else {
            return
        }
        DispatchQueue.main.async { [weak self] in
            self?.latestAlert = alert
        }
    }
}

// ─────────────────────────────────────────────────────────────
//  AlertMessage - 服务器下发的预警结构
//  根据你的服务器实际返回格式调整字段
// ─────────────────────────────────────────────────────────────
struct AlertMessage: Codable, Identifiable {
    let id: String          // 唯一 ID
    let level: AlertLevel   // 严重程度
    let keyword: String?    // 触发关键词（可选）
    let text: String        // 原始转写文本片段
    let suggestion: String  // 建议干预话术
    let timestamp: Double   // Unix 时间戳

    var date: Date { Date(timeIntervalSince1970: timestamp) }
}

enum AlertLevel: String, Codable {
    case low    = "low"     // 一般敏感词
    case medium = "medium"  // 需关注
    case high   = "high"    // 立即干预
}

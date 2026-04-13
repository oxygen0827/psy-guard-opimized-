import SwiftUI

// ─────────────────────────────────────────────────────────────
//  AppState - 统一管理 BLE + 服务器中继
// ─────────────────────────────────────────────────────────────
final class AppState: ObservableObject {

    let ble = BLEAudioManager()
    let relay = ServerRelay()

    // 改成你的服务器地址
    private let serverURL = "ws://your-server.com/audio"

    @Published var isRecording = false
    @Published var alerts: [AlertMessage] = []

    init() {
        // BLE 收到音频 -> 转发给服务器
        ble.onAudioChunk = { [weak self] data in
            self?.relay.feed(audioChunk: data)
        }

        // 服务器预警 -> 追加到列表
        relay.$latestAlert
            .compactMap { $0 }
            .receive(on: RunLoop.main)
            .sink { [weak self] alert in
                self?.alerts.insert(alert, at: 0)
                // 高风险触发震动
                if alert.level == .high {
                    UINotificationFeedbackGenerator().notificationOccurred(.warning)
                }
            }
            .store(in: &cancellables)
    }

    private var cancellables = Set<AnyCancellable>()

    func toggleRecording() {
        isRecording.toggle()
        ble.setRecording(isRecording)
        if !isRecording {
            relay.flushIfNeeded()
        }
    }

    func connectServer() {
        relay.connect(to: serverURL)
    }

    func clearAlerts() {
        alerts.removeAll()
    }
}

// ─────────────────────────────────────────────────────────────
//  ContentView
// ─────────────────────────────────────────────────────────────
struct ContentView: View {
    @StateObject private var app = AppState()

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                StatusBarView(app: app)
                Divider()
                AlertListView(alerts: app.alerts)
            }
            .navigationTitle("心理咨询监测")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button("清空") { app.clearAlerts() }
                        .disabled(app.alerts.isEmpty)
                }
            }
        }
        .onAppear { app.connectServer() }
    }
}

// ─────────────────────────────────────────────────────────────
//  状态栏（BLE + 服务器 + 录音控制）
// ─────────────────────────────────────────────────────────────
struct StatusBarView: View {
    @ObservedObject var app: AppState

    var bleStatusText: String {
        switch app.ble.state {
        case .idle:         return "未启动"
        case .scanning:     return "扫描中..."
        case .connecting:   return "连接中..."
        case .connected:    return "已连接 (\(app.ble.rssi) dBm)"
        case .disconnected: return "已断开"
        }
    }

    var bleStatusColor: Color {
        switch app.ble.state {
        case .connected:    return .green
        case .scanning,
             .connecting:   return .orange
        default:            return .red
        }
    }

    var body: some View {
        VStack(spacing: 12) {
            HStack(spacing: 24) {
                StatusDot(label: "设备", value: bleStatusText, color: bleStatusColor)
                StatusDot(label: "服务器",
                          value: app.relay.isConnected ? "已连接" : "未连接",
                          color: app.relay.isConnected ? .green : .red)
            }
            .padding(.top, 12)

            // 录音按钮
            Button(action: app.toggleRecording) {
                HStack(spacing: 8) {
                    Image(systemName: app.isRecording ? "stop.circle.fill" : "mic.circle.fill")
                        .font(.title2)
                    Text(app.isRecording ? "停止监测" : "开始监测")
                        .fontWeight(.semibold)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 14)
                .background(app.isRecording ? Color.red : Color.blue)
                .foregroundColor(.white)
                .cornerRadius(12)
                .padding(.horizontal, 20)
            }
            .disabled(app.ble.state != .connected || !app.relay.isConnected)
            .padding(.bottom, 12)
        }
        .background(Color(.systemGroupedBackground))
    }
}

struct StatusDot: View {
    let label: String
    let value: String
    let color: Color

    var body: some View {
        VStack(spacing: 4) {
            HStack(spacing: 6) {
                Circle().fill(color).frame(width: 8, height: 8)
                Text(value).font(.subheadline).foregroundColor(.primary)
            }
            Text(label).font(.caption).foregroundColor(.secondary)
        }
    }
}

// ─────────────────────────────────────────────────────────────
//  预警列表
// ─────────────────────────────────────────────────────────────
struct AlertListView: View {
    let alerts: [AlertMessage]

    var body: some View {
        if alerts.isEmpty {
            VStack(spacing: 12) {
                Spacer()
                Image(systemName: "checkmark.shield")
                    .font(.system(size: 48))
                    .foregroundColor(.green.opacity(0.6))
                Text("暂无预警").foregroundColor(.secondary)
                Spacer()
            }
        } else {
            List(alerts) { alert in
                AlertRowView(alert: alert)
            }
            .listStyle(.plain)
        }
    }
}

struct AlertRowView: View {
    let alert: AlertMessage

    var levelColor: Color {
        switch alert.level {
        case .low:    return .yellow
        case .medium: return .orange
        case .high:   return .red
        }
    }

    var levelText: String {
        switch alert.level {
        case .low:    return "关注"
        case .medium: return "警示"
        case .high:   return "紧急"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label(levelText, systemImage: "exclamationmark.triangle.fill")
                    .foregroundColor(levelColor)
                    .fontWeight(.semibold)
                Spacer()
                Text(alert.date, style: .time)
                    .font(.caption)
                    .foregroundColor(.secondary)
            }

            if let kw = alert.keyword {
                Text("触发词：\(kw)")
                    .font(.caption)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(levelColor.opacity(0.15))
                    .cornerRadius(6)
            }

            Text("「\(alert.text)」")
                .font(.subheadline)
                .foregroundColor(.secondary)
                .lineLimit(2)

            Text(alert.suggestion)
                .font(.subheadline)
                .padding(10)
                .background(Color(.secondarySystemGroupedBackground))
                .cornerRadius(8)
        }
        .padding(.vertical, 4)
    }
}

#Preview {
    ContentView()
}

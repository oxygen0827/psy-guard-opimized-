import SwiftUI

struct ContentView: View {

    @StateObject private var vm = AppViewModel()

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                statusBar
                recordButton
                alertList
            }
            .navigationTitle("心理咨询预警")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("清空") { vm.clearAlerts() }
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    // MARK: - 状态栏

    private var statusBar: some View {
        VStack(spacing: 8) {
            HStack {
                Circle()
                    .fill(vm.bleConnected ? .green : .gray)
                    .frame(width: 10, height: 10)
                Text("设备: \(vm.bleStatus)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
            }
            HStack {
                Circle()
                    .fill(vm.serverConnected ? .green : .orange)
                    .frame(width: 10, height: 10)
                Text("服务器: \(vm.serverStatus)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
            }
        }
        .padding(.horizontal)
        .padding(.vertical, 12)
        .background(Color(.systemGroupedBackground))
    }

    // MARK: - 录音按钮

    private var recordButton: some View {
        Button(action: { vm.toggleRecording() }) {
            VStack(spacing: 8) {
                Image(systemName: vm.isRecording ? "mic.fill" : "mic")
                    .font(.system(size: 48))
                    .foregroundStyle(vm.isRecording ? .red : .primary)
                    .symbolEffect(.pulse, isActive: vm.isRecording)
                Text(vm.isRecording ? "录制中 - 点击停止" : "点击开始监听")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 32)
        .disabled(!vm.bleConnected)
        .opacity(vm.bleConnected ? 1 : 0.4)
    }

    // MARK: - 预警列表

    private var alertList: some View {
        Group {
            if vm.alerts.isEmpty {
                ContentUnavailableView(
                    "暂无预警",
                    systemImage: "checkmark.shield",
                    description: Text("开始监听后，检测到异常内容将在此显示")
                )
            } else {
                List(vm.alerts) { alert in
                    AlertRow(alert: alert)
                }
                .listStyle(.plain)
            }
        }
    }
}

// MARK: - 预警行

struct AlertRow: View {
    let alert: AlertMessage

    private var levelColor: Color {
        switch alert.level {
        case .high:   return .red
        case .medium: return .orange
        case .low:    return .yellow
        }
    }

    private var levelText: String {
        switch alert.level {
        case .high:   return "高危"
        case .medium: return "警告"
        case .low:    return "提示"
        }
    }

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            // 等级标签
            Text(levelText)
                .font(.caption2.bold())
                .foregroundStyle(.white)
                .padding(.horizontal, 6)
                .padding(.vertical, 3)
                .background(levelColor, in: Capsule())

            VStack(alignment: .leading, spacing: 4) {
                if !alert.keyword.isEmpty {
                    Text("关键词：\(alert.keyword)")
                        .font(.caption.bold())
                        .foregroundStyle(levelColor)
                }
                Text(alert.text)
                    .font(.subheadline)
                    .lineLimit(3)
                Text(alert.time.formatted(date: .omitted, time: .standard))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 4)
    }
}

#Preview {
    ContentView()
}

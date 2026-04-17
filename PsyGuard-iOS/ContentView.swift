import SwiftUI

struct ContentView: View {

    @StateObject private var vm = AppViewModel()

    var body: some View {
        NavigationStack {
            VStack(spacing: 0) {
                statusBar
                recordButton
                transcriptBox
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
        VStack(spacing: 6) {
            HStack {
                Circle()
                    .fill(vm.bleConnected ? .green : .gray)
                    .frame(width: 10, height: 10)
                Text("设备: \(vm.bleStatus)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                if vm.isRecording {
                    Label(vm.sessionDurationText, systemImage: "waveform")
                        .font(.caption.monospacedDigit())
                        .foregroundStyle(.red)
                }
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
        .padding(.vertical, 10)
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
        .padding(.vertical, 28)
        .disabled(!vm.bleConnected)
        .opacity(vm.bleConnected ? 1 : 0.4)
    }

    // MARK: - 实时字幕

    private var transcriptBox: some View {
        Group {
            if vm.isRecording || !vm.transcript.isEmpty {
                ScrollViewReader { proxy in
                    ScrollView {
                        Text(vm.transcript.isEmpty ? "等待语音..." : vm.transcript)
                            .font(.footnote)
                            .foregroundStyle(vm.transcript.isEmpty ? .secondary : .primary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(10)
                            .id("bottom")
                    }
                    .frame(height: 76)
                    .background(Color(.secondarySystemGroupedBackground))
                    .clipShape(RoundedRectangle(cornerRadius: 10))
                    .padding(.horizontal)
                    .padding(.bottom, 8)
                    .onChange(of: vm.transcript) { _, _ in
                        proxy.scrollTo("bottom", anchor: .bottom)
                    }
                }
            }
        }
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
                    AlertRow(alert: alert) { id in
                        vm.acknowledgeAlert(id: id)
                    }
                }
                .listStyle(.plain)
            }
        }
    }
}

// MARK: - 预警行

struct AlertRow: View {
    let alert: AlertMessage
    let onAcknowledge: (UUID) -> Void

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
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top) {
                Text(levelText)
                    .font(.caption2.bold())
                    .foregroundStyle(.white)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 3)
                    .background(levelColor, in: Capsule())

                VStack(alignment: .leading, spacing: 2) {
                    if !alert.keyword.isEmpty {
                        Text("关键词：\(alert.keyword)")
                            .font(.caption.bold())
                            .foregroundStyle(levelColor)
                    }
                    Text(alert.time.formatted(date: .omitted, time: .standard))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }

                Spacer()

                if alert.isAcknowledged {
                    Label("已处理", systemImage: "checkmark.circle.fill")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                } else {
                    Button {
                        onAcknowledge(alert.id)
                    } label: {
                        Text("标记处理")
                            .font(.caption2)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 4)
                            .background(.blue.opacity(0.12), in: Capsule())
                            .foregroundStyle(.blue)
                    }
                    .buttonStyle(.plain)
                }
            }

            Text(alert.text)
                .font(.subheadline)
                .lineLimit(4)

            if !alert.suggestion.isEmpty {
                Label(alert.suggestion, systemImage: "lightbulb")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.top, 2)
            }
        }
        .padding(.vertical, 4)
        .opacity(alert.isAcknowledged ? 0.55 : 1)
    }
}

#Preview {
    ContentView()
}

import AVFoundation

/// 用手机麦克风采集音频，输出 16kHz 16-bit PCM mono，格式与 XIAO 固件完全一致。
/// 仅用于调试：绕过 BLE 固件，直接验证服务器端语音识别是否正常。
final class MicCapture {

    private let engine    = AVAudioEngine()
    private var converter: AVAudioConverter?

    private let targetFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16,
        sampleRate:   16000,
        channels:     1,
        interleaved:  true
    )!

    var onChunk: ((Data) -> Void)?

    // MARK: - Public

    func requestAndStart(completion: @escaping (Bool) -> Void) {
        AVAudioSession.sharedInstance().requestRecordPermission { [weak self] granted in
            DispatchQueue.main.async {
                guard granted, let self else { completion(false); return }
                do {
                    try self.startEngine()
                    completion(true)
                } catch {
                    completion(false)
                }
            }
        }
    }

    func stop() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        try? AVAudioSession.sharedInstance().setActive(false,
              options: .notifyOthersOnDeactivation)
    }

    // MARK: - Private

    private func startEngine() throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.record, mode: .measurement, options: [])
        try session.setActive(true)

        let inputNode = engine.inputNode
        let hwFormat  = inputNode.inputFormat(forBus: 0)
        converter     = AVAudioConverter(from: hwFormat, to: targetFormat)

        // 每 50ms 回调一次，与 XIAO 发包节奏接近
        let bufSize = AVAudioFrameCount(hwFormat.sampleRate * 0.05)
        inputNode.installTap(onBus: 0, bufferSize: bufSize, format: hwFormat) { [weak self] buf, _ in
            self?.convert(buf)
        }

        engine.prepare()
        try engine.start()
    }

    private func convert(_ input: AVAudioPCMBuffer) {
        guard let converter else { return }

        let ratio    = targetFormat.sampleRate / input.format.sampleRate
        let capacity = AVAudioFrameCount(Double(input.frameLength) * ratio) + 1

        guard let output = AVAudioPCMBuffer(pcmFormat: targetFormat,
                                            frameCapacity: capacity) else { return }

        var inputConsumed = false
        var err: NSError?
        converter.convert(to: output, error: &err) { _, status in
            guard !inputConsumed else { status.pointee = .noDataNow; return nil }
            inputConsumed        = true
            status.pointee       = .haveData
            return input
        }

        guard err == nil, output.frameLength > 0 else { return }

        let data = Data(bytes: output.int16ChannelData![0],
                        count: Int(output.frameLength) * 2)
        onChunk?(data)
    }
}

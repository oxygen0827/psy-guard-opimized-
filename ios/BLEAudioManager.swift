import CoreBluetooth
import Combine

// ─────────────────────────────────────────────────────────────
//  UUID 常量（与 Arduino 代码保持一致）
// ─────────────────────────────────────────────────────────────
enum XIOUUID {
    static let service = CBUUID(string: "6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
    static let txAudio = CBUUID(string: "6E400003-B5A3-F393-E0A9-E50E24DCCA9E")  // notify <- 开发板
    static let control = CBUUID(string: "6E400002-B5A3-F393-E0A9-E50E24DCCA9E")  // write  -> 开发板
}

// ─────────────────────────────────────────────────────────────
//  连接状态
// ─────────────────────────────────────────────────────────────
enum BLEState {
    case idle
    case scanning
    case connecting
    case connected
    case disconnected(Error?)
}

// ─────────────────────────────────────────────────────────────
//  BLEAudioManager
//  负责：扫描、连接、订阅音频通知、发送控制指令
// ─────────────────────────────────────────────────────────────
final class BLEAudioManager: NSObject, ObservableObject {

    // UI 可观察属性
    @Published var state: BLEState = .idle
    @Published var rssi: Int = 0

    // 音频数据回调（外部注册，避免直接耦合）
    var onAudioChunk: ((Data) -> Void)?

    private var centralManager: CBCentralManager!
    private var peripheral: CBPeripheral?
    private var txChar: CBCharacteristic?
    private var ctrlChar: CBCharacteristic?

    override init() {
        super.init()
        centralManager = CBCentralManager(delegate: self, queue: .main)
    }

    // ── 公开接口 ───────────────────────────────────────────────

    func startScan() {
        guard centralManager.state == .poweredOn else { return }
        state = .scanning
        centralManager.scanForPeripherals(
            withServices: [XIOUUID.service],
            options: [CBCentralManagerScanOptionAllowDuplicatesKey: false]
        )
    }

    func stopScan() {
        centralManager.stopScan()
    }

    func disconnect() {
        guard let p = peripheral else { return }
        centralManager.cancelPeripheralConnection(p)
    }

    /// 发送录音控制指令
    func setRecording(_ on: Bool) {
        guard let p = peripheral, let ctrl = ctrlChar else { return }
        let value: UInt8 = on ? 0x01 : 0x00
        p.writeValue(Data([value]), for: ctrl, type: .withResponse)
    }
}

// ─────────────────────────────────────────────────────────────
//  CBCentralManagerDelegate
// ─────────────────────────────────────────────────────────────
extension BLEAudioManager: CBCentralManagerDelegate {

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        if central.state == .poweredOn {
            startScan()
        } else {
            state = .idle
        }
    }

    func centralManager(_ central: CBCentralManager,
                        didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any],
                        rssi RSSI: NSNumber) {
        // 找到 XIAO-Sense 就立即连接
        guard peripheral.name == "XIAO-Sense" else { return }
        stopScan()
        self.peripheral = peripheral
        state = .connecting
        centralManager.connect(peripheral, options: nil)
    }

    func centralManager(_ central: CBCentralManager,
                        didConnect peripheral: CBPeripheral) {
        state = .connected
        peripheral.delegate = self
        peripheral.discoverServices([XIOUUID.service])
        peripheral.readRSSI()
    }

    func centralManager(_ central: CBCentralManager,
                        didDisconnectPeripheral peripheral: CBPeripheral,
                        error: Error?) {
        state = .disconnected(error)
        txChar = nil
        ctrlChar = nil
        self.peripheral = nil
        // 断连后自动重扫
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in
            self?.startScan()
        }
    }

    func centralManager(_ central: CBCentralManager,
                        didFailToConnect peripheral: CBPeripheral,
                        error: Error?) {
        state = .disconnected(error)
        startScan()
    }
}

// ─────────────────────────────────────────────────────────────
//  CBPeripheralDelegate
// ─────────────────────────────────────────────────────────────
extension BLEAudioManager: CBPeripheralDelegate {

    func peripheral(_ peripheral: CBPeripheral,
                    didDiscoverServices error: Error?) {
        guard let services = peripheral.services else { return }
        for service in services where service.uuid == XIOUUID.service {
            peripheral.discoverCharacteristics(
                [XIOUUID.txAudio, XIOUUID.control],
                for: service
            )
        }
    }

    func peripheral(_ peripheral: CBPeripheral,
                    didDiscoverCharacteristicsFor service: CBService,
                    error: Error?) {
        guard let chars = service.characteristics else { return }
        for char in chars {
            switch char.uuid {
            case XIOUUID.txAudio:
                txChar = char
                // 订阅音频通知
                peripheral.setNotifyValue(true, for: char)
            case XIOUUID.control:
                ctrlChar = char
            default:
                break
            }
        }
    }

    /// 收到音频数据包
    func peripheral(_ peripheral: CBPeripheral,
                    didUpdateValueFor characteristic: CBCharacteristic,
                    error: Error?) {
        guard characteristic.uuid == XIOUUID.txAudio,
              let data = characteristic.value else { return }
        onAudioChunk?(data)
    }

    func peripheral(_ peripheral: CBPeripheral, didReadRSSI RSSI: NSNumber, error: Error?) {
        rssi = RSSI.intValue
    }
}

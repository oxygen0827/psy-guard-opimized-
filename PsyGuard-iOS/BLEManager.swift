import Foundation
import CoreBluetooth

// Nordic UART Service UUIDs — 和 Arduino 端保持一致
private let kServiceUUID        = CBUUID(string: "6E400001-B5A3-F393-E0A9-E50E24DCCA9E")
private let kTXCharUUID         = CBUUID(string: "6E400003-B5A3-F393-E0A9-E50E24DCCA9E") // 设备→手机 notify
private let kRXCharUUID         = CBUUID(string: "6E400002-B5A3-F393-E0A9-E50E24DCCA9E") // 手机→设备 write

enum BLEState {
    case idle, scanning, connected, streaming
}

protocol BLEManagerDelegate: AnyObject {
    func bleStateChanged(_ state: BLEState)
    func bleDidReceiveAudio(_ data: Data)
    func bleDidFailWithError(_ error: String)
}

final class BLEManager: NSObject, ObservableObject {

    // MARK: - Published
    @Published var state: BLEState = .idle
    @Published var deviceName: String = ""
    @Published var isRecording: Bool = false

    // MARK: - Private
    private var centralManager: CBCentralManager!
    private var peripheral: CBPeripheral?
    private var txChar: CBCharacteristic?  // 接收音频
    private var rxChar: CBCharacteristic?  // 发送控制命令

    weak var delegate: BLEManagerDelegate?

    override init() {
        super.init()
        centralManager = CBCentralManager(delegate: self, queue: .main)
    }

    // MARK: - Public API

    func startScan() {
        guard centralManager.state == .poweredOn else {
            print("[BLE] startScan 被调用但蓝牙未就绪，state=\(centralManager.state.rawValue)")
            return
        }
        state = .scanning
        delegate?.bleStateChanged(.scanning)
        print("[BLE] 开始扫描所有设备...")
        centralManager.scanForPeripherals(withServices: nil, options: [CBCentralManagerScanOptionAllowDuplicatesKey: false])
    }

    func stopScan() {
        centralManager.stopScan()
    }

    func disconnect() {
        guard let p = peripheral else { return }
        centralManager.cancelPeripheralConnection(p)
    }

    /// 发送开始/停止录音指令到开发板
    func sendControl(_ start: Bool) {
        guard let p = peripheral, let char = rxChar else { return }
        let byte: UInt8 = start ? 1 : 0
        p.writeValue(Data([byte]), for: char, type: .withResponse)
        isRecording = start
    }
}

// MARK: - CBCentralManagerDelegate

extension BLEManager: CBCentralManagerDelegate {

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        switch central.state {
        case .poweredOn:
            print("[BLE] 蓝牙已开启，开始扫描")
            startScan()
        case .poweredOff:
            print("[BLE] 蓝牙已关闭")
        case .unauthorized:
            print("[BLE] 蓝牙权限被拒绝")
        case .unsupported:
            print("[BLE] 设备不支持蓝牙")
        case .resetting:
            print("[BLE] 蓝牙重置中")
        case .unknown:
            print("[BLE] 蓝牙状态未知")
        @unknown default:
            break
        }
    }

    func centralManager(_ central: CBCentralManager,
                        didDiscover peripheral: CBPeripheral,
                        advertisementData: [String: Any],
                        rssi RSSI: NSNumber) {
        let name = peripheral.name ?? "(无名)"
        print("[BLE] 发现设备: \(name)  RSSI=\(RSSI)  advData=\(advertisementData.keys.joined(separator: ","))")
        guard name.contains("XIAO") || name.contains("Sense") || name.contains("Psy") || name.contains("Arduino") else { return }

        self.peripheral = peripheral
        centralManager.stopScan()
        centralManager.connect(peripheral, options: nil)
        deviceName = name
    }

    func centralManager(_ central: CBCentralManager,
                        didConnect peripheral: CBPeripheral) {
        peripheral.delegate = self
        peripheral.discoverServices([kServiceUUID])
        state = .connected
        delegate?.bleStateChanged(.connected)
    }

    func centralManager(_ central: CBCentralManager,
                        didDisconnectPeripheral peripheral: CBPeripheral,
                        error: Error?) {
        self.peripheral = nil
        txChar = nil
        rxChar = nil
        state = .idle
        isRecording = false
        delegate?.bleStateChanged(.idle)
        // 断线后自动重扫
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) { [weak self] in
            self?.startScan()
        }
    }

    func centralManager(_ central: CBCentralManager,
                        didFailToConnect peripheral: CBPeripheral,
                        error: Error?) {
        delegate?.bleDidFailWithError(error?.localizedDescription ?? "连接失败")
        startScan()
    }
}

// MARK: - CBPeripheralDelegate

extension BLEManager: CBPeripheralDelegate {

    func peripheral(_ peripheral: CBPeripheral,
                    didDiscoverServices error: Error?) {
        guard let services = peripheral.services else { return }
        for service in services where service.uuid == kServiceUUID {
            peripheral.discoverCharacteristics([kTXCharUUID, kRXCharUUID], for: service)
        }
    }

    func peripheral(_ peripheral: CBPeripheral,
                    didDiscoverCharacteristicsFor service: CBService,
                    error: Error?) {
        guard let chars = service.characteristics else { return }
        for char in chars {
            if char.uuid == kTXCharUUID {
                txChar = char
                peripheral.setNotifyValue(true, for: char) // 订阅音频通知
                state = .streaming
                delegate?.bleStateChanged(.streaming)
            } else if char.uuid == kRXCharUUID {
                rxChar = char
            }
        }
    }

    func peripheral(_ peripheral: CBPeripheral,
                    didUpdateValueFor characteristic: CBCharacteristic,
                    error: Error?) {
        guard characteristic.uuid == kTXCharUUID,
              let data = characteristic.value else { return }
        delegate?.bleDidReceiveAudio(data)
    }
}

import AppKit
import IOKit.pwr_mgt

/// Only idle sleep assertions: no synthetic input, activity declaration, unlock,
/// or system preference changes. A separate screen-saver policy may still lock.
enum KeepAwakeAssertionKind: String, CaseIterable {
    case system = "PreventUserIdleSystemSleep"
    case display = "PreventUserIdleDisplaySleep"

    var label: String { self == .system ? "系统" : "显示器" }
}

struct KeepAwakeFailure: Error, Equatable {
    let message: String
}

protocol KeepAwakeAssertionBackend: AnyObject {
    func create(_ kind: KeepAwakeAssertionKind) -> Result<UInt32, KeepAwakeFailure>
    func isActive(_ id: UInt32) -> Bool
    func release(_ id: UInt32) -> Result<Void, KeepAwakeFailure>
}

final class SystemKeepAwakeBackend: KeepAwakeAssertionBackend {
    func create(_ kind: KeepAwakeAssertionKind) -> Result<UInt32, KeepAwakeFailure> {
        var id: IOPMAssertionID = 0
        let result = IOPMAssertionCreateWithName(kind.rawValue as CFString,
                                               IOPMAssertionLevel(kIOPMAssertionLevelOn),
                                               "任务雷达夜间监督：防止空闲休眠" as CFString, &id)
        guard result == kIOReturnSuccess else { return .failure(failure("创建", result)) }
        return .success(id)
    }

    func isActive(_ id: UInt32) -> Bool {
        guard let properties = IOPMAssertionCopyProperties(id)?.takeRetainedValue() as? [String: Any],
              let level = properties[kIOPMAssertionLevelKey] as? NSNumber else { return false }
        return level.uint32Value == kIOPMAssertionLevelOn
    }

    func release(_ id: UInt32) -> Result<Void, KeepAwakeFailure> {
        let result = IOPMAssertionRelease(id)
        // A removed assertion after wake needs no release. Its identifier must
        // not prevent a fresh assertion from being acquired.
        if result != kIOReturnSuccess, IOPMAssertionCopyProperties(id)?.takeRetainedValue() == nil { return .success(()) }
        guard result == kIOReturnSuccess else { return .failure(failure("释放", result)) }
        return .success(())
    }

    private func failure(_ operation: String, _ code: IOReturn) -> KeepAwakeFailure {
        KeepAwakeFailure(message: "\(operation)失败（IOKit \(String(format: "0x%08x", code))）")
    }
}

struct KeepAwakeStatus: Equatable {
    var requested = false
    var systemActive = false
    var displayActive = false
    var errors: [String] = []
    var fullyActive: Bool { requested && systemActive && displayActive && errors.isEmpty }
    var hasActiveAssertion: Bool { systemActive || displayActive }
    var summary: String {
        if fullyActive { return "夜间常亮已开启" }
        if requested { return "夜间常亮未完全生效" }
        return hasActiveAssertion ? "夜间常亮关闭待重试" : "夜间常亮已关闭"
    }
    var detail: String {
        (["系统防休眠：\(systemActive ? "已生效" : "未生效")；显示器常亮：\(displayActive ? "已生效" : "未生效")"] + errors +
         ["仅阻止空闲休眠；屏保自动锁定、主动锁屏、合盖或低电量仍可能停止桌面操作。不会自动解锁或修改锁屏设置。"])
            .joined(separator: "\n")
    }
}

/// Owned by the app/store. Call on the main thread, set its persisted preference
/// explicitly at startup, and publish status through onStatusChange.
final class KeepAwakeController {
    private let backend: KeepAwakeAssertionBackend
    private var assertions: [KeepAwakeAssertionKind: UInt32] = [:]
    private var requested = false
    private var wakeObserver: NSObjectProtocol?
    private var terminationObserver: NSObjectProtocol?
    private var timer: Timer?
    private(set) var status = KeepAwakeStatus()
    var onStatusChange: ((KeepAwakeStatus) -> Void)?

    init(backend: KeepAwakeAssertionBackend = SystemKeepAwakeBackend(), observeLifecycle: Bool = true) {
        self.backend = backend
        if observeLifecycle {
            wakeObserver = NSWorkspace.shared.notificationCenter.addObserver(
                forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
                    self?.recheck()
                }
            terminationObserver = NotificationCenter.default.addObserver(
                forName: NSApplication.willTerminateNotification, object: nil, queue: .main) { [weak self] _ in
                    self?.stop()
                }
            let timer = Timer(timeInterval: 60, repeats: true) { [weak self] _ in self?.recheck() }
            RunLoop.main.add(timer, forMode: .common)
            self.timer = timer
        }
    }

    func setEnabled(_ enabled: Bool) {
        requested = enabled
        recheck()
    }

    /// Revalidates actual assertion ownership after wake and reacquires missing
    /// assertions. Existing active assertions are never duplicated.
    func recheck() {
        var errors: [String] = []
        for kind in KeepAwakeAssertionKind.allCases {
            if let id = assertions[kind], !backend.isActive(id) {
                // A vanished or inactive assertion no longer keeps the machine up.
                // Release any surviving inactive record before replacing it.
                switch backend.release(id) {
                case .success: assertions.removeValue(forKey: kind)
                case .failure(let error):
                    errors.append("\(kind.label)：\(error.message)")
                    continue
                }
            }
            if requested {
                guard assertions[kind] == nil else { continue }
                switch backend.create(kind) {
                case .success(let id):
                    assertions[kind] = id
                    if !backend.isActive(id) { errors.append("\(kind.label)：创建后未能确认生效") }
                case .failure(let error): errors.append("\(kind.label)：\(error.message)")
                }
            } else if let id = assertions[kind] {
                switch backend.release(id) {
                case .success: assertions.removeValue(forKey: kind)
                case .failure(let error):
                    // Keep the ID for the next retry instead of leaking ownership.
                    errors.append("\(kind.label)：\(error.message)")
                }
            }
        }
        let next = KeepAwakeStatus(requested: requested,
                                   systemActive: assertions[.system].map(backend.isActive) ?? false,
                                   displayActive: assertions[.display].map(backend.isActive) ?? false,
                                   errors: errors)
        if next != status {
            status = next
            onStatusChange?(next)
        }
    }

    func stop() {
        setEnabled(false)
        timer?.invalidate()
        timer = nil
    }

    deinit {
        timer?.invalidate()
        if let wakeObserver { NSWorkspace.shared.notificationCenter.removeObserver(wakeObserver) }
        if let terminationObserver { NotificationCenter.default.removeObserver(terminationObserver) }
        for id in assertions.values { _ = backend.release(id) }
        // macOS releases any remaining process assertions when the process exits.
    }
}

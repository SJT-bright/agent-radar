import Foundation
@main struct PermissionStoreChecks {
    static func main() {
        var count = 0
        func check(_ ok: Bool, _ name: String) { precondition(ok, name); count += 1 }
        let suite = "local.agentradar.permission-tests." + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        defaults.set(true, forKey: "autoContinueOptIn.v2")
        let store = MonitorStore(defaults: defaults)
        store.beginPermissionRepair()
        check(store.permissionRepairActive && store.autoContinueEnabled, "repair pauses runtime while preserving preference")
        check(defaults.bool(forKey: "autoContinueOptIn.v2"), "repair does not alter persistent authorization")
        store.beginPermissionRepair()
        store.endPermissionRepair()
        check(!store.permissionRepairActive && store.autoContinueEnabled, "closing duplicate show restores once")
        store.helperPermissionResult = .helper("ready")
        store.windowPermissionResult = .window(granted: true, apps: 1, windows: 2)
        store.recordWindowPermissionRevocation()
        check(store.windowPermissionResult.state == .denied && store.accessibilityCheck == .failed, "background revocation clears prior green")
        check(store.helperPermissionResult.state == .ready && store.accessibilityCheckLabel == "未授权", "component trust cannot mask revoked window access")
        print("Permission Store: \(count) checks passed")
    }
}

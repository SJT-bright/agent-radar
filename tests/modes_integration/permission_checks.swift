import Foundation
@main struct PermissionChecks {
    static func main() {
        var count = 0
        func check(_ ok: Bool, _ name: String) { precondition(ok, name); count += 1 }
        let good = PermissionProbeResult.window(granted: true, apps: 2, windows: 3)
        let denied = PermissionProbeResult.window(granted: false, apps: 0, windows: 0)
        let empty = PermissionProbeResult.window(granted: true, apps: 2, windows: 0)
        let helper = PermissionProbeResult.helper("ready")
        check(good.state == .ready && good.detail.contains("3"), "requires actual window result")
        check(denied.state == .denied, "untrusted denied")
        check(empty.state == .unconfirmed, "trusted alone not enough")
        check(helper.state == .ready && helper.detail.contains("尚未验证"), "component trust not actual input claim")
        check(PermissionProbeResult.helper("permission_required").state == .denied, "permission distinct")
        for code in ["bridge_error", "bridge_timeout", "bridge_unavailable"] {
            check(PermissionProbeResult.helper(code).state == .unavailable, "component error not denial " + code)
        }
        check(PermissionProbeResult.helper("another_recovery").state == .unconfirmed, "busy unknown")
        check(PermissionProbeResult.combinedLabel(window: good, helper: helper) == "已生效", "both successful")
        check(PermissionProbeResult.combinedLabel(window: good, helper: .helper("bridge_error")) == "组件异常", "window success cannot hide component failure")
        check(PermissionProbeResult.combinedLabel(window: good, helper: .helper("permission_required")) == "输入未授权", "helper permission shown separately")
        check(PermissionProbeResult.combinedLabel(window: denied, helper: helper) == "未授权", "helper cannot greenwash native denial")
        check(PermissionProbeResult.combinedLabel(window: empty, helper: helper) == "未确认", "no windows remains unknown")
        check(PermissionProbeResult.combinedLabel(window: good, helper: .checking) == "查询中", "partial result not prematurely green")
        check(PermissionRestartPlan.arguments(waitingFor: []) == nil, "empty wait list rejected")
        check(PermissionRestartPlan.arguments(waitingFor: [0, 99]) == nil, "invalid pid rejected")
        check(PermissionRestartPlan.arguments(waitingFor: [-2]) == nil, "negative pid rejected")
        let args = PermissionRestartPlan.arguments(waitingFor: [99, 99, 101])!
        check(args.suffix(2) == ["99", "101"], "wait pids dedup")
        check(args[1].contains("/usr/bin/open -a '/Applications/任务雷达.app' --args --permission-recheck"), "restart fixed formal application")
        check(args[1].contains("no new instance started") && args[1].contains("exit 1"), "timeout prevents duplicates")
        check(args[1].contains("permission-restart.log"), "restart failure has local receipt")
        check(PermissionRestartPlan.shouldRestartInstance(path: PermissionRestartPlan.formalPath, pid: 3, currentPID: 4), "formal instance selected")
        check(PermissionRestartPlan.shouldRestartInstance(path: "/tmp/other.app", pid: 4, currentPID: 4), "own build copy exits for formal restart")
        check(!PermissionRestartPlan.shouldRestartInstance(path: "/tmp/other.app", pid: 3, currentPID: 4), "unrelated copy not silently restarted")
        print("Permission recovery: \(count) checks passed")
    }
}

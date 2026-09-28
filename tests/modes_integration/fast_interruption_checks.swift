import Foundation

private final class FastFailureBridge: ContinuationSending {
    var isRunning = false
    var requests: [[String: Any]] = []
    var times: [Double] = []
    let clock: () -> Double
    init(clock: @escaping () -> Double) { self.clock = clock }
    func cancel() {}
    func run(_ request: [String: Any], completion: @escaping (String, Bool, Double?) -> Void) {
        requests.append(request)
        times.append(clock())
        completion("sent_pending_confirmation", true, nil)
    }
}

private final class FastCompletedJudge: ContinuationSending {
    var isRunning = false
    func cancel() {}
    func run(_ request: [String: Any], completion: @escaping (String, Bool, Double?) -> Void) {
        completion("judged_complete", false, nil)
    }
}

@main struct FastInterruptionChecks {
    static func main() {
        var checks = 0
        func check(_ value: Bool, _ name: String) { precondition(value, name); checks += 1 }
        // Each scenario has an actual Controller send receipt, then a newer
        // terminal turn with no intervening running observation.
        for scenario in ["503", "user-stopped", "explicit-stop", "unknown-reason", "auth", "permission", "context",
                         "unknown-status", "window-only", "same-turn", "old-turn", "cancel-before", "cancel-queued",
                         "stale-before", "stale-queued", "disabled", "paused"] {
            var now = 10_000.0
            let bridge = FastFailureBridge(clock: { now })
            let defaults = UserDefaults(suiteName: "agentradar.fast-interruption.\(UUID().uuidString)")!
            let controller = ContinuationController(bridge: bridge, judgeBridge: FastCompletedJudge(), clock: { now }, countDefaults: defaults)
            var rules = PromptRules(); rules.completionMode = "rage"; rules.ragePrompt = "优化项目并验证"
            rules.interruptText = "不应被使用的自定义文本"
            controller.rulesProvider = { rules }
            controller.configure(enabled: true, paused: false)
            controller.setSupervisedIDs(["workbuddy:isolated"])
            var row = SessionRecord(id: "workbuddy:isolated", app_id: "workbuddy", app_name: "WorkBuddy",
                title: "Isolated fast interruption", project: "/isolated", status: "running", evidence: "明确轮次事件",
                updated_at: now, source: "local-session", target: "workbuddy://chat/isolated", started_at: now - 10, timing_basis: "turn")
            controller.observe([row], fresh: true)
            row.status = "completed"; controller.observe([row], fresh: true)
            now += 5; controller.tick()
            check(bridge.requests.count == 1, scenario + " initial optimization sent")
            let previousKey = ContinuationPolicy.key(row)
            let previousStart = row.started_at!
            let interruptedAt = now
            if scenario == "cancel-before" { controller.cancelCurrent() }
            if scenario == "disabled" { controller.configure(enabled: false, paused: false) }
            if scenario == "paused" { controller.configure(enabled: true, paused: true) }
            now += 1
            row.started_at = now; row.updated_at = now; row.status = "interrupted"
            row.status_reason = "服务端返回错误（HTTP 503）"
            switch scenario {
            case "user-stopped": row.user_stopped = true
            case "explicit-stop": row.status_reason = "用户主动停止"
            case "unknown-reason": row.status_reason = "应用记录了中止事件，未记录触发者"
            case "auth": row.status_reason = "认证失败"
            case "permission": row.status_reason = "访问权限不足"
            case "context": row.status_reason = "上下文超过模型限制"
            case "unknown-status": row.status = "unknown"
            case "window-only": row.source = "window"; row.timing_basis = "observed"
            case "same-turn": row.started_at = previousStart
            case "old-turn": row.started_at = previousStart - 1
            default: break
            }
            if scenario == "stale-before" {
                controller.observe([row], fresh: false); controller.tick()
            }
            controller.observe([row], fresh: true)
            if scenario == "503" {
                check(controller.pendingSessionIDs.contains(row.id), "immediate 503 is queued despite no running snapshot")
                check(bridge.requests.count == 1, "interruption preserves one-second settling")
            }
            if scenario == "cancel-queued" {
                controller.cancelCurrent(sessionID: row.id, key: ContinuationPolicy.key(row))
            }
            if scenario == "stale-queued" {
                now += 16; controller.tick()
                check(controller.pendingSessionIDs.isEmpty, "expired snapshot drops fast failure recovery")
            }
            for _ in 0..<20 {
                now += 5
                controller.observe([row], fresh: true)
                controller.tick()
            }
            if scenario == "503" {
                check(bridge.requests.count == 2, "one optimization and exactly one interrupted recovery")
                let recovery = bridge.requests[1]
                check(recovery["text"] as? String == rules.ragePrompt, "fast failure uses next ordinary cadence prompt")
                check(recovery["kind"] as? String == "interrupt" && recovery["automation_mode"] as? String == "rage", "fast failure preserves rage interrupt protocol")
                check(recovery["key"] as? String == ContinuationPolicy.key(row) && recovery["key"] as? String != previousKey, "recovery targets only the newer exact round")
                check((bridge.times.last ?? .infinity) - interruptedAt <= 10, "interruption recovery dispatches within ten seconds")
            } else {
                check(bridge.requests.count == 1, scenario + " must not resume")
            }
        }
        print("Fast interruption controller: \(checks) checks passed")
    }
}

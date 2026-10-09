import Foundation

private final class CountdownSender: ContinuationSending {
    var isRunning = false
    var requests: [[String: Any]] = []
    var completion: ((String, Bool, Double?) -> Void)?
    func cancel() { isRunning = false; completion = nil }
    func run(_ request: [String: Any], completion: @escaping (String, Bool, Double?) -> Void) {
        requests.append(request); isRunning = true; self.completion = completion
    }
    func finish(_ code: String, _ attempted: Bool, retryAfter: Double? = nil) {
        isRunning = false; let callback = completion; completion = nil; callback?(code, attempted, retryAfter)
    }
}

@main struct CountdownChecks {
    static func main() {
        var checks = 0
        func check(_ value: Bool, _ message: String) { precondition(value, message); checks += 1 }
        var now = 1000.0, idle = 0.0
        let sender = CountdownSender()
        let name = "agentradar.countdown." + UUID().uuidString
        let defaults = UserDefaults(suiteName: name)!
        defer { defaults.removePersistentDomain(forName: name) }
        let controller = ContinuationController(bridge: sender, clock: { now }, inputIdle: { idle }, countDefaults: defaults)
        var rules = PromptRules(); rules.completionMode = "rage"
        controller.rulesProvider = { rules }
        controller.configure(enabled: true, paused: false)
        controller.setSupervisedIDs(["workbuddy:countdown", "workbuddy:second"])
        var notices: [String: ContinuationNotice] = [:]
        controller.onNotice = { notices[$0.sessionID ?? ""] = $0 }
        var first = SessionRecord(id: "workbuddy:countdown", app_id: "workbuddy", app_name: "WorkBuddy",
            title: "Countdown test", project: "/isolated", status: "running", evidence: "", updated_at: now,
            source: "local-session", target: "workbuddy://chat/one", started_at: now - 10, timing_basis: "turn")
        var second = first; second.id = "workbuddy:second"; second.title = "Second test"; second.target = "workbuddy://chat/two"
        controller.observe([first, second], fresh: true)
        first.status = "interrupted"; first.status_reason = "HTTP 503 服务错误"
        controller.observe([first, second], fresh: true)
        check(notices[first.id]?.timing?.deadline == 1001, "one-second scheduled deadline")
        check(notices[first.id]?.timing?.label(at: now, idleSeconds: idle) == "1 秒后准备操作电脑", "scheduled countdown")
        check(notices[first.id]?.canRetry == false, "queued auto recovery cannot be manually duplicated")
        now += 1; controller.tick()
        check(sender.requests.isEmpty, "hardware input blocks even before starting helper")
        check(notices[first.id]?.timing?.phase == .inputIdle, "input wait phase")
        check(notices[first.id]?.timing?.label(at: now, idleSeconds: 0) == "键鼠静止倒计时：2 秒", "two seconds at movement")
        check(notices[first.id]?.timing?.label(at: now, idleSeconds: 1.1) == "键鼠静止倒计时：1 秒", "countdown decreases")
        idle = 1.99; controller.tick(); check(sender.requests.isEmpty, "never round readiness down")
        idle = 2; controller.tick()
        check(sender.requests.count == 1, "starts at actual two-second threshold")
        check(notices[first.id]?.timing?.phase == .operating, "actual operation start phase")
        second.status = "interrupted"; second.status_reason = "HTTP 503 服务错误"
        controller.observe([first, second], fresh: true)
        check(notices[second.id]?.timing?.phase == .queue, "busy queue has no invented start ETA")
        sender.finish("user_active", false); idle = 0
        check(notices[first.id]?.timing?.deadline == now + 1, "temporary block uses actual reschedule deadline")
        now += 1; controller.tick(); check(sender.requests.count == 1, "new input resets idle wait")
        idle = 2; controller.tick(); check(sender.requests.count == 2, "resumes same round after idle")
        sender.finish("sent_pending_confirmation", true)
        check(notices[first.id]?.timing?.phase == .confirming, "confirmation is distinct from pending operation")
        controller.tick()
        check(sender.requests.count == 3, "next independent session proceeds")
        sender.finish("user_active", true)
        check(notices[second.id]?.timing == nil, "after-write interruption has no false automatic countdown")
        check(notices[second.id]?.message.contains("不自动重试") == true, "after-write stop explained")
        for _ in 0..<5 { now += 1; controller.tick() }
        check(sender.requests.count == 3, "no duplicate after-write sends")
        for value in [Double.nan, Double.infinity, -1] { check(HardwareInputIdle.remaining(value) == 2, "invalid hardware sample fails closed") }
        let waiting = RecoveryTiming(phase: .scheduled, deadline: 2000)
        check(waiting.label(at: 1994.1, idleSeconds: 100) == "6 秒后准备操作电脑", "ceil real deadline")
        check(waiting.label(at: 1999.1, idleSeconds: 100) == "1 秒后准备操作电脑", "last scheduled second")
        check(waiting.label(at: 2000, idleSeconds: 0) == "键鼠静止倒计时：2 秒", "schedule alone cannot grant readiness")
        check(waiting.label(at: 2000, idleSeconds: 2).contains("准备核验"), "zero is check eligibility, not proof of send")
        controller.configure(enabled: false, paused: false)
        check(notices.values.allSatisfy { $0.timing == nil }, "disable removes pending countdowns")
        for code in ["rate-limit", "app_unavailable", "focus_changed", "cooldown", "composer_unreadable"] {
            var time = 5000.0, input = 2.0
            let bridge = CountdownSender()
            let c = ContinuationController(bridge: bridge, clock: { time }, inputIdle: { input }, countDefaults: defaults)
            var config = PromptRules(); config.completionMode = "rage"
            config.rateLimitWakeEnabled = true; config.rateLimitWaitSeconds = 600
            c.rulesProvider = { config }
            c.configure(enabled: true, paused: false)
            c.setSupervisedIDs([first.id])
            var notice: ContinuationNotice?
            c.onNotice = { notice = $0 }
            var row = first; row.status = "running"; row.started_at = time - 20
            c.observe([row], fresh: true)
            row.status = "interrupted"; row.status_reason = code == "rate-limit" ? "使用频率或配额超限（HTTP 429）" : "HTTP 503 服务错误"
            c.observe([row], fresh: true)
            if code == "rate-limit" {
                check(notice?.timing?.deadline == time + 10, "even unnormalized legacy 600 seconds capped at policy and scheduling")
                check(notice?.timing?.phase == .rateLimited, "rate-limit has explicit phase")
                check(notice?.message.contains("限流") == true, "rate-limit reason remains visible")
                check(notice?.timing?.label(at: time, idleSeconds: input).contains("10 秒") == true, "rate-limit shows real ten-second wait")
                time += 10; c.observe([row], fresh: true)
                check(bridge.requests.count == 1, "rate-limit dispatches at ten-second deadline")
            } else {
                time += 1; c.tick()
                bridge.finish(code, false, retryAfter: 367)
                if code == "cooldown" {
                    check(notice?.timing == nil && c.pendingSessionIDs.isEmpty, "long helper cooldown terminates instead of fabricating short wait or cycling")
                } else {
                    check(notice?.timing?.deadline == time + 10, "helper wait capped at ten seconds for " + code)
                    check(notice?.message.contains(ContinuationController.explain(code)) == true, "precise failure reason shown for " + code)
                    time += 10; input = 0; c.observe([row], fresh: true)
                    check(bridge.requests.count == 1, "user input still prevents retry for " + code)
                    input = 2; c.tick()
                    check(bridge.requests.count == 2, "fresh same-round retry after idle for " + code)
                    bridge.finish(code, true)
                    c.tick()
                    check(bridge.requests.count == 2 && notice?.timing == nil, "never retries after composer write for " + code)
                }
            }
            c.stop()
        }
        print("Recovery countdown: \(checks) checks passed")
    }
}

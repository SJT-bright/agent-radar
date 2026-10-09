import Foundation

final class MockContinuationBridge: ContinuationSending {
    var isRunning = false
    var requests: [[String: Any]] = []
    var results: [(String, Bool, Double?)] = []
    var pending: ((String, Bool, Double?) -> Void)?
    var hold = false
    func cancel() { isRunning = false }
    func run(_ request: [String: Any], completion: @escaping (String, Bool, Double?) -> Void) {
        requests.append(request)
        if hold { pending = completion; isRunning = true; return }
        let value = results.isEmpty ? ("sent_pending_confirmation", true, nil) : results.removeFirst()
        completion(value.0, value.1, value.2)
    }
}
final class CompletedAnswerJudge: ContinuationSending {
    var isRunning = false
    func cancel() {}
    func run(_ request: [String: Any], completion: @escaping (String, Bool, Double?) -> Void) {
        precondition(request["mode"] as? String == "judge")
        completion("judged_complete", false, nil)
    }
}
@main struct ModeControllerChecks {
    static func main() {
        var checks = 0
        func check(_ ok: Bool, _ name: String) { precondition(ok, name); checks += 1 }
        var now = 1000.0
        func row(_ id: String = "one", _ status: String = "running", start: Double = 990) -> SessionRecord {
            SessionRecord(id: "workbuddy:" + id, app_id: "workbuddy", app_name: "WorkBuddy", title: id,
                project: "/test", status: status, evidence: "HTTP 503", updated_at: now,
                source: "local-session", target: "workbuddy://chat/" + id, started_at: start, timing_basis: "turn")
        }
        func setup(_ enabled: Bool = true, rage: Bool = true) -> (ContinuationController, MockContinuationBridge) {
            let bridge = MockContinuationBridge()
            let defaults = UserDefaults(suiteName: "agentradar.mode-controller.\(UUID().uuidString)")!
            let controller = ContinuationController(bridge: bridge, judgeBridge: CompletedAnswerJudge(), clock: { now }, inputIdle: { 1000 }, countDefaults: defaults)
            var rules = PromptRules(); rules.completionMode = rage ? "rage" : "collaboration"; rules.ragePrompt = "optimize"
            controller.rulesProvider = { rules }
            controller.configure(enabled: enabled, paused: false)
            controller.setSupervisedIDs(Set(["workbuddy:one"] + (0..<12).map { "workbuddy:" + String($0) }))
            return (controller, bridge)
        }
        do {
            let (c, b) = setup(false, rage: false)
            var notices: [ContinuationNotice] = []; c.onNotice = { notices.append($0) }
            c.observe([row()], fresh: true); c.observe([row("one", "completed")], fresh: true)
            check(notices.last?.isCompletion == true && notices.last?.message == "这个项目已完结，请验收", "collaboration completion notice")
            check(b.requests.isEmpty, "collaboration no send")
            let stopped = row("one", "interrupted", start: 991)
            c.observe([stopped], fresh: true)
            c.requestManualRetry(sessionID: stopped.id, key: ContinuationPolicy.key(stopped))
            now += 3; c.tick()
            check(b.requests.count == 1 && b.requests[0]["manual_retry"] as? Bool == true, "manual works with auto off")
            check(b.requests[0]["text"] as? String == ContinuationPolicy.resumeText, "manual exact resume text")
            c.requestManualRetry(sessionID: stopped.id, key: ContinuationPolicy.key(stopped))
            check(b.requests.count == 1, "double click dedup")
            check(c.diagnostic.contains("自动发送：关闭"), "manual never toggles global")
        }
        do {
            let (c, b) = setup(false, rage: false)
            var notices: [ContinuationNotice] = []; c.onNotice = { notices.append($0) }
            let completed = row("manual-complete", "completed")
            c.observe([completed], fresh: true)
            let completionEvent = ContinuationEvent(session: completed, key: ContinuationPolicy.key(completed), detectedAt: now,
                                                    canContinue: false, explanation: "这个项目已完结，请验收", kind: "completion")
            // A completed reminder may explicitly continue even with automatic sending off.
            c.observe([row("manual-complete", "running")], fresh: true)
            c.observe([completed], fresh: true)
            check(notices.last?.isCompletion == true && notices.last?.canRetry == true, "completed reminder icon can continue")
            c.requestManualRetry(sessionID: completed.id, key: completionEvent.key)
            now += 3; c.tick()
            check(b.requests.count == 1 && b.requests[0]["kind"] as? String == "followup", "completed click routes as followup")
            check(b.requests[0]["text"] as? String == ContinuationPolicy.manualContinueText &&
                  b.requests[0]["manual_retry"] as? Bool == true, "completed click sends fixed continue once")
            c.requestManualRetry(sessionID: completed.id, key: completionEvent.key)
            check(b.requests.count == 1, "completed double click cannot duplicate")
        }
        do {
            let (c, b) = setup()
            c.observe([row("one", "completed")], fresh: true)
            now += 6; c.tick()
            check(b.requests.isEmpty, "startup history never auto sends")
            c.startRageForCompleted(); now += 5; c.tick()
            check(b.requests.count == 1, "explicit historical start")
            c.observe([row("one", "completed", start: now - 1)], fresh: true)
            now += 5; c.tick()
            check(b.requests.count == 2, "fast completed new round loops")
            check(b.requests[1]["automation_mode"] as? String == "rage", "bridge mode propagated")
        }
        do {
            let (c, b) = setup()
            c.observe([row()], fresh: true); c.observe([row("one", "completed")], fresh: true)
            c.configure(enabled: false, paused: false); now += 6; c.tick()
            check(b.requests.isEmpty && c.pendingSessionIDs.isEmpty, "disable cancels queue")
        }
        do {
            let (c, b) = setup()
            c.observe([row()], fresh: true); c.observe([row("one", "completed")], fresh: true)
            c.configure(enabled: true, paused: false); now += 6; c.tick()
            check(b.requests.isEmpty, "apply rules configure cancels old prompt")
        }
        do {
            let (c, b) = setup()
            let running = (0..<12).map { row(String($0)) }
            let completed = (0..<12).map { row(String($0), "completed") }
            c.observe(running, fresh: true); c.observe(completed, fresh: true)
            check(c.pendingSessionIDs.count == 12, "more than eight preserved")
            b.results = [("cooldown", false, 60), ("target_unverified", false, nil)]
            now += 5; c.tick()
            // A cooled-down session yields to another ready session without answer judgement.
            now += 5; c.observe(completed, fresh: true); c.tick()
            check(b.requests.count >= 2 && b.requests[0]["id"] as? String != b.requests[1]["id"] as? String, "cooldown yields to next session")
        }
        do {
            let (c, b) = setup(false)
            var r = row("one", "interrupted")
            c.observe([r], fresh: true); now += 16
            c.requestManualRetry(sessionID: r.id, key: ContinuationPolicy.key(r))
            check(b.requests.isEmpty, "manual stale blocked")
            c.observe([r], fresh: true)
            c.requestManualRetry(sessionID: r.id, key: "wrong")
            check(b.requests.isEmpty, "manual wrong round blocked")
            r.status = "running"; c.observe([r], fresh: true)
            c.requestManualRetry(sessionID: r.id, key: ContinuationPolicy.key(r))
            check(b.requests.isEmpty, "manual running blocked")
            r.status = "interrupted"; r.user_stopped = true; c.observe([r], fresh: true)
            c.requestManualRetry(sessionID: r.id, key: ContinuationPolicy.key(r))
            check(b.requests.isEmpty, "manual user stopped blocked")
            r.user_stopped = false; r.source = "window"; c.observe([r], fresh: true)
            c.requestManualRetry(sessionID: r.id, key: ContinuationPolicy.key(r))
            check(b.requests.isEmpty, "manual window evidence blocked")
            r.source = "local-session"; var duplicate = r; duplicate.id = "other"; c.observe([r, duplicate], fresh: true)
            c.requestManualRetry(sessionID: r.id, key: ContinuationPolicy.key(r))
            check(b.requests.isEmpty, "duplicate title blocked")
        }
        do {
            let (c, b) = setup(false)
            b.results = [("permission_required", false, nil)]
            let r = row("one", "interrupted"); c.observe([r], fresh: true)
            var notices: [ContinuationNotice] = []; c.onNotice = { notices.append($0) }
            c.requestManualRetry(sessionID: r.id, key: ContinuationPolicy.key(r))
            now += 3; c.tick()
            check(notices.last?.canRetry == true, "unattempted permission failure retryable")
            c.requestManualRetry(sessionID: r.id, key: ContinuationPolicy.key(r))
            now += 3; c.tick()
            check(b.requests.count == 2, "permission retry click accepted")
        }
        do {
            let (c, b) = setup(false); b.hold = true
            let r = row("one", "interrupted"); c.observe([r], fresh: true)
            c.requestManualRetry(sessionID: r.id, key: ContinuationPolicy.key(r))
            now += 3; c.tick()
            c.cancelCurrent()
            b.pending?("sent_pending_confirmation", true, nil)
            c.requestManualRetry(sessionID: r.id, key: ContinuationPolicy.key(r))
            check(b.requests.count == 1 && c.pendingSessionIDs.isEmpty, "cancel uncertain write fails closed and stale callback ignored")
        }
        do {
            let (c, b) = setup()
            c.observe([row()], fresh: true); c.observe([row("one", "completed")], fresh: true)
            c.exclude([row().id]); now += 6; c.tick()
            check(b.requests.isEmpty, "remove cancels queue")
        }
        do {
            let (c, b) = setup(false)
            b.results = [("user_active", false, nil), ("sent_pending_confirmation", true, nil)]
            let r = row("click", "interrupted"); c.observe([r], fresh: true)
            var notices: [ContinuationNotice] = []; c.onNotice = { notices.append($0) }
            c.requestManualRetry(sessionID: r.id, key: ContinuationPolicy.key(r))
            c.requestManualRetry(sessionID: r.id, key: ContinuationPolicy.key(r))
            check(b.requests.isEmpty && notices.last?.isRecovering == true, "click allows idle settling and disables double click")
            now += 3; c.tick()
            check(b.requests.count == 1, "manual first idle check")
            now += 5; c.tick()
            check(b.requests.count == 2, "user active transient retried without another click")
        }
        do {
            let (c, b) = setup(false)
            let r = row("click-cancel", "interrupted"); c.observe([r], fresh: true)
            c.requestManualRetry(sessionID: r.id, key: ContinuationPolicy.key(r))
            c.cancelCurrent(sessionID: "other", key: "old")
            check(c.pendingSessionIDs.contains(r.id), "old notice cannot cancel another task")
            c.cancelCurrent(sessionID: r.id, key: ContinuationPolicy.key(r))
            now += 3; c.tick()
            check(b.requests.isEmpty && c.pendingSessionIDs.isEmpty, "identity cancellation removes queued manual")
        }
        for cancellation in ["stale", "pause", "exclude", "rules"] {
            let (c, b) = setup(false)
            let r = row("queued-cancel", "interrupted"); c.observe([r], fresh: true)
            var notices: [ContinuationNotice] = []; c.onNotice = { notices.append($0) }
            c.requestManualRetry(sessionID: r.id, key: ContinuationPolicy.key(r))
            if cancellation == "stale" { now += 16; c.tick() }
            else if cancellation == "pause" { c.configure(enabled: false, paused: true) }
            else if cancellation == "exclude" { c.exclude([r.id]) }
            else { c.configure(enabled: false, paused: false) }
            check(b.requests.isEmpty && c.pendingSessionIDs.isEmpty && notices.last?.isRecovering == false
                && notices.last?.cancellable == false, "queued manual exits busy on " + cancellation)
        }
        do {
            // An unconfirmed send must never block a different interrupted chat.
            let (c, b) = setup()
            let running = [row("0"), row("1"), row("2")]
            c.observe(running, fresh: true)
            let stopped = [row("0", "interrupted"), row("1", "interrupted"), row("2", "interrupted")]
            c.observe(stopped, fresh: true)
            for _ in 0..<3 { now += 1; c.tick() }
            check(b.requests.count == 3, "three interruptions dispatch within three seconds without confirmation blocking")
            check(c.pendingSessionIDs.count == 3, "three sent rounds confirm independently")
            var refreshed = stopped
            refreshed[0] = row("0", "running", start: now)
            c.observe(refreshed, fresh: true)
            check(c.pendingSessionIDs == ["workbuddy:1", "workbuddy:2"], "new-round receipt clears only its own confirmation")
            c.cancelCurrent(sessionID: "workbuddy:1", key: ContinuationPolicy.key(stopped[1]))
            check(c.pendingSessionIDs == ["workbuddy:2"], "targeted cancel preserves other confirmations")
            c.configure(enabled: false, paused: false)
            check(c.pendingSessionIDs.isEmpty && b.requests.count == 3, "configuration clears pending confirmations without resending")
        }
        do {
            let (c, b) = setup()
            c.observe([row("0"), row("1")], fresh: true)
            c.observe([row("0", "completed"), row("1")], fresh: true)
            // Completion optimization has already spent five seconds in its queue.
            now += 4
            c.observe([row("0", "completed"), row("1", "interrupted")], fresh: true)
            now += 1; c.tick()
            check(b.requests.count == 1 && b.requests[0]["id"] as? String == "workbuddy:1", "ready interruption precedes older completed optimization")
            now += 1; c.tick()
            check(b.requests.count == 2 && b.requests[1]["id"] as? String == "workbuddy:0", "optimization follows without waiting for interruption receipt")
        }
        do {
            let (c, b) = setup()
            b.results = [("focus_changed", false, nil), ("sent_pending_confirmation", true, nil)]
            c.observe([row()], fresh: true); c.observe([row("one", "interrupted")], fresh: true)
            now += 1; c.tick(); now += 1; c.tick()
            check(b.requests.count == 2, "unattempted rage interruption transient rechecks after one second")
            now += 1; c.tick()
            check(b.requests.count == 2, "sent rage interruption cannot resend while awaiting receipt")
        }
        do {
            let (c, b) = setup()
            c.observe([row()], fresh: true); c.observe([row("one", "interrupted")], fresh: true)
            now += 1; c.tick()
            c.setSupervisedIDs([])
            check(c.pendingSessionIDs.isEmpty && b.requests.count == 1, "unmark clears detached confirmation")
        }
        do {
            let (c, b) = setup(); b.hold = true
            var notices: [ContinuationNotice] = []; c.onNotice = { notices.append($0) }
            c.observe([row()], fresh: true); c.observe([row("one", "interrupted")], fresh: true)
            now += 1; c.tick(); now += 10; c.tick()
            check(notices.last?.message.contains("超过 10 秒") == true, "overdue bridge displays truthful pending status")
            let count = notices.count
            now += 1; c.tick()
            check(notices.count == count && b.requests.count == 1, "overdue status emits once without another send")
            c.cancelCurrent()
        }
        do {
            let (c, b) = setup(); b.hold = true
            c.observe([row()], fresh: true); c.observe([row("one", "interrupted")], fresh: true)
            now += 1; c.tick()
            check(b.isRunning, "bridge is in flight before collector loss")
            c.observe([row("one", "interrupted")], fresh: false)
            check(!b.isRunning && c.pendingSessionIDs.isEmpty, "collector loss cancels in-flight input immediately")
            b.pending?("sent_pending_confirmation", true, nil)
            check(c.pendingSessionIDs.isEmpty, "stale bridge receipt cannot resurrect cancelled work")
        }
        print("Mode controller: \(checks) checks passed")
    }
}

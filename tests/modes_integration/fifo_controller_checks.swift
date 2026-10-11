import Foundation

private final class FIFOSender: ContinuationSending {
    var isRunning = false
    var requests: [[String: Any]] = []
    var callback: ((String, Bool, Double?) -> Void)?
    func cancel() { isRunning = false; callback = nil }
    func run(_ request: [String: Any], completion: @escaping (String, Bool, Double?) -> Void) {
        precondition(!isRunning, "desktop requests must never overlap")
        requests.append(request); isRunning = true; callback = completion
    }
    func finish(_ code: String, attempted: Bool = false) {
        isRunning = false; let done = callback; callback = nil; done?(code, attempted, nil)
    }
    var ids: [String] { requests.compactMap { $0["id"] as? String } }
}

@main struct FIFOControllerChecks {
    static func main() {
        var checks = 0
        func check(_ value: Bool, _ message: String) { precondition(value, message); checks += 1 }
        let suite = "agentradar.fifo." + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        var now = 1000.0
        let sender = FIFOSender()
        let controller = ContinuationController(bridge: sender, clock: { now }, inputIdle: { 1000 }, countDefaults: defaults)
        var rules = PromptRules(); rules.completionMode = "rage"
        controller.rulesProvider = { rules }
        controller.configure(enabled: true, paused: false)
        func row(_ id: String) -> SessionRecord {
            SessionRecord(id: "workbuddy:" + id, app_id: "workbuddy", app_name: "WorkBuddy",
                title: id, project: "/isolated/" + id, status: "running", evidence: "明确轮次事件",
                updated_at: now, source: "local-session", target: "workbuddy://chat/" + id,
                started_at: now - 10, timing_basis: "turn")
        }
        var a = row("a"), b = row("b"), c = row("c")
        controller.setSupervisedIDs([a.id, b.id])
        var notices: [String: ContinuationNotice] = [:]
        controller.onNotice = { notices[$0.sessionID ?? ""] = $0 }
        func observe() { controller.observe([a, b, c], fresh: true) }
        observe()
        a.status = "completed"; observe() // First item settles for five seconds.
        b.status = "interrupted"; b.status_reason = "HTTP 503 服务错误"; observe()
        c.status = "completed"; observe()
        controller.requestManualRetry(sessionID: c.id, key: ContinuationPolicy.key(c))
        now += 3; observe()
        check(sender.ids.isEmpty, "ready interruption/manual requests cannot pass a delayed FIFO head")
        check(notices[b.id]?.timing?.phase == .queue && notices[c.id]?.timing?.phase == .queue,
              "later items show queue wait rather than an invented independent countdown")
        controller.setDesktopNavigationBusy(true)
        now += 2; observe()
        check(sender.ids.isEmpty, "explicit window activation shares the desktop lane")
        controller.setDesktopNavigationBusy(false); controller.tick()
        check(sender.ids == [a.id], "first arrival owns the desktop")
        sender.finish("focus_changed")
        observe()
        check(sender.ids == [a.id], "temporary failure waits its actual retry deadline")
        now += 5; observe()
        check(sender.ids == [a.id, a.id], "head retry keeps its place before other conversations")
        sender.finish("sent_pending_confirmation", attempted: true)
        for _ in 0..<29 { now += 1; observe() }
        check(sender.ids == [a.id, a.id], "no other navigation during the first response confirmation")
        check(notices[b.id]?.timing?.phase == .queue, "confirmation still owns the desktop lane")
        now += 1; observe()
        check(sender.ids == [a.id, a.id, b.id], "bounded confirmation timeout advances exactly one item")
        check(notices[a.id]?.message.contains("30 秒") == true, "timeout is explicit, never a success claim")
        sender.finish("target_unverified")
        now += 1; observe()
        check(sender.ids == [a.id, a.id, b.id, b.id], "route retry also keeps its FIFO position")
        sender.finish("sent_pending_confirmation", attempted: true)
        now += 1; b.status = "running"; b.started_at = now; observe()
        check(sender.ids.last == c.id && sender.ids.count == 5, "verified new round releases lane for the manual item")
        check(notices[c.id]?.timing?.phase == .operating, "manual item survives more than 15 seconds of queue wait")
        sender.finish("user_active", attempted: true)
        for _ in 0..<3 { now += 1; observe() }
        check(sender.ids.count == 5, "written or sent items never get replayed after failure")
        a.status = "running"; a.started_at = now; observe()
        check(notices[a.id]?.message.contains("发送已确认") == true, "late confirmation remains observable without resending")

        // A manual click upgrades an existing queued item in place; cancellation
        // removes only its slot, and a stale snapshot cancels the remaining work.
        a.status = "completed"; observe()
        b.status = "interrupted"; b.status_reason = "HTTP 503 服务错误"; observe()
        controller.requestManualRetry(sessionID: a.id, key: ContinuationPolicy.key(a))
        now += 1; observe()
        check(sender.ids.count == 5, "manual conversion preserves the earlier head and its delay")
        controller.cancelCurrent(sessionID: a.id, key: ContinuationPolicy.key(a))
        controller.tick()
        check(sender.ids.count == 6 && sender.ids.last == b.id, "cancelled head allows the next slot to proceed")
        sender.finish("sent_pending_confirmation", attempted: true)
        controller.observe([a, b, c], fresh: false)
        check(controller.pendingSessionIDs.isEmpty, "stale evidence releases confirmation and cancels pending work")
        controller.stop()
        print("FIFO desktop queue: \(checks) checks passed")
    }
}

import Foundation

private final class SupervisionClock { var now = 10_000.0 }
private final class SupervisionRules { var value = PromptRules() }
private final class SupervisionSender: ContinuationSending {
    var isRunning = false
    var requests: [[String: Any]] = []
    var responses: [(String, Bool)] = []
    func cancel() { isRunning = false }
    func run(_ request: [String: Any], completion: @escaping (String, Bool, Double?) -> Void) {
        requests.append(request)
        let response = responses.isEmpty ? ("sent_pending_confirmation", true) : responses.removeFirst()
        completion(response.0, response.1, nil)
    }
}
private final class SupervisionJudge: ContinuationSending {
    var isRunning = false
    var requests: [[String: Any]] = []
    func cancel() { isRunning = false }
    func run(_ request: [String: Any], completion: @escaping (String, Bool, Double?) -> Void) {
        requests.append(request)
        completion("judgement_unknown", false, nil)
    }
}
private final class SupervisionHarness {
    let clock = SupervisionClock()
    let rules = SupervisionRules()
    let sender = SupervisionSender()
    let judge = SupervisionJudge()
    let controller: ContinuationController
    var notices: [ContinuationNotice] = []
    init(marked: Set<String> = ["workbuddy:a"], mode: String = "rage", enabled: Bool = true,
         defaults: UserDefaults) {
        let c = clock, s = sender, j = judge, r = rules
        controller = ContinuationController(bridge: s, judgeBridge: j, clock: { c.now }, inputIdle: { 1000 }, countDefaults: defaults)
        r.value.completionMode = mode
        r.value.ragePrompt = "普通：继续优化"
        r.value.rageAlternateEvery = 3
        r.value.rageAlternatePrompt = "特别：从使用者角度检查并优化"
        controller.rulesProvider = { r.value }
        controller.configure(enabled: enabled, paused: false)
        controller.setSupervisedIDs(marked)
        controller.onNotice = { [weak self] in self?.notices.append($0) }
    }
    func row(_ id: String = "a", status: String = "running", start: Double = 9990,
             app: String = "workbuddy") -> SessionRecord {
        SessionRecord(id: app + ":" + id, app_id: app, app_name: app, title: "Project " + id,
            project: "/isolated/" + id, status: status, evidence: "服务端返回错误（HTTP 503）", updated_at: clock.now,
            source: "local-session", target: app + "://chat/" + id, started_at: start, timing_basis: "turn")
    }
    func finish(_ id: String = "a", start: Double = 9990, status: String = "completed", app: String = "workbuddy") {
        controller.observe([row(id, start: start, app: app)], fresh: true)
        controller.observe([row(id, status: status, start: start, app: app)], fresh: true)
    }
    func advance(_ seconds: Double = 5) { clock.now += seconds; controller.tick() }
}

@main struct SupervisionControllerChecks {
    static func main() {
        var checks = 0
        func check(_ ok: Bool, _ name: String) { precondition(ok, name); checks += 1 }
        var suites: [String] = []
        func freshDefaults() -> UserDefaults {
            let suite = "local.agentradar.controller-tests." + UUID().uuidString
            suites.append(suite)
            return UserDefaults(suiteName: suite)!
        }
        defer { for suite in suites { UserDefaults(suiteName: suite)?.removePersistentDomain(forName: suite) } }
        func harness(marked: Set<String> = ["workbuddy:a"], mode: String = "rage", enabled: Bool = true,
                     defaults: UserDefaults? = nil) -> SupervisionHarness {
            SupervisionHarness(marked: marked, mode: mode, enabled: enabled, defaults: defaults ?? freshDefaults())
        }
        do {
            let h = harness(marked: [])
            h.finish(); h.advance()
            check(h.judge.requests.isEmpty && h.sender.requests.isEmpty, "unmarked completion is read-only")
            h.finish(start: 10006, status: "interrupted"); h.advance()
            check(h.sender.requests.isEmpty, "unmarked interruption is read-only")
        }
        do {
            let h = harness(marked: ["zcode:a"])
            h.clock.now = 10002
            var ended = h.row(status: "completed", app: "zcode")
            ended.ended_at = 10001
            h.controller.observe([ended], fresh: true)
            h.advance()
            check(h.sender.requests.count == 1, "fresh ZCode lifecycle end after monitor start survives a missed running snapshot")
            h.controller.observe([ended], fresh: true); h.advance()
            check(h.sender.requests.count == 1, "fresh terminal fallback sends only once")
            let old = harness(marked: ["zcode:a"])
            var historical = old.row(status: "completed", app: "zcode")
            historical.ended_at = 9999
            old.controller.observe([historical], fresh: true); old.advance()
            check(old.sender.requests.isEmpty, "terminal round before monitoring began remains historical")
        }
        do {
            let h = harness()
            h.controller.observe([h.row()], fresh: true)
            h.controller.setSupervisedIDs(["workbuddy:a"])
            check(h.sender.requests.isEmpty, "marking a running conversation only arms a baseline")
            h.controller.observe([h.row(status: "completed")], fresh: true)
            check(h.judge.requests.isEmpty, "fresh completed round never calls answer judge")
            h.advance()
            check(h.sender.requests.count == 1 && h.sender.requests[0]["text"] as? String == h.rules.value.ragePrompt,
                  "first fresh completion sends ordinary configured prompt")
            let request = h.sender.requests[0]
            check(request["id"] as? String == "workbuddy:a" && request["project"] as? String == "/isolated/a"
                && request["started_at"] as? Double == 9990 && request["source"] as? String == "local-session",
                  "send retains exact session, turn and project provenance")
            h.controller.observe([h.row(status: "completed")], fresh: true); h.advance()
            check(h.sender.requests.count == 1, "same terminal round is never sent twice")
        }
        do {
            let h = harness()
            h.controller.observe([h.row()], fresh: true)
            h.controller.setSupervisedIDs([], temporarilyUnavailable: ["workbuddy:a"])
            h.controller.observe([h.row(status: "unknown")], fresh: true)
            h.controller.setSupervisedIDs(["workbuddy:a"])
            h.controller.observe([h.row(status: "completed")], fresh: true)
            h.advance()
            check(h.sender.requests.count == 1, "provider scope survives brief unknown snapshot")
        }
        do {
            let h = harness()
            h.controller.observe([h.row()], fresh: true)
            h.controller.setSupervisedIDs([])
            h.controller.observe([h.row(status: "completed")], fresh: true)
            h.advance()
            check(h.sender.requests.isEmpty, "explicit provider unmark forgets running baseline")
        }
        do {
            let h = harness(marked: ["grok:new"])
            h.finish("new", app: "grok"); h.advance()
            check(h.sender.requests.first?["text"] as? String == h.rules.value.ragePrompt(forApp: "grok"),
                  "Grok fresh completion uses its ordinary provider prompt")
            check(h.judge.requests.isEmpty, "Grok completion does not depend on judgement")
        }
        do {
            let h = harness()
            h.controller.observe([h.row(status: "completed"), h.row("b", status: "completed")], fresh: true)
            h.advance()
            check(h.sender.requests.isEmpty, "historical completed rows stay inert")
            h.controller.startRageForCompleted(); h.advance()
            check(h.sender.requests.count == 1 && h.sender.requests[0]["id"] as? String == "workbuddy:a",
                  "explicit start only sends to marked historical row")
            h.controller.startRageForCompleted(); h.advance()
            check(h.sender.requests.count == 1, "historical start does not duplicate same round")
        }
        do {
            let h = harness()
            for (index, status) in ["completed", "interrupted", "completed", "interrupted", "completed", "completed"].enumerated() {
                h.finish(start: 9990 + Double(index) * 10, status: status)
                h.advance(status == "interrupted" ? 1 : 5)
            }
            let texts = h.sender.requests.compactMap { $0["text"] as? String }
            check(texts.count == 6, "each verified terminal round sends once")
            // 中断恢复使用按软件恢复提示词（默认 resumeText），不占用续接轮换；
            // 特别提示词节奏只由完成续接推进。
            let resume = ContinuationPolicy.resumeText(forApp: "workbuddy",
                                                       overrides: h.rules.value.appResumeTexts,
                                                       global: h.rules.value.resumePrompt)
            check(texts == [h.rules.value.ragePrompt, resume, h.rules.value.rageAlternatePrompt,
                            resume, h.rules.value.ragePrompt, h.rules.value.rageAlternatePrompt],
                  "interruptions send per-app resume text; completions keep cadence: \(texts)")
            check(h.judge.requests.isEmpty, "mixed cadence never requests answer judgement")
        }
        do {
            let h = harness(marked: ["workbuddy:b"])
            h.rules.value.rageAlternateEvery = 2
            h.finish("b", start: 9990); h.advance()
            h.finish("b", start: 10000); h.advance()
            check(h.sender.requests.compactMap { $0["text"] as? String } ==
                  [h.rules.value.ragePrompt, h.rules.value.rageAlternatePrompt],
                  "user-configured interval changes special prompt placement")
        }
        do {
            let h = harness(marked: ["workbuddy:c"])
            h.rules.value.rageAlternateEvery = 2
            // Keep one completed round fresh while its two pre-input failures
            // retry. Only the successful third operation advances cadence.
            h.sender.responses = [("target_unverified", false), ("target_unverified", false),
                                  ("sent_pending_confirmation", true)]
            h.finish("c"); h.advance(); h.advance(); h.advance()
            check(h.sender.requests.compactMap { $0["text"] as? String } ==
                  [h.rules.value.ragePrompt, h.rules.value.ragePrompt, h.rules.value.ragePrompt],
                  "failed route attempts do not advance cadence")
            h.finish("c", start: 10020); h.advance()
            check(h.sender.requests.last?["text"] as? String == h.rules.value.rageAlternatePrompt,
                  "next successful round follows the configured cadence")
        }
        for code in ["search_result_missing", "header_unverified", "sidebar_target_missing", "tree_incomplete"] {
            let h = harness()
            h.sender.responses = [(code, false), ("sent_pending_confirmation", true)]
            h.finish(); h.advance(); h.advance()
            check(h.sender.requests.count == 2, "pre-input transient \(code) retries the same fresh round")
        }
        for code in ["session_title_ambiguous", "project_name_ambiguous", "search_result_ambiguous", "search_input_changed"] {
            let h = harness()
            h.sender.responses = [(code, false)]
            h.finish(); h.advance(); h.advance()
            check(h.sender.requests.count == 1, "ambiguous or changed target \(code) cannot retry")
        }
        do {
            let shared = freshDefaults()
            let first = harness(marked: ["workbuddy:restart"], defaults: shared)
            first.rules.value.rageAlternateEvery = 2
            first.finish("restart"); first.advance()
            check(first.sender.requests.count == 1, "first controller records successful send")
            let second = harness(marked: ["workbuddy:restart"], defaults: shared)
            second.rules.value.rageAlternateEvery = 2
            second.finish("restart", start: 10001); second.advance()
            check(second.sender.requests.first?["text"] as? String == second.rules.value.rageAlternatePrompt,
                  "restart restores per-session successful-click count")
        }
        do {
            let h = harness(marked: ["workbuddy:a", "workbuddy:b"])
            h.finish("a"); h.advance()
            h.controller.observe([h.row("a", start: h.clock.now)], fresh: true)
            h.finish("b", start: 10001); h.advance()
            check(h.sender.requests.count == 2 && h.sender.requests.allSatisfy { $0["text"] as? String == h.rules.value.ragePrompt },
                  "each conversation has an independent cadence counter")
        }
        for invalidation in ["unmark", "pause", "disable", "mode", "cancel", "exclude", "stale", "waiting",
                             "running", "new-turn", "user-stopped", "window-only", "project-changed", "duplicate-id"] {
            let h = harness(); h.finish()
            var row = h.row(status: "completed")
            switch invalidation {
            case "unmark": h.controller.setSupervisedIDs([])
            case "pause": h.controller.configure(enabled: true, paused: true)
            case "disable": h.controller.configure(enabled: false, paused: false)
            case "mode": h.rules.value.completionMode = "collaboration"; h.controller.configure(enabled: true, paused: false)
            case "cancel": h.controller.cancelCurrent(sessionID: row.id, key: ContinuationPolicy.key(row))
            case "exclude": h.controller.exclude([row.id])
            case "stale": h.clock.now += 16
            case "waiting": row.status = "waiting"; h.controller.observe([row], fresh: true)
            case "running": row.status = "running"; h.controller.observe([row], fresh: true)
            case "new-turn": row.started_at = 10001; h.controller.observe([row], fresh: true)
            case "user-stopped": row.user_stopped = true; h.controller.observe([row], fresh: true)
            case "window-only": row.source = "window"; h.controller.observe([row], fresh: true)
            case "project-changed": row.project = "/another"; h.controller.observe([row], fresh: true)
            case "duplicate-id": h.controller.observe([row, row], fresh: true)
            default: break
            }
            h.advance()
            check(h.sender.requests.isEmpty, "queued send cannot cross " + invalidation)
        }
        do {
            let h = harness(marked: [], mode: "collaboration")
            h.finish(); h.advance()
            check(h.sender.requests.isEmpty && h.notices.last?.message == "这个项目已完结，请验收",
                  "collaboration completion remains notice-only")
            h.finish(start: 10006, status: "interrupted"); h.advance()
            check(h.sender.requests.count == 1, "collaboration opted-in interruption remains independent of marks")
        }
        do {
            let h = harness(marked: [], enabled: false)
            let row = h.row(status: "interrupted")
            h.controller.observe([row], fresh: true)
            h.controller.requestManualRetry(sessionID: row.id, key: ContinuationPolicy.key(row)); h.advance(3)
            check(h.sender.requests.count == 1 && h.judge.requests.isEmpty,
                  "manual one-shot still works without marks or global automation")
        }
        do {
            // Twelve simulated hours across three independently supervised conversations.
            let ids = ["a", "b", "c"]
            let h = harness(marked: Set(ids.map { "workbuddy:" + $0 }))
            for turn in 0..<720 {
                let start = h.clock.now
                for _ in 0..<5 {
                    h.controller.observe(ids.map { h.row($0, start: start) }, fresh: true)
                    h.clock.now += 10
                }
                let interrupted = turn % 7 == 0
                var rows = ids.map { h.row($0, status: interrupted ? "interrupted" : "completed", start: start) }
                h.controller.observe(rows, fresh: true)
                for _ in 0..<10 {
                    h.clock.now += 1; h.controller.observe(rows, fresh: true)
                    // Simulate an actual same-session acknowledgement for each
                    // click. The next desktop request must await this evidence.
                    for request in h.sender.requests.dropFirst(turn * ids.count) {
                        if let index = rows.firstIndex(where: { $0.id == request["id"] as? String }), rows[index].status != "running" {
                            rows[index].status = "running"
                            rows[index].started_at = h.clock.now
                        }
                    }
                }
                check(h.sender.requests.count == (turn + 1) * ids.count,
                      "all marked sessions continue at turn \(turn)")
            }
            check(h.clock.now == 53_200, "twelve simulated hours elapsed")
            let keys = h.sender.requests.compactMap { $0["key"] as? String }
            check(Set(keys).count == 2160, "2160 sends have unique session-round keys")
            check(h.controller.pendingSessionIDs.isEmpty, "all simulated response receipts were confirmed in order")
            check(h.sender.requests.allSatisfy { $0["project"] as? String != nil },
                  "every send carries project provenance")
            // 中断轮使用按软件恢复提示词；所有成功自动发送参与次数周期。
            // 完成轮按该会话累计发送次数选择普通或特别提示词。
            let resume = ContinuationPolicy.resumeText(forApp: "workbuddy",
                                                       overrides: h.rules.value.appResumeTexts,
                                                       global: h.rules.value.resumePrompt)
            for id in ids {
                let texts = h.sender.requests.filter { $0["id"] as? String == "workbuddy:" + id }
                    .compactMap { $0["text"] as? String }
                check(texts.count == 720, "twelve hours send every round for each session")
                for (index, text) in texts.enumerated() {
                    let interruptedRound = index % 7 == 0
                    if interruptedRound {
                        check(text == resume, "interrupt round uses per-app resume text at \(index)")
                    } else {
                        let expected = index % 3 == 2 ? h.rules.value.rageAlternatePrompt : h.rules.value.ragePrompt
                        check(text == expected, "completion round cadence at \(index): got \(text)")
                    }
                }
            }
        }
        print("Selected supervision controller: \(checks) checks passed")
    }
}

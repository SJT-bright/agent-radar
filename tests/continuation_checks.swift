import Foundation

@main struct ContinuationChecks {
    static func main() {
        var count = 0
        func check(_ ok: Bool, _ name: String) { precondition(ok, name); count += 1 }
        var row = SessionRecord(id: "workbuddy:one", app_id: "workbuddy", app_name: "WorkBuddy", title: "Test",
            project: "/test", status: "running", evidence: "HTTP 503", updated_at: 100,
            source: "local-session", target: "workbuddy://chat/one", started_at: 90, timing_basis: "turn")
        func transition(_ state: String, mode: String = "collaboration", configure: (inout SessionRecord) -> Void = { _ in }) -> [ContinuationEvent] {
            var p = ContinuationPolicy(), r = row
            configure(&r); r.status = "running"; _ = p.observe([r], now: 100)
            r.status = state
            return p.observe([r], now: 101, interruptText: "custom", rateLimitAllowed: true,
                rateLimitText: "custom rate", followUps: [r.id: "legacy"], completionMode: mode, ragePrompt: " optimize ", supervisedIDs: [r.id])
        }
        var p = ContinuationPolicy()
        row.status = "completed"
        check(p.observe([row], now: 100, completionMode: "rage", ragePrompt: "x").isEmpty, "startup completed inert")
        row.status = "interrupted"
        check(p.observe([row], now: 100).isEmpty, "startup interrupted inert")
        row.status = "running"
        let collaboration = transition("completed")
        check(collaboration.count == 1 && !collaboration[0].canContinue && collaboration[0].kind == "completion", "collaboration notices only")
        check(collaboration[0].explanation == "这个项目已完结，请验收", "acceptance exact copy")
        let rage = transition("completed", mode: "rage")
        check(rage.count == 1 && rage[0].canContinue && rage[0].kind == "followup" && rage[0].text.isEmpty, "rage completion queues a followup without content judgement")
        check(rage[0].automationMode == "rage" && rage[0].delay == 5, "rage metadata")
        for state in ["idle", "unknown", "waiting"] {
            check(transition(state).isEmpty, "no false completion " + state)
            check(transition(state, mode: "rage").isEmpty, "no false rage " + state)
        }
        check(transition("interrupted").first?.text == "custom", "legacy interruption prompt preserved")
        check(transition("interrupted", mode: "rage").first?.text == ContinuationPolicy.resumeText, "rage interruption fixed")
        let rate = transition("interrupted", mode: "rage") { $0.status_reason = "配额 HTTP 429" }
        check(rate.first?.canContinue == true && rate.first?.text == ContinuationPolicy.resumeText, "rage rate fixed")
        check(rate[0].delay == 300 && rate[0].bypassRestriction, "rate waits")
        // 狂暴中断首次武装只等待 1 秒；不以跨新轮冷却阻塞恢复。
        check((transition("interrupted", mode: "rage").first?.delay ?? 999) == 1, "rage interruption arms after one second")
        // 长轮次中断（如 HTTP 502）在真实轮次起点下必须可自动恢复。
        let network = transition("interrupted") { $0.status_reason = "网络连接失败或断开（HTTP 502）" }
        check(network.first?.canContinue == true && network.first?.text == "custom", "network 502 interruption recovers")
        check(transition("interrupted", mode: "rage") { $0.status_reason = "网络连接失败或断开（HTTP 502）" }.first?.canContinue == true,
              "rage network 502 interruption recovers")
        for reason in ["认证失败", "上下文超过限制", "访问权限", "中止事件", "未记录触发者", "触发者待确认", "用户主动"] {
            check(transition("interrupted", mode: "rage") { $0.status_reason = reason }.first?.canContinue == false, "forbidden auto " + reason)
        }
        check(transition("stalled", mode: "rage").first?.canContinue == false, "stalled only reminds")
        check(transition("completed") { $0.app_id = "cline" }.first?.kind == "completion", "unsupported app completion visible")
        check(transition("completed", mode: "rage") { $0.app_id = "cline" }.first?.canContinue == false, "unsupported cannot optimize")
        check(transition("completed", mode: "rage") { $0.app_id = "qoder-cn" }.first?.canContinue == true, "Qoder CN can continue a verified completed round")
        check(transition("interrupted", mode: "rage") { $0.app_id = "qoder-cn" }.first?.canContinue == true, "Qoder CN can recover a verified interruption")
        check(transition("completed", mode: "rage") { $0.app_id = "qoder" }.first?.canContinue == true, "Qoder can continue a verified completed round")
        check(transition("interrupted", mode: "rage") { $0.app_id = "qoder" }.first?.canContinue == true, "Qoder can recover a verified interruption")
        check(transition("completed") { $0.source = "window" }.isEmpty, "window source cannot arm")
        check(transition("completed") { $0.user_stopped = true }.isEmpty, "user stopped cannot arm")
        // 手动停止不算中断：不产生提醒事件；展示为中性「已手动停止」。
        check(transition("interrupted") { $0.user_stopped = true }.isEmpty, "manual stop is not an interruption")
        var stoppedRow = row
        stoppedRow.status = "interrupted"; stoppedRow.user_stopped = true
        check(stoppedRow.statusLabel == "已手动停止", "manual stop label is neutral")
        check(transition("completed") { $0.timing_basis = "observed" }.isEmpty, "guessed start cannot arm")
        _ = p.observe([row], now: 100); row.status = "completed"
        check(p.observe([row], now: 101).count == 1, "first completion")
        check(p.observe([row], now: 102).isEmpty, "completion once")
        check(p.startCompleted([row], now: 103, prompt: "x", supervisedIDs: [row.id]).count == 1, "explicit historical start after notice")
        check(p.startCompleted([row], now: 104, prompt: "x", supervisedIDs: [row.id]).isEmpty, "explicit click dedup")
        row.status = "running"; _ = p.observe([row], now: 105); row.status = "completed"
        check(p.observe([row], now: 106).isEmpty, "same old round cannot rearm")
        row.started_at = 110; row.status = "running"; _ = p.observe([row], now: 110)
        row.status = "completed"
        check(p.observe([row], now: 111, completionMode: "rage", ragePrompt: "x", supervisedIDs: [row.id]).first?.kind == "followup", "new round rearm")
        row.started_at = 120
        p.observeConfirmedRound(row, now: 121)
        check(p.observe([row], now: 121, completionMode: "rage", ragePrompt: "x", supervisedIDs: [row.id]).first?.kind == "followup", "fast completed receipt rearms")
        row.status = "running"; row.started_at = 130; _ = p.observe([row], now: 130)
        p.resetObservation(); row.status = "interrupted"
        check(p.observe([row], now: 131).isEmpty, "pause reset")
        row.status = "running"; _ = p.observe([row], now: 140); row.status = "unknown"; _ = p.observe([row], now: 141)
        row.status = "interrupted"
        check(p.observe([row], now: 142).count == 1, "brief unknown preserves same verified turn")
        row.started_at = 145; row.status = "running"; _ = p.observe([row], now: 145)
        row.status = "unknown"; row.started_at = 146; _ = p.observe([row], now: 146)
        row.status = "interrupted"
        check(p.observe([row], now: 147).isEmpty, "unknown with changed turn cannot recover")
        row.status = "running"; _ = p.observe([row], now: 150); row.status = "interrupted"
        check(p.observe([row], now: 280).isEmpty, "expired observed baseline")
        row.started_at = 290; row.status = "running"; _ = p.observe([row], now: 290)
        p.recordAttempt(row, now: 289); p.recordAttempt(row, now: 288); p.recordAttempt(row, now: 287)
        row.status = "interrupted"
        let cooled = p.observe([row], now: 291, completionMode: "rage", supervisedIDs: [row.id])
        check(cooled.first?.canContinue == true && (cooled.first?.delay ?? 0) == 1, "rage new round has no cross-send cooldown")
        row.status_reason = "触发者待确认"
        check(ContinuationPolicy.restriction(row, manual: true) == nil, "manual explicit authorizes ambiguous interruption")
        row.status_reason = "用户主动停止"
        check(ContinuationPolicy.restriction(row, manual: true) != nil, "manual explicit stop reason refused without boolean")
        row.status_reason = "触发者待确认"
        row.user_stopped = true
        check(ContinuationPolicy.restriction(row, manual: true) != nil, "manual keeps user stop boundary")
        print("Continuation policy: \(count) checks passed")
    }
}

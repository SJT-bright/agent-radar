import Foundation

@main struct ClockChecks {
    static func main() {
        var checks = 0
        func check(_ value: Bool, _ message: String) {
            precondition(value, message)
            checks += 1
        }
        func sample(_ state: String, start: Double? = nil, end: Double? = nil) -> SessionRecord {
            SessionRecord(id: "test", app_id: "test", app_name: "Test", title: "Task", project: "",
                          status: state, evidence: "", updated_at: 100, source: "local-session", target: "",
                          started_at: start, ended_at: end, timing_basis: start == nil ? nil : "turn")
        }
        var clocks = SessionClocks()
        let first = clocks.update([sample("running")], now: 100)[0]
        check(first.elapsedLabel(at: 105) == "已观察 5秒", "Observed timer must not invent task start")
        let second = clocks.update([sample("running")], now: 110)[0]
        check(second.started_at == 100, "Polling must not reset elapsed")
        let lost = clocks.update([sample("unknown")], now: 120)[0]
        check(lost.ended_at == 110, "Lost evidence freezes at last known observation")
        check(lost.elapsedLabel(at: 500) == "已观察 10秒", "Unknown cannot keep counting")
        let recovered = clocks.update([sample("running")], now: 130)[0]
        check(recovered.started_at == 100 && recovered.ended_at == nil, "Recovery continues same observed run")
        let stopped = clocks.update([sample("interrupted")], now: 150)[0]
        check(stopped.elapsedLabel(at: 999) == "已观察 50秒", "Interruption freezes")
        let stoppedAgain = clocks.update([sample("interrupted")], now: 160)[0]
        check(stoppedAgain.ended_at == 150, "Repeated stop record must not advance clock")
        let newRun = clocks.update([sample("running")], now: 170)[0]
        check(newRun.started_at == 170, "New turn after terminal must reset")
        let actual = clocks.update([sample("running", start: 200)], now: 210)[0]
        check(actual.elapsedLabel(at: 220) == "本轮 20秒", "Use authoritative start")
        var knownStart = sample("running", start: 200)
        knownStart.timing_reason = "已读取本轮开始事件"
        _ = clocks.update([knownStart], now: 210)
        var truncatedTail = sample("running")
        truncatedTail.timing_reason = "日志末尾不含起点"
        let continued = clocks.update([truncatedTail], now: 220)[0]
        check(continued.started_at == 200 && continued.timing_reason == "已读取本轮开始事件", "Keep previously confirmed timing evidence when a growing tail no longer contains it")
        let done = clocks.update([sample("completed", start: 200, end: 230)], now: 250)[0]
        check(done.elapsedLabel(at: 1000) == "本轮 30秒", "Authoritative end remains frozen")
        let future = sample("running", start: 900)
        check(future.elapsedLabel(at: 800) == "时间戳顺序异常", "Clock skew cannot show negative time")
        let noEvidence = sample("unknown")
        check(noEvidence.elapsedLabel(at: 900) == "未提供开始时间", "Unknown does not get a made-up timer")
        check(sample("stalled", start: 200).elapsedLabel(at: 300) == "本轮 1分40秒", "Suspected stall is not a confirmed stop")
        var confirmed = sample("interrupted", start: 200, end: 210)
        confirmed.timing_basis = "last-confirmed"
        check(confirmed.elapsedLabel(at: 900) == "已确认 10秒", "A missing crash timestamp must not imply an exact end")

        let suite = "local.agentradar.tests." + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = MonitorStore(defaults: defaults, writeHealthDiagnostics: false)
        check(!store.autoContinueEnabled, "New installations default to reminders only")
        defaults.set(true, forKey: "autoContinueEnabled")
        check(!MonitorStore(defaults: defaults, writeHealthDiagnostics: false).autoContinueEnabled, "Legacy default-on does not opt into draft replacement")
        store.setAutoContinue(true)
        check(MonitorStore(defaults: defaults, writeHealthDiagnostics: false).autoContinueEnabled, "Explicit opt-in survives restart")
        store.setAutoContinue(false)
        let recovery = ContinuationController()
        var notices: [ContinuationNotice] = []
        recovery.onNotice = { notices.append($0) }
        var reminderRow = sample("running", start: 100)
        reminderRow.app_id = "workbuddy-ai"
        recovery.observe([reminderRow], fresh: true)
        reminderRow.status = "interrupted"
        reminderRow.status_reason = "请求或响应流超时（HTTP 502）"
        recovery.observe([reminderRow], fresh: true)
        check(notices.count == 1 && notices[0].message.contains("仅提醒") && recovery.pendingSessionIDs.isEmpty,
              "Disabled automation still reports a new interruption without queuing a send")
        recovery.observe([reminderRow], fresh: true)
        check(notices.count == 1, "Reminder is not repeated on every poll")
        recovery.configure(enabled: true, paused: false)
        recovery.observe([reminderRow], fresh: true)
        check(recovery.pendingSessionIDs.isEmpty, "Enabling does not replay historical failures")
        recovery.stop()
        check(store.panelSize.width == 60, "Panel starts as a compact hover target")
        store.setExpanded(true)
        let now = Date().timeIntervalSince1970
        store.apps = [AppRecord(id: "test", name: "Test", bundleID: "local.test", pid: 42, path: "")]
        var historicalFailure = sample("interrupted", start: now - 4000, end: now - 3600)
        historicalFailure.id = "historical"
        historicalFailure.updated_at = now - 3600
        var latest = sample("completed", start: now - 100, end: now - 10)
        latest.id = "latest"
        latest.updated_at = now - 10
        store.sessions = [historicalFailure, latest]
        check(store.displaySessions.map(\.id) == ["latest"], "Old failure must not hide the newer completed conversation")
        var active = sample("running", start: now - 60)
        active.updated_at = now
        var secondActive = active
        secondActive.id = "second"
        store.sessions = [historicalFailure, active, secondActive]
        check(Set(store.displaySessions.map(\.id)) == ["test", "second"], "All simultaneous active conversations must be visible")
        let collapsedHeight = store.panelSize.height
        // 跨午夜运行时，「1 小时前」已属昨天，分组展开不再收录它（当日过滤）。
        let historicalIsToday = Calendar.current.isDateInToday(Date(timeIntervalSince1970: now - 3600))
        check(store.conversationGroups.count == 1 &&
              store.conversationGroups[0].sessions.count == (historicalIsToday ? 3 : 2) &&
              store.conversationGroups[0].primary.isActive &&
              !store.isAppGroupExpanded("test"), "Same-app conversations start in one collapsed group")
        store.toggleAppGroup("test")
        check(store.isAppGroupExpanded("test") && store.panelSize.height > collapsedHeight &&
              store.displaySessions.count == 2 &&
              store.conversationGroups[0].sessions.contains(where: { $0.id == "historical" }) == historicalIsToday,
              "Expanding reveals readable history without changing the current monitoring list")
        store.toggleAppGroup("test")
        check(!store.isAppGroupExpanded("test") && store.panelSize.height == collapsedHeight,
              "Collapsing restores the compact panel height")
        historicalFailure.ended_at = now - 30
        historicalFailure.updated_at = now - 30
        store.sessions = [historicalFailure, active]
        check(Set(store.displaySessions.map(\.id)) == ["historical", "test"], "A fresh interruption remains visible beside another active conversation")
        active.status = "unknown"
        store.sessions = [historicalFailure, active]
        check(!store.displaySessions.contains(where: { $0.id == "test" }), "Unreadable tasks stay out of the floating UI")
        check(store.sessions.contains(where: { $0.id == "test" }), "Unreadable tasks remain monitored in the background")
        store.sessions = [active, latest]
        check(store.displaySessions.isEmpty, "A newer unreadable task must not be replaced by an old completed task")
        var uncertainCodex = active
        uncertainCodex.app_id = "codex"
        uncertainCodex.app_name = "Codex"
        uncertainCodex.id = "codex:f9160256-f9b4-5a49-b6a2-1c558250176b"
        uncertainCodex.target = "codex://threads/f9160256-f9b4-5a49-b6a2-1c558250176b"
        uncertainCodex.project = "/projects/long-conversation"
        store.apps.append(AppRecord(id: "codex", name: "Codex", bundleID: "com.openai.codex", pid: 43, path: ""))
        store.sessions = [uncertainCodex]
        check(store.displaySessions.map(\.id) == [uncertainCodex.id], "Verified Codex identity remains visible while lifecycle is unknown")
        check(store.activeCount == 0 && store.ongoingSessions.isEmpty && !uncertainCodex.isReadable,
              "Visible uncertainty cannot grant automation or invent an active task")
        check(uncertainCodex.statusLabel == "待确认", "Unknown lifecycle must be stated explicitly")
        store.removeSession(uncertainCodex)
        check(store.displaySessions.isEmpty, "Unknown Codex respects explicit removal")
        store.restoreSession(RemovedSession.key(for: uncertainCodex))
        uncertainCodex.target = "codex://threads/different"
        store.sessions = [uncertainCodex]
        check(store.displaySessions.isEmpty, "Mismatched Codex identity cannot be displayed as verified")
        var unnamed = active
        unnamed.status = "running"
        unnamed.title = "Test · 会话待识别"
        store.sessions = [unnamed]
        check(store.displaySessions.isEmpty, "Placeholders cannot appear even with active status")
        store.sessions = [latest]
        check(store.displaySessions.count == 1, "An idle application's latest conversation is expanded by default")
        check(store.panelSize.width == 238, "Expanded panel stays narrow")

        historicalFailure.ended_at = now - 3600
        historicalFailure.updated_at = now - 3600
        store.sessions = [historicalFailure, latest]
        store.removeSession(latest)
        check(store.displaySessions.isEmpty, "Removal must not resurrect old history")
        check(store.visibleSessions.allSatisfy { $0.id != latest.id }, "Removal applies to alternate list too")
        check(store.sessions.contains(latest), "Removal preserves the observed source record")
        check(store.undoRemovalKey != nil, "Removal offers inline undo")
        let reloaded = MonitorStore(defaults: defaults, writeHealthDiagnostics: false)
        reloaded.apps = store.apps
        reloaded.sessions = store.sessions
        check(reloaded.displaySessions.isEmpty && reloaded.isRemoved(latest), "Exclusion survives app restart")
        var renamed = latest
        renamed.title = "Renamed conversation"
        renamed.updated_at = now + 10
        renamed.status = "running"
        check(reloaded.isRemoved(renamed), "Stable source ID survives title/status changes")
        store.undoRemoval()
        check(store.displaySessions.map(\.id) == [latest.id] && store.undoRemovalKey == nil, "Undo restores the current record")
        check(MonitorStore(defaults: defaults, writeHealthDiagnostics: false).removedSessions.isEmpty, "Restore persists immediately")
        active.status = "running"
        store.sessions = [active, secondActive]
        store.removeSession(active)
        check(store.displaySessions.map(\.id) == [secondActive.id] && store.activeCount == 1,
              "Removing one task keeps other concurrent tasks and counts accurate")
        store.removeSession(secondActive)
        check(store.displaySessions.isEmpty && store.removedSessions.count == 2, "Multiple removals stay excluded")
        store.restoreAllSessions()
        check(store.displaySessions.count == 2 && store.removedSessions.isEmpty, "Restore all preserves current states")
        var window = active
        window.source = "window"; window.id = "window:test:123:1"; window.pid = 123
        store.removeSession(window)
        window.id = "window:test:456:9"; window.pid = 456
        check(store.isRemoved(window), "Native exclusion survives transient PID/window ID changes")
        window.app_id = "other-app"
        check(!store.isRemoved(window), "Same title in another app is not removed")
        window.app_id = "test"; window.title = "Different task"
        check(!store.isRemoved(window), "Different native task remains visible")
        store.restoreAllSessions()

        let controller = ContinuationController()
        controller.configure(enabled: true, paused: false)
        var recoverable = active
        recoverable.app_id = "codex"
        recoverable.status_reason = "连接失败"
        controller.observe([recoverable], fresh: true)
        recoverable.status = "interrupted"
        controller.observe([recoverable], fresh: true)
        check(controller.pendingSessionIDs == [recoverable.id], "A valid transition queues recovery before sending")
        controller.exclude([recoverable.id])
        check(controller.pendingSessionIDs.isEmpty, "Removal cancels pending recovery immediately")
        recoverable.status = "running"; recoverable.started_at = now + 2
        controller.observe([recoverable], fresh: true, excludedIDs: [recoverable.id])
        recoverable.status = "interrupted"
        controller.observe([recoverable], fresh: true, excludedIDs: [recoverable.id])
        check(controller.pendingSessionIDs.isEmpty, "Removed tasks cannot queue new automatic recovery")
        controller.observe([recoverable], fresh: true)
        check(controller.pendingSessionIDs.isEmpty, "Restoring an interrupted task does not replay missed actions")
        recoverable.status = "running"; recoverable.started_at = now + 3
        controller.observe([recoverable], fresh: true)
        recoverable.status = "interrupted"
        controller.observe([recoverable], fresh: true)
        check(controller.pendingSessionIDs == [recoverable.id], "A new observed turn after restoration can recover")
        controller.stop()

        store.expanded = false
        check(store.panelSize.width == 60 && store.panelSize.height == 32, "Collapse keeps a small horizontal capsule")
        var motion = PanelMotion(size: NSSize(width: 60, height: 32), target: NSSize(width: 238, height: 548))
        let initialStep = motion.advance(by: 1.0 / 60)
        check(initialStep.width > 60 && initialStep.width < 238, "Click starts motion on the next display tick")
        for _ in 0..<5 { _ = motion.advance(by: 1.0 / 60) }
        let beforeReversal = motion.width
        let velocityBeforeReversal = motion.widthVelocity
        motion.target = NSSize(width: 60, height: 32)
        check(motion.width == beforeReversal && motion.widthVelocity == velocityBeforeReversal,
              "Reversal preserves current position and velocity rather than restarting from an endpoint")
        for _ in 0..<45 { _ = motion.advance(by: 1.0 / 60) }
        check(motion.settled && abs(motion.width - 60) < 0.25, "Reversed motion settles at the closed rail")
        motion.target = NSSize(width: 238, height: 548)
        for _ in 0..<45 { _ = motion.advance(by: 1.0 / 60) }
        check(motion.settled && abs(motion.height - 548) < 0.5, "Repeated expansion reaches its target without a queue")

        var panelHover = PanelHoverState()
        panelHover.presentationChanged(expanded: false, pointerInside: false)
        check(panelHover.update(expanded: false, pointerInside: true, now: 0) == true,
              "Hovering anywhere on the compact panel opens the whole window")
        panelHover.presentationChanged(expanded: true, pointerInside: true)
        check(panelHover.update(expanded: true, pointerInside: false, now: 0.1) == false,
              "Leaving the expanded panel collapses immediately without delay")
        check(panelHover.update(expanded: false, pointerInside: true, now: 0.2) == true,
              "Hovering the compact rail reopens after an immediate collapse")
        panelHover.presentationChanged(expanded: true, pointerInside: true)
        check(panelHover.update(expanded: true, pointerInside: true, dragging: true, now: 0.3) == nil,
              "Dragging the panel never collapses it")
        check(panelHover.update(expanded: true, pointerInside: false, dragging: true, now: 0.4) == nil,
              "Sliding outside mid-drag does not collapse either")
        check(panelHover.update(expanded: true, pointerInside: false, now: 0.5) == false,
              "Collapsing resumes immediately once the drag ends")
        panelHover.presentationChanged(expanded: false, pointerInside: true)
        check(panelHover.update(expanded: false, pointerInside: true, now: 4) == nil,
              "Clicking the outer fold control does not reopen while still under the pointer")
        check(panelHover.update(expanded: false, pointerInside: false, now: 4.1) == nil &&
              panelHover.update(expanded: false, pointerInside: true, now: 4.2) == true,
              "Leaving and reentering rearms compact-window hover")
        panelHover.presentationChanged(expanded: true, pointerInside: false)
        check(panelHover.update(expanded: true, pointerInside: false, now: 5) == nil,
              "Programmatic opening waits for the pointer to enter before auto-folding")
        // 久远对话不上屏：非活动条目仅当天显示，分组展开同样只补当天
        var staleDone = sample("completed", start: now - 90000, end: now - 89000)
        staleDone.id = "stale-done"
        staleDone.updated_at = now - 89000
        var freshDone = sample("completed", start: now - 500, end: now - 10)
        freshDone.id = "fresh-done"
        freshDone.updated_at = now - 10
        store.sessions = [staleDone, freshDone]
        check(store.displaySessions.map(\.id) == ["fresh-done"],
              "Only today's conversations surface for an idle app")
        check(store.conversationGroups.count == 1 && store.conversationGroups[0].sessions.count == 1,
              "Disclosed group history is limited to today")
        print("\(checks) session clock checks passed")
    }
}

import AppKit
import Foundation

struct ContinuationNotice {
    var title: String
    var conversation: String
    var message: String
    var cancellable = false
    var needsPermission = false
    var isCompletion = false
    var sessionID: String? = nil
    var recoveryKey: String? = nil
    var canRetry = false
    var isRecovering = false
}

protocol ContinuationSending: AnyObject {
    var isRunning: Bool { get }
    func cancel()
    func run(_ request: [String: Any], completion: @escaping (String, Bool, Double?) -> Void)
}

final class ContinuationBridge: ContinuationSending {
    private var process: Process?
    private var generation = 0
    var isRunning: Bool { process?.isRunning == true }

    func cancel() {
        generation += 1
        if let process = process, process.isRunning {
            process.terminate()
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                if process.isRunning { kill(process.processIdentifier, SIGKILL) }
            }
        }
    }

    func run(_ request: [String: Any], completion: @escaping (String, Bool, Double?) -> Void) {
        guard !isRunning, let root = Bundle.main.resourcePath else { completion("another_recovery", false, nil); return }
        let task = Process(), input = Pipe(), output = Pipe()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        task.arguments = ["-B", root + "/supervisor/bridge.py"]
        task.environment = ProcessInfo.processInfo.environment.merging([
            "PYTHONPATH": root + "/watchdog:" + root + "/python:" + root + "/collector",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"
        ]) { _, new in new }
        task.standardInput = input
        task.standardOutput = output
        task.standardError = FileHandle.nullDevice
        generation += 1
        let current = generation
        task.terminationHandler = { [weak self] task in
            let data = output.fileHandleForReading.readDataToEndOfFile()
            let result = data.count < 4096 ? (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] : nil
            DispatchQueue.main.async {
                guard let self = self, self.generation == current else { return }
                self.process = nil
                completion(result?["code"] as? String ?? "bridge_timeout", result?["attempted"] as? Bool ?? true, result?["retry_after"] as? Double)
            }
        }
        do {
            try task.run()
            process = task
            input.fileHandleForWriting.write(try JSONSerialization.data(withJSONObject: request))
            try? input.fileHandleForWriting.close()
        } catch {
            process = nil
            completion("bridge_unavailable", false, nil)
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 40) { [weak self] in
            guard self?.generation == current, task.isRunning else { return }
            task.terminate()
            DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                if task.isRunning { kill(task.processIdentifier, SIGKILL) }
            }
        }
    }
}

final class ContinuationController {
    var onNotice: ((ContinuationNotice) -> Void)?
    var onSummary: ((String) -> Void)?
    var rulesProvider: (() -> PromptRules)?
    private var policy = ContinuationPolicy()
    private let bridge: ContinuationSending
    private let countDefaults: UserDefaults
    private static let rageSendCountsKey = "rageSuccessfulSendsBySession.v1"
    private var successfulRageSends: [String: Int]
    private var supervisedIDs = Set<String>()
    private let clock: () -> Double
    private var rows: [SessionRecord] = []
    private var excludedIDs = Set<String>()
    private var lastFresh: Double?
    private var queue: [ContinuationEvent] = []
    private var active: ContinuationEvent?
    private var attemptedKeys = Set<String>()
    private var confirmations: [String: (event: ContinuationEvent, until: Double, delayReported: Bool)] = [:]
    private var timer: Timer?
    private var enabled = false
    private var paused = false
    private var executing = false
    private var helperPermission = false
    private var monitoringBeganAt: Double?
    private var lastResult = "尚未发生新的意外中断"
    private var generation = 0
    private var rules: PromptRules { rulesProvider?() ?? PromptRules() }
    private var fresh: Bool { lastFresh.map { clock() - $0 <= 15 } ?? false }

    init(bridge: ContinuationSending = ContinuationBridge(), judgeBridge: ContinuationSending = ContinuationBridge(), clock: @escaping () -> Double = { Date().timeIntervalSince1970 }, countDefaults: UserDefaults = .standard) {
        // Keep judgeBridge in the initializer for existing callers, but never run answer judgement.
        _ = judgeBridge
        self.bridge = bridge; self.clock = clock; self.countDefaults = countDefaults
        self.successfulRageSends = (countDefaults.dictionary(forKey: Self.rageSendCountsKey) ?? [:]).compactMapValues {
            guard let count = $0 as? Int, count >= 0 else { return nil }
            return count
        }
    }
    var diagnostic: String {
        "运行模式：\(rules.completionMode == "rage" ? "狂暴模式" : "协作模式")\n自动发送：\(enabled ? "开启" : "关闭")\n输入组件：\(helperPermission ? "辅助功能可用" : "等待授权或检查")\n监督标记：\(supervisedIDs.count)\n排队：\(queue.count)\n每轮只尝试一次；狂暴完成轮次间隔至少 5 秒，可核验意外中断在发现后 1 秒排定，优先争取 10 秒内发送；权限、限流、用户操作或其他输入仍可能阻碍。发送后独立核验新轮次。关闭、暂停或修改规则立即取消旧任务。\n最近结果：\(lastResult)"
    }
    func start(enabled: Bool) {
        self.enabled = enabled
        monitoringBeganAt = enabled && !paused ? clock() : nil
        timer?.invalidate()
        let timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in self?.tick() }
        self.timer = timer
        RunLoop.main.add(timer, forMode: .common)
        checkReadiness()
    }
    func stop() {
        monitoringBeganAt = nil
        timer?.invalidate(); cancelCurrent()
        discardQueued({ _ in true }, message: "监督已停止，已取消恢复")
    }
    func configure(enabled: Bool, paused: Bool) {
        self.enabled = enabled; self.paused = paused
        monitoringBeganAt = enabled && !paused ? clock() : nil
        policy.resetObservation(); discardQueued({ _ in true }, message: "设置或监控状态已变化，已取消恢复"); cancelCurrent()
        onSummary?(paused ? "监督已暂停" : (enabled ? "自动发送已开启" : "仅提醒，不自动发送"))
    }
    func checkReadiness() {
        guard !executing, !bridge.isRunning else { return }
        bridge.run(["mode": "health"]) { [weak self] code, _, _ in
            guard let self = self else { return }
            self.helperPermission = code == "ready"
            self.lastResult = Self.explain(code)
            self.onSummary?(self.enabled ? (code == "ready" ? "恢复组件授权已确认，具体会话仍需核验" : code == "permission_required" ? "恢复组件待授权" : Self.explain(code)) : "仅提醒，不自动发送")
        }
    }
    var pendingSessionIDs: Set<String> {
        Set(queue.map { $0.session.id })
            .union(active.map { [$0.session.id] } ?? [])
            .union(confirmations.values.map { $0.event.session.id })
    }
    /// Selection changes are scoped: work belonging to another marked session survives.
    func setSupervisedIDs(_ ids: Set<String>, temporarilyUnavailable: Set<String> = []) {
        // Provider-wide supervision must survive a short unknown snapshot.
        // Only IDs already supervised can be retained; an unknown new row
        // cannot enter automatic work before it becomes readable.
        let desired = ids.union(supervisedIDs.intersection(temporarilyUnavailable))
        let removed = supervisedIDs.subtracting(desired)
        let added = desired.subtracting(supervisedIDs)
        supervisedIDs = desired
        policy.forget(removed)
        discardConfirmations({ removed.contains($0.session.id) && !$0.manual }, message: "已取消监督标记，停止跟踪发送确认")
        discardQueued({ removed.contains($0.session.id) && $0.automationMode == "rage" && !$0.manual }, message: "已取消监督标记，停止本轮自动操作")
        if let event = active, removed.contains(event.session.id), event.automationMode == "rage", !event.manual { cancelActiveSend() }
        if fresh && !paused {
            for row in rows where added.contains(row.id) && row.status == "running" && !excludedIDs.contains(row.id) && unique(row) {
                policy.observeConfirmedRound(row, now: clock())
            }
        }
    }
    func exclude(_ ids: Set<String>) {
        excludedIDs.formUnion(ids)
        discardConfirmations({ ids.contains($0.session.id) }, message: "会话已移除，停止跟踪发送确认")
        policy.forget(ids); discardQueued({ ids.contains($0.session.id) }, message: "会话已从雷达移除，已取消恢复")
        if let active = active, ids.contains(active.session.id) { cancelActiveSend() }
    }
    func observe(_ sessions: [SessionRecord], fresh: Bool, excludedIDs: Set<String> = []) {
        rows = sessions
        self.excludedIDs = excludedIDs
        exclude(excludedIDs)
        guard fresh, !paused else {
            lastFresh = nil; policy.resetObservation()
            tick()
            return
        }
        lastFresh = clock()
        // Confirm the previous send before evaluating a fast new completion.
        confirmNewRound()
        let current = rules
        let now = clock()
        let included = sessions.filter { !excludedIDs.contains($0.id) }
        var events = policy.observe(included, now: now,
            interruptText: current.interruptText, rateLimitAllowed: current.rateLimitWakeEnabled,
            rateLimitDelay: Double(current.rateLimitWaitMinutes) * 60, rateLimitText: current.rateLimitText,
            completionMode: current.completionMode, ragePrompt: current.ragePrompt, supervisedIDs: supervisedIDs)
        // A fresh ZCode lifecycle end can be committed between snapshots even
        // when its running row was briefly absent. It is a new monitored round
        // only if its authoritative end happened after monitoring began.
        if let began = monitoringBeganAt, enabled, current.completionMode == "rage" {
            for row in included where row.app_id == "zcode" && ["completed", "interrupted"].contains(row.status) {
                guard supervisedIDs.contains(row.id), unique(row),
                      !events.contains(where: { $0.session.id == row.id }),
                      row.source == "local-session", row.timing_basis == "turn",
                      ContinuationPolicy.identityRestriction(row) == nil,
                      let start = row.started_at, let end = row.ended_at,
                      start > 0, end >= start, end > began,
                      now - end >= -2, now - end <= 120 else { continue }
                policy.observeConfirmedRound(row, now: now)
                events += policy.observe([row], now: now,
                    interruptText: current.interruptText, rateLimitAllowed: current.rateLimitWakeEnabled,
                    rateLimitDelay: Double(current.rateLimitWaitMinutes) * 60, rateLimitText: current.rateLimitText,
                    completionMode: current.completionMode, ragePrompt: current.ragePrompt,
                    supervisedIDs: supervisedIDs)
            }
        }
        accept(events)
        tick()
    }
    private func accept(_ events: [ContinuationEvent]) {
        for event in events {
            if event.canContinue && enabled {
                enqueue(event)
            }
            else {
                let text = event.kind == "completion" ? event.explanation
                    : (!enabled && event.canContinue ? "自动发送未开启，仅提醒。" + event.explanation : event.explanation)
                notice(event, text, cancellable: false)
            }
        }
    }
    private func discardQueued(_ matches: (ContinuationEvent) -> Bool, message: String) {
        let cancelled = queue.filter(matches)
        queue.removeAll(where: matches)
        for event in cancelled where event.manual { notice(event, message, cancellable: false) }
    }
    private func enqueue(_ incoming: ContinuationEvent) {
        guard !attemptedKeys.contains(incoming.key), active?.key != incoming.key,
              !queue.contains(where: { $0.key == incoming.key }) else { return }
        var event = incoming
        event.notBefore = clock() + max(0, event.delay)
        queue.append(event)
        notice(event, event.manual ? "已排定手动恢复" : "已排定恢复，发送前会再次核验会话", cancellable: true)
    }
    func startRageForCompleted() {
        guard enabled, !paused, fresh, rules.completionMode == "rage" else {
            onSummary?("请先开启狂暴模式与自动发送，并等待最新采集"); return
        }
        let candidates = rows.filter { supervisedIDs.contains($0.id) && !excludedIDs.contains($0.id) && unique($0) }
        for event in policy.startCompleted(candidates, now: clock(), prompt: rules.ragePrompt, supervisedIDs: supervisedIDs) { enqueue(event) }
        onSummary?("已排定 \(queue.count) 条已标记会话的持续优化")
        tick()
    }
    func requestManualRetry(sessionID: String, key: String) {
        guard let row = rows.first(where: { $0.id == sessionID }) else { return }
        let completed = row.status == "completed"
        var event = ContinuationEvent(session: row, key: key, detectedAt: clock(), canContinue: true,
                                      explanation: completed ? "手动继续当前会话" : "手动恢复当前会话",
                                      kind: completed ? "followup" : "interrupt",
                                      text: completed ? ContinuationPolicy.manualContinueText :
                                          ContinuationPolicy.resumeText(forApp: row.app_id,
                                              overrides: rules.appResumeTexts, global: rules.resumePrompt),
                                      delay: 3, patience: 15, automationMode: rules.completionMode, manual: true)
        event.bypassRestriction = !completed && rules.rateLimitWakeEnabled && ContinuationPolicy.isRateLimited(row)
        guard !paused, fresh, !excludedIDs.contains(sessionID), unique(row), ["completed", "interrupted"].contains(row.status),
              ContinuationPolicy.key(row) == key else { notice(event, "会话状态或轮次已变化，或采集已过期；未执行恢复", cancellable: false); return }
        let restriction = completed ? ContinuationPolicy.followupRestriction(row)
            : ContinuationPolicy.restriction(row, allowRateLimit: event.bypassRestriction, manual: true)
        if let reason = restriction {
            notice(event, reason, cancellable: false); return
        }
        guard !attemptedKeys.contains(key) else { notice(event, Self.explain("already_attempted"), cancellable: false); return }
        guard active?.key != key, !queue.contains(where: { $0.key == key && $0.manual }) else { return }
        queue.removeAll { $0.key == key }
        enqueue(event)
        tick()
    }
    func cancelCurrent(sessionID: String, key: String) {
        discardConfirmations({ $0.session.id == sessionID && $0.key == key }, message: "已停止跟踪发送确认；已发送消息不会撤回")
        let cancelled = queue.first { $0.session.id == sessionID && $0.key == key }
        queue.removeAll { $0.session.id == sessionID && $0.key == key }
        if active?.session.id == sessionID && active?.key == key { cancelActiveSend() }
        else if let event = cancelled { notice(event, "已取消本轮恢复", cancellable: false) }
    }
    func cancelCurrent() {
        discardConfirmations({ _ in true }, message: "已停止跟踪发送确认；已发送消息不会撤回")
        cancelActiveSend()
    }
    private func cancelActiveSend() {
        generation += 1
        let cancelled = active
        if executing, let event = active { attemptedKeys.insert(event.key) }
        bridge.cancel(); executing = false; active = nil
        if let event = cancelled { notice(event, "已取消本轮恢复；若已点击发送，不会撤回消息", cancellable: false) }
    }
    private func unique(_ row: SessionRecord) -> Bool {
        rows.filter { $0.id == row.id }.count == 1 && !rows.contains { other in
            guard other.id != row.id, other.app_id == row.app_id,
                  (other.navigation_title ?? other.title) == (row.navigation_title ?? row.title) else { return false }
            // 同名但导航键不同（不同会话 ID）时可以区分定位，不再整体放弃恢复。
            if let key = row.navigation_key, let otherKey = other.navigation_key, key != otherKey { return false }
            // ZCode 的跨项目同名任务由桥端 project+title 联合核验，工作区不同即可区分。
            if row.app_id == "zcode", !row.project.isEmpty, row.project != other.project { return false }
            return true
        }
    }
    private func sameIdentity(_ row: SessionRecord, _ original: SessionRecord) -> Bool {
        row.id == original.id && row.app_id == original.app_id && row.project == original.project
            && row.source == original.source && row.target == original.target
            && row.navigation_key == original.navigation_key && row.navigation_title == original.navigation_title
    }
    private func isAutomaticRageSend(_ event: ContinuationEvent) -> Bool {
        event.automationMode == "rage" && !event.manual && ["followup", "interrupt"].contains(event.kind)
    }
    private func nextRagePrompt(for row: SessionRecord) -> String? {
        let current = rules
        let every = max(1, current.rageAlternateEvery)
        let sent = successfulRageSends[row.id, default: 0]
        let alternate = sent % every == every - 1
        let prompt = alternate ? current.rageAlternatePrompt : current.ragePrompt(forApp: row.app_id)
        return ContinuationPolicy.cleaned(PromptRules.sanitize(prompt))
    }
    private func recordSuccessfulRageSend(for sessionID: String) {
        let sent = successfulRageSends[sessionID, default: 0]
        successfulRageSends[sessionID] = sent == Int.max ? 1 : sent + 1
        countDefaults.set(successfulRageSends, forKey: Self.rageSendCountsKey)
    }
    private func retryAllowed(_ event: ContinuationEvent) -> Bool {
        guard !paused, fresh, !excludedIDs.contains(event.session.id), !attemptedKeys.contains(event.key),
              let row = rows.first(where: { $0.id == event.session.id }), unique(row),
              ["completed", "interrupted"].contains(row.status), ContinuationPolicy.key(row) == event.key else { return false }
        return row.status == "completed" ? ContinuationPolicy.followupRestriction(row) == nil
            : ContinuationPolicy.restriction(row, allowRateLimit: rules.rateLimitWakeEnabled && ContinuationPolicy.isRateLimited(row), manual: true) == nil
    }
    private func notice(_ event: ContinuationEvent, _ message: String, cancellable: Bool = true, permission: Bool = false) {
        lastResult = message; onSummary?(message)
        let recovering = (active?.key == event.key && executing) || confirmations[event.key] != nil || queue.contains { $0.key == event.key && $0.manual }
        onNotice?(ContinuationNotice(title: event.session.app_name + (event.kind == "completion" ? " · 完成待验收" : event.kind == "followup" ? " · 持续监督" : " · 中断恢复"),
            conversation: event.session.title, message: message, cancellable: cancellable,
            needsPermission: permission, isCompletion: event.kind == "completion", sessionID: event.session.id,
            recoveryKey: event.key, canRetry: !recovering && retryAllowed(event), isRecovering: recovering))
    }
    private func discardConfirmations(_ matches: (ContinuationEvent) -> Bool, message: String) {
        let cancelled = confirmations.values.map { $0.event }.filter(matches)
        for event in cancelled { confirmations[event.key] = nil }
        for event in cancelled { notice(event, message, cancellable: false) }
    }
    private func confirmNewRound() {
        guard fresh else { return }
        for pending in Array(confirmations.values) {
            let event = pending.event
            if let row = rows.first(where: { $0.id == event.session.id }),
               !sameIdentity(row, event.session) || row.user_stopped == true {
                confirmations[event.key] = nil
                notice(event, "会话身份已变化或已主动停止，停止跟踪发送确认", cancellable: false)
                continue
            }
            if let row = rows.first(where: { $0.id == event.session.id }), unique(row),
               sameIdentity(row, event.session),
               ["running", "completed", "interrupted"].contains(row.status),
               (row.started_at ?? 0) > (event.session.started_at ?? 0),
               ContinuationPolicy.identityRestriction(row) == nil {
                // A fast terminal turn between polls also proves delivery. The
                // ordinary interruption restrictions still decide its recovery.
                policy.observeConfirmedRound(row, now: clock())
                confirmations[event.key] = nil
                notice(event, "已观察到原会话的新轮次，发送已确认", cancellable: false)
            } else if clock() >= pending.until && !pending.delayReported {
                // Slow apps may finish between polls after the 30-second notice.
                // Keep one receipt per pending session so that terminal turn can
                // still be observed; this never retries the original send.
                confirmations[event.key]?.delayReported = true
                notice(event, "已点击发送，30 秒内尚未观察到新轮次；继续观察，不重复发送")
            }
        }
    }
    private func nextReadyIndex(at now: Double) -> Int? {
        queue.indices.filter { queue[$0].notBefore <= now && (enabled || queue[$0].manual) }.min {
            let lhs = queue[$0], rhs = queue[$1]
            func priority(_ event: ContinuationEvent) -> Int {
                event.manual ? 0 : (event.kind == "interrupt" ? 1 : 2)
            }
            if priority(lhs) != priority(rhs) { return priority(lhs) < priority(rhs) }
            return lhs.detectedAt < rhs.detectedAt
        }
    }
    private func reportRecoveryDelays(at now: Double) {
        func overdue(_ event: ContinuationEvent) -> Bool {
            !event.manual && event.automationMode == "rage" && event.kind == "interrupt"
                && !event.bypassRestriction && !event.recoveryDelayReported && now - event.detectedAt > 10
        }
        if var event = active, executing, overdue(event) {
            event.recoveryDelayReported = true; active = event
            notice(event, "恢复已超过 10 秒，仍在核验会话与输入框；尚未确认发送")
        }
        for index in queue.indices where overdue(queue[index]) {
            queue[index].recoveryDelayReported = true
            notice(queue[index], "恢复已超过 10 秒，正在等待前一操作或重新核验输入条件；尚未发送")
        }
    }
    // Internal visibility supports deterministic mock-clock scheduling checks.
    func tick() {
        guard !paused else { return }
        let now = clock()
        if !fresh {
            discardConfirmations({ _ in true }, message: "采集已过期，停止跟踪发送确认；不会重复发送")
            if active != nil { cancelActiveSend() }
            discardQueued({ _ in true }, message: "采集已过期，已取消本轮恢复"); return
        }
        confirmNewRound()
        reportRecoveryDelays(at: now)
        guard !executing else { return }
        discardQueued({ now - $0.detectedAt > $0.patience }, message: "等待超过时限，本轮恢复已取消")
        if active == nil, !bridge.isRunning, let index = nextReadyIndex(at: now) {
            active = queue.remove(at: index)
        }
        guard var event = active else { return }
        guard enabled || event.manual else { cancelActiveSend(); return }
        guard event.manual || event.automationMode != "rage" || (rules.completionMode == "rage" && supervisedIDs.contains(event.session.id)) else {
            finish(event, "会话已取消监督或模式已变化，已停止自动操作"); return
        }
        guard let current = rows.first(where: { $0.id == event.session.id }), unique(current), !excludedIDs.contains(current.id) else {
            finish(event, "会话不可读或无法唯一定位，已取消恢复"); return
        }
        guard (event.kind == "followup" ? current.status == "completed" : current.status == "interrupted"),
              ContinuationPolicy.key(current) == event.key,
              sameIdentity(current, event.session) else { finish(event, "任务身份、状态或轮次已变化，已取消恢复"); return }
        let restriction = event.kind == "followup" ? ContinuationPolicy.followupRestriction(current)
            : ContinuationPolicy.restriction(current, allowRateLimit: event.bypassRestriction && ContinuationPolicy.isRateLimited(current), manual: event.manual)
        if let reason = restriction { finish(event, reason); return }
        if now - event.detectedAt > event.patience { finish(event, "等待超过时限，本轮恢复已取消"); return }
        guard !bridge.isRunning else { return }
        if isAutomaticRageSend(event) {
            if event.kind == "followup" {
                guard let text = nextRagePrompt(for: current) else {
                    finish(event, "持续监督提示词为空，未自动发送"); return
                }
                event.text = text
            }
            // 中断恢复沿用策略层解析的按软件提示词，不套用续接轮换。
        }
        event.session = current; active = event
        executing = true
        notice(event, "正在定位原会话并检查输入框…")
        var request: [String: Any] = ["mode": "send", "id": current.id, "app_id": current.app_id,
            "title": current.title, "key": event.key, "target": current.target, "text": event.text,
            "project": current.project, "source": current.source,
            "overwrite_draft": true, "kind": event.kind, "allow_rate_limit": event.bypassRestriction,
            "automation_mode": event.automationMode, "manual_retry": event.manual]
        request["started_at"] = current.started_at
        request["navigation_title"] = current.navigation_title
        request["navigation_key"] = current.navigation_key
        let token = generation
        bridge.run(request) { [weak self] code, attempted, retryAfter in
            guard let self = self, self.generation == token, self.active?.key == event.key else { return }
            self.executing = false
            if code == "permission_required" { self.helperPermission = false }
            if attempted || code == "already_attempted" || code == "sent_pending_confirmation" || code == "sent_queued_promoted" {
                self.attemptedKeys.insert(event.key)
                self.policy.recordAttempt(current, now: self.clock())
            }
            if code == "sent_pending_confirmation" || code == "sent_queued_promoted" {
                if self.isAutomaticRageSend(event) {
                    self.recordSuccessfulRageSend(for: current.id)
                }
                self.helperPermission = true
                self.confirmations[event.key] = (event, self.clock() + 30, false)
                self.active = nil
                let elapsed = max(0, Int(self.clock() - event.detectedAt))
                self.notice(event, code == "sent_queued_promoted"
                    ? "已点击发送和插队（发现后 \(elapsed) 秒），正在确认原会话的新轮次…"
                    : "已点击发送（发现后 \(elapsed) 秒），正在确认原会话的新轮次…")
            } else if !attempted && code == "cooldown" && event.automationMode != "rage" {
                self.finish(event, Self.explain(code))
            } else if !attempted && ["user_active", "locked", "permission_required", "focus_changed", "another_recovery", "cooldown"].contains(code) {
                if event.manual && !["cooldown", "user_active", "locked", "focus_changed", "another_recovery"].contains(code) {
                    self.finish(event, Self.explain(code))
                } else {
                    // Keep the once-only delay notice flag if AX took over ten
                    // seconds before returning this temporary failure.
                    var deferred = self.active ?? event
                    deferred.recoveryDelayReported = self.active?.recoveryDelayReported ?? event.recoveryDelayReported
                    let fastInterrupt = event.automationMode == "rage" && event.kind == "interrupt" && !event.bypassRestriction
                    let fallback: Double = code == "permission_required" ? 15 : (fastInterrupt ? 1 : (code == "cooldown" ? 60 : 5))
                    deferred.notBefore = self.clock() + max(1, retryAfter ?? fallback)
                    if code == "cooldown" && event.automationMode == "rage" {
                        deferred.patience = max(deferred.patience, deferred.notBefore - deferred.detectedAt + 600)
                    }
                    self.active = nil; self.queue.append(deferred)
                    self.notice(event, Self.explain(code), permission: code == "permission_required")
                }
            } else if !attempted && !event.manual && event.routeRetries < 3
                      && ["target_unverified", "app_unavailable", "search_unavailable", "search_result_missing",
                          "header_unverified", "sidebar_target_missing", "tree_incomplete"].contains(code) {
                // 路由瞬态失败（标题自动改名、索引撕裂读、应用正在重启）
                // 不再终止该轮恢复：有限次退避重试，持续失败仍然如实终局。
                var deferred = self.active ?? event
                deferred.routeRetries += 1
                let fastInterrupt = event.automationMode == "rage" && event.kind == "interrupt" && !event.bypassRestriction
                let fallback: Double = code == "app_unavailable" ? 20 : (fastInterrupt ? 1 : 5)
                deferred.notBefore = self.clock() + max(1, retryAfter ?? fallback)
                self.active = nil; self.queue.append(deferred)
                self.notice(event, Self.explain(code) + "；稍后自动重试（\(deferred.routeRetries)/3）")
            } else { self.finish(event, Self.explain(code)) }
        }
    }
    private func finish(_ event: ContinuationEvent, _ message: String) {
        active = nil; executing = false
        notice(event, message, cancellable: false, permission: message == Self.explain("permission_required"))
    }

    static func explain(_ code: String) -> String {
        ["session_navigation_unverified": "ZCode 工作区可跳转，但具体会话输入定位尚未核验；未输入、未发送",
         "app_ui_unavailable": "目标应用界面报错或连接已断开，请先恢复该应用",
         "journal_capacity": "防重复会话记录已达容量，本轮保持未发送",
         "ready": "输入组件已就绪", "permission_required": "需要给「任务雷达」开启辅助功能；授权后会重新检查本轮状态",
         "user_active": "你正在操作键盘鼠标，稍后再继续", "locked": "桌面已锁定或会话不可用，解锁后再继续",
         "draft_present": "本次请求未允许覆盖草稿，未发送", "input_changed": "输入回读不一致，已停止；请检查输入框",
         "composer_unreadable": "未找到唯一且可读取的对话输入框", "target_unverified": "无法核验对应会话，未输入文字",
         "session_title_ambiguous": "同一项目有多个同名会话，请先给目标会话改成唯一标题；未输入文字",
         "project_name_ambiguous": "多个工作区名称相同，无法核验目标项目；未输入文字",
         "search_unavailable": "未找到可操作的原生任务搜索入口；未输入会话文字",
         "search_input_changed": "任务搜索框失焦或回读不一致，已停止定位；未输入会话文字",
         "search_result_missing": "原生搜索尚未找到完整标题对应的任务；未输入会话文字",
         "search_result_ambiguous": "原生搜索有多个匹配任务，无法唯一定位；未输入会话文字",
         "sidebar_target_missing": "侧栏中未找到目标会话，请展开对应智能体或更多会话；未输入文字",
         "sidebar_target_ambiguous": "侧栏有多个匹配会话，无法唯一定位；未输入文字",
         "header_unverified": "已导航，但顶部会话标题或项目与目标不一致；未输入文字",
         "window_ambiguous": "存在多个候选窗口，无法核验具体会话", "app_unavailable": "对应应用未运行或存在多个实例",
         "already_running": "会话仍在运行，不需要继续", "state_changed": "任务状态或轮次已变化，未继续发送",
         "focus_changed": "前台窗口发生变化，稍后重新定位", "tree_incomplete": "对话控件读取超时或不完整，本轮不发送",
         "send_unavailable": "没有找到可用的发送按钮；如已填入文字，请手动检查", "send_unconfirmed": "发送动作未得到确认，不会重复点击",
         "input_failed": "快速输入未成功，本轮已停止", "already_attempted": "本轮已经尝试过继续，不重复发送",
         "cooldown": "恢复操作正在冷却，等待后重新检查", "journal_unreadable": "防重复记录不可读，本轮不发送",
         "another_recovery": "另一个恢复操作仍在进行，稍后重试", "user_stopped": "你已主动停止，本轮保持停止",
         "manual_resolution": "需要先处理配额、认证或停止原因，本轮不自动继续", "unsupported": "此应用暂不能可靠自动发送",
         "bridge_unavailable": "自动输入组件未能启动", "bridge_timeout": "自动输入组件超时，结果未知；不会重复尝试",
         "bridge_error": "自动输入组件遇到异常，本轮不再尝试"][code] ?? "自动继续未完成：" + code
    }
}

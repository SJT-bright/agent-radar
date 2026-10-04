import Foundation

struct ContinuationEvent {
    var session: SessionRecord
    let key: String
    let detectedAt: Double
    let canContinue: Bool
    let explanation: String
    var kind = "interrupt"
    var text = "刚才中断了，请继续"
    var delay: Double = 5
    var patience: Double = 600
    var bypassRestriction = false
    var automationMode = "collaboration"
    var manual = false
    var notBefore: Double = 0
    var recoveryDelayReported = false
    var routeRetries = 0
}

/// Only explicit lifecycle evidence arms automatic work. Historical rows are inert.
struct ContinuationPolicy {
    static let resumeText = "刚才中断了，请继续"
    static let grokResumeText = "刚才中断了，请继续未完成的工作；恢复后从真实使用者的角度检查体验，提出可验证的建议并实施优化，完成后说明验证结果。"
    static let manualContinueText = "请继续"

    /// 中断恢复文字按软件解析：专属 > 全局自定义 > 固定默认（grok 保留专属默认）。
    static func resumeText(forApp appID: String, overrides: [String: String], global: String) -> String {
        let trim: (String?) -> String? = { text in
            let trimmed = text?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            return trimmed.isEmpty ? nil : trimmed
        }
        if let custom = trim(overrides[appID]) { return custom }
        if let globalText = trim(global) { return globalText }
        return appID == "grok" ? grokResumeText : resumeText
    }
    private var running: [String: (start: Double, seen: Double)] = [:]
    // A watermark per session survives observation resets; old rounds never re-arm.
    private var announced: [String: [String: Double]] = [:]
    private var attempts: [String: [Double]] = [:]

    static func key(_ row: SessionRecord) -> String {
        row.id + ":" + String(Int64((row.started_at ?? 0) * 1000))
    }
    mutating func resetObservation() { running.removeAll() }
    mutating func forget(_ ids: Set<String>) { for id in ids { running[id] = nil } }

    static func identityRestriction(_ row: SessionRecord) -> String? {
        if row.user_stopped == true { return "你已主动停止，本轮保持停止" }
        guard row.isReadable, ["local-session", "local-db", "local-log"].contains(row.source) else {
            return "只有窗口状态或会话不可读，无法确认具体轮次；请手动检查"
        }
        guard let start = row.started_at, start.isFinite, start > 0,
              ["turn", "last-confirmed"].contains(row.timing_basis ?? "") else {
            return "缺少真实轮次起点，暂不能自动继续"
        }
        return nil
    }
    static func inputRestriction(_ row: SessionRecord) -> String? {
        if row.app_id == "claude-code" { return "终端内的具体标签页尚不能核验，请手动继续" }
        if row.app_id == "autoclaw" && (row.navigation_key == nil || row.navigation_title == nil) {
            return "未找到唯一的 AutoClaw 会话映射，暂不能自动定位"
        }
        if !["codex", "workbuddy", "workbuddy-ai", "zcode", "autoclaw", "grok", "qoder-cn", "qoder"].contains(row.app_id) {
            return "此应用尚未适配可靠的输入与发送定位"
        }
        return nil
    }
    static func restriction(_ row: SessionRecord, allowRateLimit: Bool = false, manual: Bool = false) -> String? {
        if let reason = identityRestriction(row) { return reason }
        let reason = row.statusReason
        if ["用户主动", "用户取消"].contains(where: reason.contains) {
            return "你已主动停止，本轮保持停止"
        }
        if !manual && ["中止事件", "未记录触发者", "触发者待确认"].contains(where: reason.contains) {
            return "无法排除主动停止，本轮只提醒"
        }
        if ["认证", "访问权限", "上下文超过"].contains(where: reason.contains)
            || (!allowRateLimit && isRateLimited(row)) {
            return "需要先解决配额、认证或上下文限制；重复发送不能恢复。" + reason
        }
        return inputRestriction(row)
    }
    static func isRateLimited(_ row: SessionRecord) -> Bool {
        let reason = row.statusReason
        return ["429", "配额", "频率"].contains(where: reason.contains)
            && !["认证", "访问权限", "上下文超过"].contains(where: reason.contains)
    }
    static func followupRestriction(_ row: SessionRecord) -> String? {
        identityRestriction(row) ?? inputRestriction(row)
    }
    static func cleaned(_ text: String?) -> String? {
        guard let trimmed = text?.trimmingCharacters(in: .whitespacesAndNewlines), !trimmed.isEmpty else { return nil }
        return trimmed
    }
    private mutating func claim(_ row: SessionRecord, kind: String) -> Bool {
        guard let start = row.started_at, start > (announced[row.id]?[kind] ?? 0) else { return false }
        announced[row.id, default: [:]][kind] = start
        return true
    }
    /// An explicit click may arm historical completions; startup never calls this.
    mutating func startCompleted(_ rows: [SessionRecord], now: Double, prompt: String, supervisedIDs: Set<String> = []) -> [ContinuationEvent] {
        rows.compactMap { row in
            guard supervisedIDs.contains(row.id), row.status == "completed", Self.followupRestriction(row) == nil,
                  claim(row, kind: "explicit-start") else { return nil }
            return ContinuationEvent(session: row, key: Self.key(row), detectedAt: now, canContinue: true,
                explanation: "已排定本轮持续优化", kind: "followup", text: "", automationMode: "rage")
        }
    }
    /// Used only after a send receipt reveals a reliable newer round between polls.
    mutating func observeConfirmedRound(_ row: SessionRecord, now: Double) {
        guard Self.identityRestriction(row) == nil, let start = row.started_at else { return }
        running[row.id] = (start, now)
    }
    mutating func observe(_ rows: [SessionRecord], now: Double,
                          interruptText: String = resumeText,
                          rateLimitAllowed: Bool = false, rateLimitDelay: Double = 300,
                          rateLimitText: String = "刚才被限流了，现在请继续",
                          followUps: [String: String] = [:],
                          completionMode: String = "collaboration", ragePrompt: String = "",
                          appResumeTexts: [String: String] = [:], rageResumeText: String = "",
                          supervisedIDs: Set<String> = []) -> [ContinuationEvent] {
        var events: [ContinuationEvent] = []
        running = running.filter { now - $0.value.seen < 120 }
        let rage = completionMode == "rage"
        for row in rows {
            if row.status == "running", Self.identityRestriction(row) == nil, let start = row.started_at {
                running[row.id] = (start, now); continue
            }
            // A short collector gap can expose a live ZCode turn as unknown
            // before its terminal database record arrives. Keep only the
            // previously observed turn start; never act on the unknown row.
            // The running baseline expires after 120 seconds above.
            guard row.isReadable else {
                if row.status != "unknown" || row.user_stopped == true ||
                    row.started_at != running[row.id]?.start {
                    running[row.id] = nil
                }
                continue
            }
            guard let old = running[row.id], row.started_at == old.start else { continue }
            if row.status == "completed" {
                running[row.id] = nil
                guard Self.identityRestriction(row) == nil, claim(row, kind: "completed") else { continue }
                let restriction = Self.followupRestriction(row)
                let marked = supervisedIDs.contains(row.id)
                events.append(ContinuationEvent(session: row, key: Self.key(row), detectedAt: now,
                    canContinue: rage && marked && restriction == nil,
                    explanation: rage ? (marked ? (restriction ?? "本轮已结束，继续持续优化") : "本轮已完成；未标记监督，仅提醒") : "这个项目已完结，请验收",
                    kind: rage && marked ? "followup" : "completion", text: "",
                    automationMode: rage ? "rage" : "collaboration"))
                continue
            }
            guard ["interrupted", "stalled"].contains(row.status) else {
                running[row.id] = nil; continue
            }
            if row.status == "stalled" { running[row.id] = (old.start, now) }
            guard claim(row, kind: row.status) else { continue }
            if row.user_stopped == true {
                // 手动停止不算中断：不提醒、不进入恢复队列。
                running[row.id] = nil
                continue
            }
            let rate = row.status == "interrupted" && rateLimitAllowed && Self.isRateLimited(row)
            let restriction = row.status == "stalled" ? "尚无明确中断证据，只提醒，不自动发送" : Self.restriction(row, allowRateLimit: rate)
            let times = (attempts[row.id] ?? []).filter { now - $0 < 3600 }
            let cooldown = !rage && (times.count >= 2 || (times.last.map { now - $0 < 300 } ?? false))
            // A newly verified round is independently recoverable. The durable
            // bridge watermark still prevents a second send to the same round.
            // Rate limits retain their configured wait; speeding up cannot clear them.
            let delay = rate ? rateLimitDelay : (rage ? 1 : 5)
            events.append(ContinuationEvent(session: row, key: Self.key(row), detectedAt: now,
                canContinue: restriction == nil && !cooldown && (!rage || supervisedIDs.contains(row.id)),
                explanation: (rage && !supervisedIDs.contains(row.id) ? "会话未标记监督，本轮只提醒" : nil) ?? restriction ?? (cooldown ? "本会话已触发恢复冷却，先检查连续失败原因" : "检测到本轮意外中断，即将返回原会话继续"),
                text: rage ? Self.resumeText(forApp: row.app_id, overrides: appResumeTexts, global: rageResumeText)
                    : (rate ? rateLimitText : interruptText), delay: delay,
                patience: max(600, delay + 600), bypassRestriction: rate,
                automationMode: rage ? "rage" : "collaboration"))
        }
        return events
    }
    mutating func recordAttempt(_ row: SessionRecord, now: Double) {
        attempts[row.id] = (attempts[row.id] ?? []).filter { now - $0 < 3600 } + [now]
    }
}

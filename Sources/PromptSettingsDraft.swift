import Foundation

/// 全局设置页的待保存字段；即时生效的模式、会话规则由 MonitorStore 持有。
struct PromptSettingsDraft: Equatable {
    var reminderPopupEnabled: Bool
    var reminderSoundEnabled: Bool
    var interruptText: String
    var rateLimitWakeEnabled: Bool
    var rateLimitWaitSeconds: Int
    var rateLimitText: String
    var ragePrompt: String
    var appRagePrompts: [String: String]
    var appResumeTexts: [String: String]
    var rageAlternateEvery: Int
    var rageAlternatePrompt: String

    init(rules: PromptRules, reminderPopupEnabled: Bool = true, reminderSoundEnabled: Bool = true) {
        self.reminderPopupEnabled = reminderPopupEnabled
        self.reminderSoundEnabled = reminderSoundEnabled
        interruptText = rules.interruptText
        rateLimitWakeEnabled = rules.rateLimitWakeEnabled
        rateLimitWaitSeconds = rules.rateLimitWaitSeconds
        rateLimitText = rules.rateLimitText
        ragePrompt = rules.ragePrompt
        // 原样复制 override 字典：草稿只承载显式自定义。留空键在保存时删除。
        // Grok 没有专属续接时使用软件默认，其他软件使用通用续接。
        appRagePrompts = rules.appRagePrompts
        appResumeTexts = rules.appResumeTexts
        rageAlternateEvery = rules.rageAlternateEvery
        rageAlternatePrompt = rules.rageAlternatePrompt
    }

    func hasChanges(comparedTo rules: PromptRules, reminderPopupEnabled: Bool = true,
                    reminderSoundEnabled: Bool = true) -> Bool {
        if self.reminderPopupEnabled != reminderPopupEnabled || self.reminderSoundEnabled != reminderSoundEnabled {
            return true
        }
        // 标量字段比较编辑值（空白/控制字符等未保存修改保持可见）；
        // 字典键只比较本窗口显示/编辑过的应用——别的应用新出现的 override
        // 不会被保存抹掉，也就不该把未编辑的草稿标脏。
        if interruptText != rules.interruptText || rateLimitWakeEnabled != rules.rateLimitWakeEnabled ||
            rateLimitWaitSeconds != rules.rateLimitWaitSeconds || rateLimitText != rules.rateLimitText ||
            ragePrompt != rules.ragePrompt || rageAlternateEvery != rules.rageAlternateEvery ||
            rageAlternatePrompt != rules.rageAlternatePrompt {
            return true
        }
        // 新增空白键不产生覆盖；清空已有专属文字则是待保存的删除，
        // 必须标脏，才能保存并触发关窗时的草稿保护。
        for (appID, text) in appRagePrompts {
            if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                if let saved = rules.appRagePrompts[appID],
                   !saved.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return true }
            } else if text != rules.ragePrompt(forApp: appID) { return true }
        }
        for (appID, text) in appResumeTexts {
            if text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                if let saved = rules.appResumeTexts[appID],
                   !saved.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return true }
            } else if text != (rules.appResumeTexts[appID] ?? "") { return true }
        }
        return false
    }

    var hasOverlengthPrompt: Bool {
        ([interruptText, rateLimitText, ragePrompt, rageAlternatePrompt] + appRagePrompts.values
            + appResumeTexts.values).contains { $0.unicodeScalars.count > PromptRules.maxLength }
    }

    func applying(to current: PromptRules) -> PromptRules {
        var result = current
        result.interruptText = interruptText
        result.rateLimitWakeEnabled = rateLimitWakeEnabled
        result.rateLimitWaitSeconds = rateLimitWaitSeconds
        result.rateLimitText = rateLimitText
        result.ragePrompt = ragePrompt
        // 合并语义：草稿只承载用户在该窗口碰过的应用；未涉及的键原样保留，
        // 窗口打开期间其它应用新出现的 override 不会被保存抹掉。
        // 显式留空值由 normalized() 删除该键。Grok 随后使用软件默认续接，
        // 其他软件使用通用续接。
        for (appID, text) in appRagePrompts {
            result.appRagePrompts[appID] = text
        }
        for (appID, text) in appResumeTexts {
            result.appResumeTexts[appID] = text
        }
        result.rageAlternateEvery = rageAlternateEvery
        result.rageAlternatePrompt = rageAlternatePrompt
        return result.normalized()
    }

    func effectiveContinuation(forApp appID: String, saved: PromptRules) -> PromptRules.EffectivePrompt {
        applying(to: saved).continuationEffect(forApp: appID)
    }

    func effectiveRecovery(forApp appID: String, saved: PromptRules) -> PromptRules.EffectivePrompt {
        applying(to: saved).recoveryEffect(forApp: appID)
    }

    var validationMessages: [String] {
        var messages: [String] = []
        let prompts: [(label: String, text: String, usesDefaultWhenEmpty: Bool)] = [
            ("中断提示词", interruptText, false),
            ("限流提示词", rateLimitText, false),
            ("普通提示词", ragePrompt, true),
            ("特别提示词", rageAlternatePrompt, true)
        ]
        func appendFieldWarnings(label: String, text: String, usesDefaultWhenEmpty: Bool) {
            if text.unicodeScalars.count > PromptRules.maxLength {
                messages.append("\(label)超过 \(PromptRules.maxLength) 字符；完整内容已保留，请删减后保存。")
            }
            if text.unicodeScalars.contains(where: Self.isRemovedControlCharacter) {
                messages.append("\(label)包含控制字符，保存时会移除；换行和制表符会保留。")
            }
            if usesDefaultWhenEmpty && PromptRules.sanitize(text).isEmpty {
                messages.append("\(label)为空或清理后为空，保存时会使用默认提示词。")
            }
        }
        for prompt in prompts {
            appendFieldWarnings(label: prompt.label, text: prompt.text, usesDefaultWhenEmpty: prompt.usesDefaultWhenEmpty)
        }
        for (appID, text) in appRagePrompts.sorted(by: { $0.key < $1.key }) {
            let label = "\(PromptRules.appDisplayNames[appID] ?? appID) 专属提示词"
            appendFieldWarnings(label: label, text: text, usesDefaultWhenEmpty: false)
            if PromptRules.sanitize(text).isEmpty {
                if appID == "grok" {
                    messages.append("\(label)为空或清理后为空，保存后不写入专属覆盖；下次普通续接使用 Grok 软件默认，不使用通用续接提示词。")
                } else {
                    messages.append("\(label)为空或清理后为空，保存后该软件使用通用续接提示词。")
                }
            }
        }
        for (appID, text) in appResumeTexts.sorted(by: { $0.key < $1.key }) {
            appendFieldWarnings(label: "\(PromptRules.appDisplayNames[appID] ?? appID) 恢复提示词",
                                text: text, usesDefaultWhenEmpty: false)
        }
        if !(1...10).contains(rateLimitWaitSeconds) {
            messages.append("限流等待时间须为 1–10 秒，保存时会调整到此范围。")
        }
        if !(2...100).contains(rageAlternateEvery) {
            messages.append("特别提示词间隔须为 2–100 次，保存时会调整到此范围。")
        }
        return messages
    }

    private static func isRemovedControlCharacter(_ scalar: Unicode.Scalar) -> Bool {
        if scalar == "\n" || scalar == "\t" { return false }
        return scalar.value < 32 || scalar.value == 127
    }
}

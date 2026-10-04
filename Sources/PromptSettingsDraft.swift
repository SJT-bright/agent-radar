import Foundation

/// 全局设置页的待保存字段；即时生效的模式、会话规则由 MonitorStore 持有。
struct PromptSettingsDraft: Equatable {
    var interruptText: String
    var rateLimitWakeEnabled: Bool
    var rateLimitWaitMinutes: Int
    var rateLimitText: String
    var ragePrompt: String
    var appRagePrompts: [String: String]
    var appResumeTexts: [String: String]
    var rageAlternateEvery: Int
    var rageAlternatePrompt: String

    init(rules: PromptRules) {
        interruptText = rules.interruptText
        rateLimitWakeEnabled = rules.rateLimitWakeEnabled
        rateLimitWaitMinutes = rules.rateLimitWaitMinutes
        rateLimitText = rules.rateLimitText
        ragePrompt = rules.ragePrompt
        // 原样复制 override 字典：草稿只承载显式自定义，留空表示用通用。
        appRagePrompts = rules.appRagePrompts
        appResumeTexts = rules.appResumeTexts
        rageAlternateEvery = rules.rageAlternateEvery
        rageAlternatePrompt = rules.rageAlternatePrompt
    }

    func hasChanges(comparedTo rules: PromptRules) -> Bool {
        // 标量字段比较编辑值（空白/控制字符等未保存修改保持可见）；
        // 字典键只比较本窗口显示/编辑过的应用——别的应用新出现的 override
        // 不会被保存抹掉，也就不该把未编辑的草稿标脏。
        if interruptText != rules.interruptText || rateLimitWakeEnabled != rules.rateLimitWakeEnabled ||
            rateLimitWaitMinutes != rules.rateLimitWaitMinutes || rateLimitText != rules.rateLimitText ||
            ragePrompt != rules.ragePrompt || rageAlternateEvery != rules.rageAlternateEvery ||
            rageAlternatePrompt != rules.rageAlternatePrompt {
            return true
        }
        // 空白 override 等同于「无 override，回到通用」，不参与标脏判定。
        for (appID, text) in appRagePrompts
            where !text.trimmingCharacters(in: .whitespaces).isEmpty &&
                  text != rules.ragePrompt(forApp: appID) { return true }
        for (appID, text) in appResumeTexts
            where !text.trimmingCharacters(in: .whitespaces).isEmpty &&
                  text != (rules.appResumeTexts[appID] ?? "") { return true }
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
        result.rateLimitWaitMinutes = rateLimitWaitMinutes
        result.rateLimitText = rateLimitText
        result.ragePrompt = ragePrompt
        // 合并语义：草稿只承载用户在该窗口碰过的应用；未涉及的键原样保留，
        // 窗口打开期间其它应用新出现的 override 不会被保存抹掉。
        // 显式留空值由 normalized() 过滤，等于「移除专属、回到通用」。
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

    var validationMessages: [String] {
        var prompts: [(label: String, text: String, usesDefaultWhenEmpty: Bool)] = [
            ("中断提示词", interruptText, false),
            ("限流提示词", rateLimitText, false),
            ("普通提示词", ragePrompt, true),
            ("特别提示词", rageAlternatePrompt, true)
        ]
        for (appID, text) in appRagePrompts.sorted(by: { $0.key < $1.key }) {
            // 专属续接为空会回退通用提示词，与普通提示词同语义，需提示用户。
            prompts.append(("\(PromptRules.appDisplayNames[appID] ?? appID) 专属提示词", text, true))
        }
        for (appID, text) in appResumeTexts.sorted(by: { $0.key < $1.key }) {
            prompts.append(("\(PromptRules.appDisplayNames[appID] ?? appID) 恢复提示词", text, false))
        }
        var messages: [String] = []
        for prompt in prompts {
            if prompt.text.unicodeScalars.count > PromptRules.maxLength {
                messages.append("\(prompt.label)超过 \(PromptRules.maxLength) 字符；完整内容已保留，请删减后保存。")
            }
            if prompt.text.unicodeScalars.contains(where: Self.isRemovedControlCharacter) {
                messages.append("\(prompt.label)包含控制字符，保存时会移除；换行和制表符会保留。")
            }
            if prompt.usesDefaultWhenEmpty && PromptRules.sanitize(prompt.text).isEmpty {
                messages.append("\(prompt.label)为空或清理后为空，保存时会使用默认提示词。")
            }
        }
        if !(1...60).contains(rateLimitWaitMinutes) {
            messages.append("限流等待时间须为 1–60 分钟，保存时会调整到此范围。")
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

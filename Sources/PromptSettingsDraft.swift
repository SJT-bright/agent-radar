import Foundation

/// 全局设置页的待保存字段；即时生效的模式、会话规则由 MonitorStore 持有。
struct PromptSettingsDraft: Equatable {
    var interruptText: String
    var rateLimitWakeEnabled: Bool
    var rateLimitWaitMinutes: Int
    var rateLimitText: String
    var ragePrompt: String
    var grokRagePrompt: String
    var rageAlternateEvery: Int
    var rageAlternatePrompt: String

    init(rules: PromptRules) {
        interruptText = rules.interruptText
        rateLimitWakeEnabled = rules.rateLimitWakeEnabled
        rateLimitWaitMinutes = rules.rateLimitWaitMinutes
        rateLimitText = rules.rateLimitText
        ragePrompt = rules.ragePrompt
        grokRagePrompt = rules.ragePrompt(forApp: "grok")
        rageAlternateEvery = rules.rageAlternateEvery
        rageAlternatePrompt = rules.rageAlternatePrompt
    }

    func hasChanges(comparedTo rules: PromptRules) -> Bool {
        // 比较编辑值而非清理后的值，让空白、超长等尚未保存的修改仍可撤销。
        // init 使用 Grok 的实际默认文字，不把隐式默认与等价显式值视为修改。
        self != Self(rules: rules)
    }

    var hasOverlengthPrompt: Bool {
        [interruptText, rateLimitText, ragePrompt, grokRagePrompt, rageAlternatePrompt]
            .contains { $0.unicodeScalars.count > PromptRules.maxLength }
    }

    func applying(to current: PromptRules) -> PromptRules {
        var result = current
        result.interruptText = interruptText
        result.rateLimitWakeEnabled = rateLimitWakeEnabled
        result.rateLimitWaitMinutes = rateLimitWaitMinutes
        result.rateLimitText = rateLimitText
        result.ragePrompt = ragePrompt
        if current.appRagePrompts["grok"] != nil || grokRagePrompt != current.ragePrompt(forApp: "grok") {
            result.appRagePrompts["grok"] = grokRagePrompt
        }
        result.rageAlternateEvery = rageAlternateEvery
        result.rageAlternatePrompt = rageAlternatePrompt
        return result.normalized()
    }

    var validationMessages: [String] {
        let prompts: [(label: String, text: String, usesDefaultWhenEmpty: Bool)] = [
            ("中断提示词", interruptText, false),
            ("限流提示词", rateLimitText, false),
            ("普通提示词", ragePrompt, true),
            ("Grok 普通提示词", grokRagePrompt, true),
            ("特别提示词", rageAlternatePrompt, true)
        ]
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

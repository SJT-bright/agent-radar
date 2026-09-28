import Foundation

@main struct PromptModeChecks {
    static func main() throws {
        var count = 0
        func check(_ value: Bool, _ name: String) {
            precondition(value, name)
            count += 1
        }
        func decode(_ json: String) throws -> PromptRules {
            try JSONDecoder().decode(PromptRules.self, from: Data(json.utf8))
        }
        let old = try decode(#"{"interruptText":"旧中断提示","rateLimitWakeEnabled":true,"rateLimitWaitMinutes":12,"rateLimitText":"旧限流提示","followUps":{"app:one":"旧会话规则"}}"#)
        check(old.completionMode == "collaboration", "upgrade defaults to collaboration")
        check(old.ragePrompt == PromptRules.defaultRagePrompt, "upgrade gets useful optimization prompt")
        check(old.interruptText == "旧中断提示" && old.rateLimitText == "旧限流提示", "upgrade preserves legacy custom prompts")
        check(old.followUps == ["app:one": "旧会话规则"], "upgrade preserves session rules")
        check(old.rateLimitWakeEnabled && old.rateLimitWaitMinutes == 12, "upgrade preserves rate limit settings")
        check(try decode("{}") == PromptRules(), "empty old record receives defaults")
        check(try decode(#"{"completionMode":"invalid"}"#).completionMode == "collaboration", "unknown modes fail to reminder mode")
        check(try decode(#"{"completionMode":null,"ragePrompt":null}"#).completionMode == "collaboration", "null fields use safe defaults")
        check(try decode(#"{"completionMode":"rage"}"#).completionMode == "rage", "explicit rage value survives decode")
        let previousDefault = "从使用者角度检查当前项目，提出可验证的优化方案；针对方案仔细调研，反复审查其必要性、可行性与风险；按最终方案实施改进并验证结果，说明证据和剩余问题。"
        check(try decode(String(data: JSONSerialization.data(withJSONObject: ["ragePrompt": previousDefault]), encoding: .utf8)!).ragePrompt == PromptRules.defaultRagePrompt, "previous default upgrades to contextual optimization")
        check(try decode(#"{"ragePrompt":"用户自定义优化提示"}"#).ragePrompt == "用户自定义优化提示", "custom optimization prompt survives migration")
        check(try decode(#"{"ragePrompt":"   \n\t"}"#).ragePrompt == PromptRules.defaultRagePrompt, "empty optimization text restored")
        check(try decode(#"{"rateLimitWaitMinutes":-10}"#).rateLimitWaitMinutes == 1, "negative wait clamped")
        check(try decode(#"{"rateLimitWaitMinutes":1000}"#).rateLimitWaitMinutes == 60, "long wait clamped")
        check(PromptRules.sanitize(" \u{0}优\u{7}化\n\t继续\u{7F} ") == "优化\n\t继续", "control characters removed with line breaks and tabs retained")
        let long = String(repeating: "👩🏽‍💻", count: 2001)
        check(PromptRules.sanitize(long).unicodeScalars.count == 2000 && PromptRules.sanitize(long).count == 500 && PromptRules.sanitize(long).last == "👩🏽‍💻", "limit retains complete characters")
        let suite = "local.agentradar.prompt-tests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        check(PromptRules.load(from: defaults) == PromptRules(), "missing persisted record defaults")
        defaults.set(Data("broken".utf8), forKey: PromptRules.defaultsKey)
        check(PromptRules.load(from: defaults) == PromptRules(), "corrupt persisted record defaults")
        var rules = old
        rules.completionMode = "rage"
        rules.ragePrompt = "  独立优化\u{0}  "
        rules.save(to: defaults)
        let saved = PromptRules.load(from: defaults)
        check(saved.completionMode == "rage" && saved.ragePrompt == "独立优化", "persistent rage settings normalize and reload")
        check(saved.followUps == old.followUps, "mode changes retain old followups")
        check(saved.rateLimitWakeEnabled == old.rateLimitWakeEnabled, "persist preserves rate settings")
        rules.completionMode = "unsupported"
        rules.ragePrompt = " "
        rules.save(to: defaults)
        check(PromptRules.load(from: defaults).completionMode == "collaboration", "save also normalizes invalid mode")
        check(PromptRules.load(from: defaults).ragePrompt == PromptRules.defaultRagePrompt, "save also normalizes empty prompt")
        check(defaults.object(forKey: "autoContinueOptIn.v2") == nil, "saving mode never grants automatic sending")
        let encoded = try JSONEncoder().encode(saved)
        check(try JSONDecoder().decode(PromptRules.self, from: encoded) == saved, "full roundtrip equality")
        print("PromptRules mode checks: \(count) passed")
    }
}

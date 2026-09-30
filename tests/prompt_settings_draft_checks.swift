import Foundation

@main struct PromptSettingsDraftChecks {
    static func main() {
        var count = 0
        func check(_ value: Bool, _ name: String) {
            precondition(value, name)
            count += 1
        }
        func hasWarning(_ draft: PromptSettingsDraft, containing text: String) -> Bool {
            draft.validationMessages.contains { $0.contains(text) }
        }

        let original = PromptRules()
        let untouched = PromptSettingsDraft(rules: original)
        check(!untouched.hasChanges(comparedTo: original), "opening settings has no unsaved changes")
        check(untouched.validationMessages.isEmpty, "default prompts require no cleanup warnings")
        check(untouched.applying(to: original) == original,
              "saving an unchanged draft does not create an override or cancel pending work")
        check(untouched.grokRagePrompt == PromptRules.defaultGrokRagePrompt,
              "an absent Grok override displays the effective default")

        var latest = original
        latest.completionMode = "rage"
        latest.followUps = ["codex:new": "新会话提示词"]
        latest.appRagePrompts = ["claude": "Claude 自定义", "future-app": "未知应用自定义"]
        check(!untouched.hasChanges(comparedTo: latest),
              "immediate mode and unrelated rules do not mark a draft dirty")
        latest.appRagePrompts["grok"] = PromptRules.defaultGrokRagePrompt
        check(!untouched.hasChanges(comparedTo: latest),
              "implicit and explicit Grok defaults are equivalent")
        check(!PromptSettingsDraft(rules: latest).hasChanges(comparedTo: original),
              "Grok default equivalence works in both directions")
        latest.appRagePrompts["grok"] = "新 Grok 提示词"
        check(untouched.hasChanges(comparedTo: latest), "a changed effective Grok prompt is detected")

        let changes: [(String, (inout PromptSettingsDraft) -> Void)] = [
            ("interrupt text", { $0.interruptText = "新的中断提示词" }),
            ("rate-limit enabled", { $0.rateLimitWakeEnabled.toggle() }),
            ("wait minutes", { $0.rateLimitWaitMinutes = 12 }),
            ("rate-limit text", { $0.rateLimitText = "新的限流提示词" }),
            ("normal prompt", { $0.ragePrompt = "新的普通提示词" }),
            ("Grok prompt", { $0.grokRagePrompt = "新的 Grok 提示词" }),
            ("special interval", { $0.rageAlternateEvery = 7 }),
            ("special prompt", { $0.rageAlternatePrompt = "新的特别提示词" })
        ]
        for (name, edit) in changes {
            var draft = untouched
            edit(&draft)
            check(draft.hasChanges(comparedTo: original), "editing \(name) is dirty")
            check(draft != untouched, "Equatable includes \(name)")
            draft = PromptSettingsDraft(rules: original)
            check(!draft.hasChanges(comparedTo: original), "reset discards \(name)")
        }

        var edited = untouched
        edited.interruptText = "  恢复\u{0}工作  "
        edited.rateLimitWakeEnabled = true
        edited.rateLimitWaitMinutes = 12
        edited.rateLimitText = "稍后继续"
        edited.ragePrompt = "完成当前改进"
        edited.grokRagePrompt = "Grok 继续工作"
        edited.rageAlternateEvery = 5
        edited.rageAlternatePrompt = "从用户角度走查"
        let saved = edited.applying(to: latest)
        check(saved.completionMode == "rage", "save preserves the latest immediate mode")
        check(saved.followUps == latest.followUps, "save preserves the latest session rules")
        check(saved.appRagePrompts["claude"] == "Claude 自定义" &&
              saved.appRagePrompts["future-app"] == "未知应用自定义",
              "save preserves known and unknown application overrides")
        check(saved.appRagePrompts["grok"] == "Grok 继续工作", "save updates only the owned Grok override")
        check(saved.interruptText == "恢复工作" && saved.rateLimitText == "稍后继续",
              "save normalizes edited interruption prompts")
        check(saved.rateLimitWakeEnabled && saved.rateLimitWaitMinutes == 12,
              "save applies rate-limit controls")
        check(saved.ragePrompt == "完成当前改进" && saved.rageAlternateEvery == 5 &&
              saved.rageAlternatePrompt == "从用户角度走查", "save applies continuation settings")
        check(latest.interruptText == original.interruptText && latest.appRagePrompts["grok"] == "新 Grok 提示词",
              "applying a draft does not mutate the supplied rules")
        check(edited.hasChanges(comparedTo: saved), "cleaned input remains visible until the UI resets the draft")
        let refreshed = PromptSettingsDraft(rules: saved)
        check(!refreshed.hasChanges(comparedTo: saved), "reload after save clears dirty state")

        var empty = untouched
        empty.interruptText = ""
        empty.rateLimitText = " \n\t "
        check(empty.validationMessages.isEmpty, "empty interruption prompts retain their existing allowed semantics")
        let emptySaved = empty.applying(to: original)
        check(emptySaved.interruptText.isEmpty && emptySaved.rateLimitText.isEmpty,
              "empty interruption prompts stay empty on save")
        empty.ragePrompt = " \n "
        empty.grokRagePrompt = "\u{0}\u{7F}"
        empty.rageAlternatePrompt = "\t"
        check(empty.validationMessages.filter { $0.contains("默认提示词") }.count == 3,
              "all three empty continuation prompts explain their default fallback")
        check(hasWarning(empty, containing: "Grok 普通提示词包含控制字符"),
              "a prompt emptied by control cleanup reports both causes")
        let defaultSaved = empty.applying(to: original)
        check(defaultSaved.ragePrompt == PromptRules.defaultRagePrompt &&
              defaultSaved.ragePrompt(forApp: "grok") == PromptRules.defaultGrokRagePrompt &&
              defaultSaved.rageAlternatePrompt == PromptRules.defaultRageAlternatePrompt,
              "empty continuation prompts use their matching defaults")

        var controls = untouched
        controls.interruptText = "首行\n\t次行"
        check(controls.validationMessages.isEmpty, "line breaks and tabs do not produce control warnings")
        check(controls.applying(to: original).interruptText == controls.interruptText,
              "allowed line breaks and tabs survive save")
        controls.interruptText = "前\u{0}\r\u{1F}\u{7F}后"
        check(hasWarning(controls, containing: "中断提示词包含控制字符"),
              "removed C0 and DEL characters are identified before save")
        check(controls.applying(to: original).interruptText == "前后", "validation matches actual control cleanup")

        var unicode = untouched
        let emoji = "👩🏽‍💻"
        unicode.ragePrompt = String(repeating: emoji, count: 500)
        check(unicode.ragePrompt.unicodeScalars.count == PromptRules.maxLength &&
              unicode.validationMessages.isEmpty, "exact Unicode scalar limit is accepted")
        check(unicode.applying(to: original).ragePrompt == unicode.ragePrompt,
              "exact-limit multiscalar graphemes survive unchanged")
        unicode.ragePrompt.append(emoji)
        check(hasWarning(unicode, containing: "普通提示词超过 2000 字符"),
              "overlength warning uses the sender's Unicode scalar limit")
        check(unicode.hasOverlengthPrompt && unicode.ragePrompt == String(repeating: emoji, count: 501),
              "overlength pasted text stays intact in the editable draft")
        unicode.ragePrompt = String(repeating: "字", count: 2300)
        check(unicode.hasOverlengthPrompt && unicode.ragePrompt.unicodeScalars.count == 2300,
              "a 2300-character paste remains editable until the user shortens it")
        unicode.ragePrompt = String(repeating: "字", count: 2000)
        check(!unicode.hasOverlengthPrompt, "save is available once the draft reaches the limit")
        unicode.ragePrompt = String(repeating: emoji, count: 501)
        let truncatedEmoji = unicode.applying(to: original).ragePrompt
        check(truncatedEmoji == String(repeating: emoji, count: 500),
              "overlength emoji input truncates only at complete grapheme boundaries")
        unicode.ragePrompt = String(repeating: "a", count: 1999) + emoji
        check(unicode.applying(to: original).ragePrompt == String(repeating: "a", count: 1999),
              "a grapheme crossing the limit is removed in full")
        unicode.ragePrompt = String(repeating: "e\u{301}", count: 1000)
        check(unicode.validationMessages.isEmpty && unicode.applying(to: original).ragePrompt.count == 1000,
              "combining marks at the boundary remain attached to their base characters")
        unicode.ragePrompt = "a" + String(repeating: "\u{301}", count: 2000)
        check(hasWarning(unicode, containing: "默认提示词"),
              "a single overlength grapheme explains the empty fallback after truncation")
        check(unicode.applying(to: original).ragePrompt == PromptRules.defaultRagePrompt,
              "an unkeepable grapheme uses the same normalized fallback as PromptRules")

        var bounds = untouched
        bounds.rateLimitWaitMinutes = 0
        bounds.rageAlternateEvery = 1
        check(bounds.validationMessages.count == 2, "out-of-range numeric controls explain normalization")
        check(bounds.applying(to: original).rateLimitWaitMinutes == 1 &&
              bounds.applying(to: original).rageAlternateEvery == 2, "numeric lower bounds match persisted rules")
        bounds.rateLimitWaitMinutes = 61
        bounds.rageAlternateEvery = 101
        check(bounds.applying(to: original).rateLimitWaitMinutes == 60 &&
              bounds.applying(to: original).rageAlternateEvery == 100, "numeric upper bounds match persisted rules")

        print("Prompt settings draft checks: \(count) passed")
    }
}

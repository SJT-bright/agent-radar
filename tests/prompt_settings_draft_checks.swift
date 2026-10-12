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
        check(untouched.appRagePrompts["grok"] == nil,
              "an absent override is not shown as a filled field")
        check(untouched.reminderPopupEnabled && untouched.reminderSoundEnabled,
              "old settings drafts default to both popup and sound enabled")
        let disabledReminders = PromptSettingsDraft(rules: original, reminderPopupEnabled: false,
                                                   reminderSoundEnabled: false)
        check(!disabledReminders.hasChanges(comparedTo: original, reminderPopupEnabled: false,
                                           reminderSoundEnabled: false),
              "loading saved disabled reminders does not mark the draft dirty")
        check(disabledReminders.applying(to: original) == original,
              "reminder preferences do not become continuation execution rules")
        var soundOnly = disabledReminders
        soundOnly.reminderSoundEnabled = true
        check(soundOnly.hasChanges(comparedTo: original, reminderPopupEnabled: false,
                                   reminderSoundEnabled: false) && !soundOnly.reminderPopupEnabled,
              "sound can be enabled independently while popups remain disabled")
        check(!soundOnly.hasChanges(comparedTo: original, reminderPopupEnabled: false,
                                    reminderSoundEnabled: true),
              "reloading a saved sound-only preference clears dirty state")

        var latest = original
        latest.completionMode = "rage"
        latest.followUps = ["codex:new": "新会话提示词"]
        latest.appRagePrompts = ["claude": "Claude 自定义", "future-app": "未知应用自定义"]
        check(!untouched.hasChanges(comparedTo: latest),
              "immediate mode and unrelated rules do not mark a draft dirty")
        latest.appRagePrompts["grok"] = ""
        check(!untouched.hasChanges(comparedTo: latest),
              "implicit and explicit empty Grok overrides are equivalent")
        // 反向：draft 持有 grok=""（显式清空）对 original 判定不脏，
        // normalized 后 grok 键被过滤，保存不会产生任何覆盖。
        // （用干净上下文：此时 latest 已带 claude/future-app 覆盖，会干扰。）
        var emptyOverrideRules = original
        emptyOverrideRules.appRagePrompts["grok"] = ""
        check(!PromptSettingsDraft(rules: emptyOverrideRules).hasChanges(comparedTo: original),
              "empty override equivalence works in both directions")
        latest.appRagePrompts["grok"] = "外部新出现的提示词"
        // 别的窗口/会话新出现的 override 不属于本草稿：保存不会抹掉它们，
        // 也就不该把未编辑的草稿标脏；用户在草稿里编辑过的键才参与判定。
        check(!untouched.hasChanges(comparedTo: latest), "an outside override does not mark the draft dirty")
        var editedGrok = untouched
        editedGrok.appRagePrompts["grok"] = "我编辑的提示词"
        check(editedGrok.hasChanges(comparedTo: latest), "editing the draft's own Grok override is dirty")
        var withResume = untouched
        withResume.appResumeTexts["grok"] = "新的 Grok 恢复提示词"
        check(withResume.hasChanges(comparedTo: latest), "a per-app resume override is detected")

        var withSavedOverrides = original
        withSavedOverrides.appRagePrompts = ["codex": "Saved Codex continuation", "qoder": "Saved Qoder continuation"]
        withSavedOverrides.appResumeTexts = ["codex": "Saved Codex recovery", "qoder": "Saved Qoder recovery"]
        let savedDraft = PromptSettingsDraft(rules: withSavedOverrides)
        check(!savedDraft.hasChanges(comparedTo: withSavedOverrides),
              "loading existing per-app overrides does not mark the draft dirty")
        for blank in ["", " \t\n "] {
            var clearedRage = savedDraft
            clearedRage.appRagePrompts["codex"] = blank
            check(clearedRage.hasChanges(comparedTo: withSavedOverrides),
                  "clearing an existing continuation override enables save and close protection")
            var clearedResume = savedDraft
            clearedResume.appResumeTexts["codex"] = blank
            check(clearedResume.hasChanges(comparedTo: withSavedOverrides),
                  "clearing an existing recovery override enables save and close protection")
            var addedBlank = untouched
            addedBlank.appRagePrompts["codex"] = blank
            addedBlank.appResumeTexts["codex"] = blank
            check(!addedBlank.hasChanges(comparedTo: original),
                  "adding empty or whitespace-only overrides without saved values remains unchanged")
        }
        var clearedBoth = savedDraft
        clearedBoth.appRagePrompts["codex"] = ""
        clearedBoth.appResumeTexts["codex"] = ""
        var currentWithOutsideChanges = withSavedOverrides
        currentWithOutsideChanges.appRagePrompts["future-app"] = "Outside continuation"
        currentWithOutsideChanges.appResumeTexts["future-app"] = "Outside recovery"
        let clearedSaved = clearedBoth.applying(to: currentWithOutsideChanges)
        check(clearedSaved.appRagePrompts["codex"] == nil && clearedSaved.appResumeTexts["codex"] == nil,
              "saving cleared overrides removes both saved Codex keys")
        check(clearedSaved.appRagePrompts["qoder"] == "Saved Qoder continuation" &&
              clearedSaved.appResumeTexts["qoder"] == "Saved Qoder recovery" &&
              clearedSaved.appRagePrompts["future-app"] == "Outside continuation" &&
              clearedSaved.appResumeTexts["future-app"] == "Outside recovery",
              "saving cleared overrides preserves unrelated and newly appeared app keys")
        check(!PromptSettingsDraft(rules: clearedSaved).hasChanges(comparedTo: clearedSaved),
              "reloading a saved override deletion clears dirty state")

        let changes: [(String, (inout PromptSettingsDraft) -> Void)] = [
            ("reminder popup", { $0.reminderPopupEnabled.toggle() }),
            ("reminder sound", { $0.reminderSoundEnabled.toggle() }),
            ("interrupt text", { $0.interruptText = "新的中断提示词" }),
            ("rate-limit enabled", { $0.rateLimitWakeEnabled.toggle() }),
            ("wait seconds", { $0.rateLimitWaitSeconds = 8 }),
            ("rate-limit text", { $0.rateLimitText = "新的限流提示词" }),
            ("normal prompt", { $0.ragePrompt = "新的普通提示词" }),
            ("Grok prompt", { $0.appRagePrompts["grok"] = "新的 Grok 提示词" }),
            ("Grok resume prompt", { $0.appResumeTexts["grok"] = "新的 Grok 恢复提示词" }),
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
        edited.rateLimitWaitSeconds = 8
        edited.rateLimitText = "稍后继续"
        edited.ragePrompt = "完成当前改进"
        edited.appRagePrompts = ["grok": "Grok 继续工作"]
        edited.rageAlternateEvery = 5
        edited.rageAlternatePrompt = "从用户角度走查"
        let saved = edited.applying(to: latest)
        check(saved.completionMode == "rage", "save preserves the latest immediate mode")
        check(saved.followUps == latest.followUps, "save preserves the latest session rules")
        check(saved.appRagePrompts["claude"] == "Claude 自定义" &&
              saved.appRagePrompts["future-app"] == "未知应用自定义",
              "save preserves known and unknown application overrides")
        check(saved.appRagePrompts["grok"] == "Grok 继续工作", "save updates only the owned Grok override")
        check(saved.appResumeTexts.isEmpty, "resume overrides absent from the draft stay absent on save")
        check(saved.interruptText == "恢复工作" && saved.rateLimitText == "稍后继续",
              "save normalizes edited interruption prompts")
        check(saved.rateLimitWakeEnabled && saved.rateLimitWaitSeconds == 8,
              "save applies rate-limit controls")
        check(saved.ragePrompt == "完成当前改进" && saved.rageAlternateEvery == 5 &&
              saved.rageAlternatePrompt == "从用户角度走查", "save applies continuation settings")
        check(latest.interruptText == original.interruptText && latest.appRagePrompts["grok"] == "外部新出现的提示词",
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
        empty.appRagePrompts["grok"] = "\u{0}\u{7F}"
        empty.rageAlternatePrompt = "\t"
        check(empty.validationMessages.filter { $0.contains("使用默认提示词") }.count == 2,
              "empty generic continuation and alternate prompts still explain their default fallback")
        check(hasWarning(empty, containing: "Grok 专属提示词为空或清理后为空，保存后不写入专属覆盖；下次普通续接使用 Grok 软件默认，不使用通用续接提示词。"),
              "empty Grok continuation names the software default")
        check(!empty.validationMessages.contains { $0.contains("Grok 专属提示词") && $0.contains("使用默认提示词") },
              "empty Grok continuation is not described as the generic default")
        check(hasWarning(empty, containing: "Grok 专属提示词包含控制字符"),
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
        bounds.rateLimitWaitSeconds = 0
        bounds.rageAlternateEvery = 1
        check(bounds.validationMessages.count == 2, "out-of-range numeric controls explain normalization")
        check(bounds.applying(to: original).rateLimitWaitSeconds == 1 &&
              bounds.applying(to: original).rageAlternateEvery == 2, "numeric lower bounds match persisted rules")
        bounds.rateLimitWaitSeconds = 61
        bounds.rageAlternateEvery = 101
        check(bounds.applying(to: original).rateLimitWaitSeconds == 10 &&
              bounds.applying(to: original).rageAlternateEvery == 100, "numeric upper bounds match persisted rules")

        let fresh = PromptRules()
        let grokContinuation = fresh.continuationEffect(forApp: "grok")
        check(grokContinuation.text == fresh.ragePrompt(forApp: "grok") &&
              grokContinuation.text == PromptRules.defaultGrokRagePrompt &&
              grokContinuation.text != fresh.ragePrompt &&
              grokContinuation.source == .softwareDefault &&
              grokContinuation.sourceLabel == "Grok 软件默认",
              "an empty Grok continuation field resolves to the Grok software default")
        let codexContinuation = fresh.continuationEffect(forApp: "codex")
        check(codexContinuation.text == fresh.ragePrompt(forApp: "codex") &&
              codexContinuation.text == PromptRules.defaultRagePrompt &&
              codexContinuation.source == .generic && codexContinuation.sourceLabel == "通用默认",
              "other apps with no override use the generic continuation")
        check(fresh.genericContinuationEffect().text == fresh.ragePrompt &&
              fresh.genericContinuationEffect().sourceLabel == "通用默认",
              "the generic field reports its own built-in text")
        check(PromptRules.continuationFieldDetail(forApp: "grok").contains("Grok 软件默认") &&
              PromptRules.continuationFieldDetail(forApp: "grok").contains("不使用上方的通用续接提示词") &&
              !PromptRules.continuationFieldDetail(forApp: "grok").contains("留空使用通用提示词") &&
              PromptRules.genericContinuationDetail.contains("Grok 软件默认"),
              "Grok field copy no longer says an empty override uses the generic prompt")
        check(PromptRules.continuationFieldDetail(forApp: "codex").contains("通用续接提示词") &&
              !PromptRules.continuationFieldDetail(forApp: "codex").contains("Grok 软件默认"),
              "non-Grok empty continuation still explains the generic prompt")

        var dedicated = fresh
        dedicated.ragePrompt = "通用续接正文"
        dedicated.appRagePrompts["grok"] = "Grok 专属正文"
        dedicated.appRagePrompts["codex"] = "Codex 专属正文"
        check(dedicated.continuationEffect(forApp: "grok").text == dedicated.ragePrompt(forApp: "grok") &&
              dedicated.continuationEffect(forApp: "grok").source == .dedicated &&
              dedicated.continuationEffect(forApp: "grok").text == "Grok 专属正文",
              "a saved Grok override stays the authorized continuation text")
        check(dedicated.continuationEffect(forApp: "codex").source == .dedicated &&
              dedicated.continuationEffect(forApp: "codex").text == "Codex 专属正文",
              "a saved Codex override stays dedicated")
        check(dedicated.continuationEffect(forApp: "qoder").source == .generic &&
              dedicated.continuationEffect(forApp: "qoder").sourceLabel == "通用" &&
              dedicated.continuationEffect(forApp: "qoder").text == "通用续接正文",
              "a customized generic prompt is the source for apps without an override")
        var blankDedicated = dedicated
        blankDedicated.appRagePrompts["grok"] = ""
        check(blankDedicated.ragePrompt(forApp: "grok") == "" &&
              blankDedicated.continuationEffect(forApp: "grok").source == .dedicated,
              "a stored empty Grok key still follows ragePrompt and is not rewritten")

        let grokRecovery = fresh.recoveryEffect(forApp: "grok")
        let codexRecovery = fresh.recoveryEffect(forApp: "codex")
        check(grokRecovery.source == .softwareDefault && grokRecovery.sourceLabel == "Grok 软件默认" &&
              grokRecovery.text == PromptRules.grokSoftwareResumeText,
              "Grok recovery with no override uses the Grok software default")
        check(codexRecovery.source == .fixedDefault && codexRecovery.sourceLabel == "固定默认" &&
              codexRecovery.text == PromptRules.fixedResumeText &&
              PromptRules.fixedResumeText == "刚才中断了，请继续",
              "other apps recover with the fixed default when no generic recovery is set")
        check(PromptRules.recoveryFieldDetail(forApp: "grok").contains("Grok 软件默认") &&
              PromptRules.recoveryFieldDetail(forApp: "codex").contains("固定默认"),
              "recovery field copy names the software default and the fixed default")
        var genericRecovery = fresh
        genericRecovery.resumePrompt = "  通用恢复正文  "
        check(genericRecovery.recoveryEffect(forApp: "grok").source == .generic &&
              genericRecovery.recoveryEffect(forApp: "grok").sourceLabel == "通用恢复" &&
              genericRecovery.recoveryEffect(forApp: "grok").text == "通用恢复正文" &&
              genericRecovery.recoveryEffect(forApp: "codex").text == "通用恢复正文",
              "a generic recovery prompt wins over both software defaults")
        genericRecovery.appResumeTexts["grok"] = " Grok 专属恢复 "
        genericRecovery.appResumeTexts["codex"] = " \n\t "
        check(genericRecovery.recoveryEffect(forApp: "grok").source == .dedicated &&
              genericRecovery.recoveryEffect(forApp: "grok").text == "Grok 专属恢复",
              "a dedicated recovery prompt wins over the generic recovery")
        check(genericRecovery.recoveryEffect(forApp: "codex").source == .generic &&
              genericRecovery.recoveryEffect(forApp: "codex").text == "通用恢复正文",
              "whitespace-only dedicated recovery falls through to the generic recovery")
        check(fresh.alternateEffect().text == PromptRules.defaultRageAlternatePrompt &&
              fresh.alternateEffect().text != PromptRules.defaultGrokRagePrompt &&
              fresh.alternateEffect().source == .generic,
              "the special turn, including Grok, uses the alternate prompt")
        check(fresh.interruptEffect().text == "刚才中断了，请继续" && fresh.interruptEffect().source == .configured &&
              fresh.rateLimitEffect().text == "刚才被限流了，现在请继续" && fresh.rateLimitEffect().source == .configured,
              "collaboration interrupt and rate-limit text keep their configured wording")
        var emptiedLimits = fresh
        emptiedLimits.interruptText = ""
        emptiedLimits.rateLimitText = ""
        check(emptiedLimits.interruptEffect().text.isEmpty && emptiedLimits.rateLimitEffect().text.isEmpty,
              "empty interrupt and rate-limit text stay empty instead of borrowing another prompt")

        var clearGrok = PromptSettingsDraft(rules: dedicated)
        clearGrok.appRagePrompts["grok"] = " \n "
        clearGrok.appResumeTexts["grok"] = ""
        let preview = clearGrok.applying(to: dedicated)
        check(preview.continuationEffect(forApp: "grok").source == .softwareDefault &&
              preview.continuationEffect(forApp: "grok").text == PromptRules.defaultGrokRagePrompt &&
              preview.ragePrompt == "通用续接正文" &&
              preview.appRagePrompts["codex"] == "Codex 专属正文",
              "clearing only the Grok continuation returns to the software default")
        check(clearGrok.effectiveContinuation(forApp: "grok", saved: dedicated).text == PromptRules.defaultGrokRagePrompt &&
              clearGrok.effectiveRecovery(forApp: "grok", saved: dedicated).source == .softwareDefault &&
              clearGrok.effectiveRecovery(forApp: "grok", saved: dedicated).text == PromptRules.grokSoftwareResumeText &&
              dedicated.appRagePrompts["grok"] == "Grok 专属正文",
              "the draft preview does not change the already authorized Grok prompt")
        var clearCodex = PromptSettingsDraft(rules: dedicated)
        clearCodex.appRagePrompts["codex"] = ""
        check(hasWarning(clearCodex, containing: "Codex 专属提示词为空或清理后为空，保存后该软件使用通用续接提示词。"),
              "clearing a non-Grok continuation explains the generic prompt")
        check(clearCodex.effectiveContinuation(forApp: "codex", saved: dedicated).source == .generic &&
              clearCodex.effectiveContinuation(forApp: "codex", saved: dedicated).text == "通用续接正文" &&
              clearCodex.effectiveContinuation(forApp: "qoder", saved: dedicated).text == "通用续接正文",
              "clearing Codex uses the generic continuation and leaves other apps unchanged")

        print("Prompt settings draft checks: \(count) passed")
    }
}

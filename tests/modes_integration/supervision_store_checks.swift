import Foundation
@main struct SupervisionStoreChecks {
    static func main() {
        var count = 0
        func check(_ ok: Bool, _ name: String) { precondition(ok, name); count += 1 }
        let suite = "local.agentradar.supervision-tests." + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        func makeStore() -> MonitorStore { MonitorStore(defaults: defaults, writeHealthDiagnostics: false) }
        func row(_ id: String, _ status: String = "running", app: String = "workbuddy") -> SessionRecord {
            SessionRecord(id: id, app_id: app, app_name: app, title: "完整项目标题 " + id,
                          project: "/isolated", status: status, evidence: "local", updated_at: 100,
                          source: "local-session", target: "workbuddy://chat/" + id,
                          started_at: 90, timing_basis: "turn")
        }
        let store = makeStore()
        check(store.supervisedSessionIDs.isEmpty, "upgrade defaults to no selected sessions")
        let a = row("a"), b = row("b"), done = row("done", "completed"), waiting = row("waiting", "waiting")
        let unknown = row("unknown", "unknown"), alternate = row("other", app: "grok")
        store.sessions = [a, b, done, waiting, unknown, alternate]
        // 多对话框：等待/停滞常驻可选，待确认（陈旧）不入选，避免历史噪音。
        check(Set(store.supervisionGroups(includeCompleted: false).flatMap { $0.sessions.map(\.id) }) == Set(["a", "b", "waiting", "other"]), "running and waiting selectable, stale unknown not")
        check(store.supervisionGroups(includeCompleted: false).count == 2, "candidates grouped by app")
        check(store.supervisionGroups(includeCompleted: true).flatMap { $0.sessions }.contains { $0.id == done.id }, "completed opt in listing")
        // 同一应用的第二条、第三条对话框必须与第一条一起出现并可勾选。
        var secondRun = row("b2", app: "workbuddy"); secondRun.updated_at = Date().timeIntervalSince1970
        var recentInterrupted = row("intr", "interrupted"); recentInterrupted.updated_at = Date().timeIntervalSince1970
        var staleInterrupted = row("intr-old", "interrupted"); staleInterrupted.updated_at = 100
        store.sessions = [a, b, secondRun, recentInterrupted, staleInterrupted, done, waiting, unknown, alternate]
        let listed = store.supervisionGroups(includeCompleted: false).flatMap { $0.sessions.map(\.id) }
        check(listed.contains("b2"), "same-app second conversation selectable")
        check(listed.contains("intr"), "recent interrupted conversation selectable")
        check(!listed.contains("intr-old"), "stale interrupted conversation not listed")
        let workbuddyGroup = store.supervisionGroups(includeCompleted: false).first { $0.primary.app_name == "workbuddy" }
        check(workbuddyGroup?.sessions.count == 5, "one app lists every dialog together")
        store.setAppSupervision(true, appID: "workbuddy")
        store.setAppSupervision(true, appID: "grok")
        check(store.supervisedAppIDs == Set(["workbuddy", "grok"]), "multiple providers selected independently")
        check(store.effectiveSupervisedIDs.isSuperset(of: ["a", "b", "b2", "other"]), "provider covers all current readable dialogs")
        check(!store.effectiveSupervisedIDs.contains("unknown"), "unreadable conversation cannot enter automation")
        check(makeStore().supervisedAppIDs == Set(["workbuddy", "grok"]), "provider scope persists without enabling global sending")
        check(!defaults.bool(forKey: "autoContinueOptIn.v2"), "provider selection does not enable sending")
        let later = row("later", app: "grok")
        store.sessions.append(later)
        store.refreshSupervision()
        check(store.effectiveSupervisedIDs.contains("later"), "provider automatically covers later conversations")
        store.setAppSupervision(false, appID: "workbuddy")
        check(!store.effectiveSupervisedIDs.contains("b") && store.effectiveSupervisedIDs.contains("later"), "one provider can stop without affecting another")
        store.setAppSupervision(false, appID: "grok")
        store.sessions.removeAll { $0.id == later.id }
        store.toggleSupervision(for: a)
        check(store.isSupervised(a), "individual mark")
        check(!store.isSupervised(b), "mark does not select all")
        check(makeStore().supervisedSessionIDs == Set(["a"]), "mark persists by stable id")
        check(!defaults.bool(forKey: "autoContinueOptIn.v2"), "mark does not enable automatic sending")
        store.setSupervision(true, for: done)
        check(store.markedCompletedCount == 1, "historical marked count")
        check(store.supervisionGroups(includeCompleted: false).flatMap { $0.sessions }.contains { $0.id == done.id }, "marked terminal remains available to deselect")
        var rules = store.promptRules; rules.completionMode = "rage"; store.applyRules(rules)
        check(store.supervisedSessionIDs == Set(["a", "done"]), "rage mode preserves selection")
        rules.completionMode = "collaboration"; store.applyRules(rules)
        check(store.supervisedSessionIDs == Set(["a", "done"]), "collaboration preserves but does not remove marks")
        check(store.supervisionDetail(for: a).contains("协作模式"), "mark is not falsely labelled sending in collaboration")
        store.toggleSupervision(for: a)
        check(!store.isSupervised(a) && store.isSupervised(done), "precise unmark retains unrelated session")
        store.setSupervision(true, for: a)
        store.removeSession(a)
        check(!store.isSupervised(a) && store.isSupervised(done), "remove cancels only matching supervision")
        check(!store.supervisionGroups(includeCompleted: true).flatMap { $0.sessions }.contains { $0.id == a.id }, "removed conversation excluded from choices")
        store.restoreAllSessions()
        check(!store.isSupervised(a), "restore never re-arms removed supervision")
        store.setSupervision(true, for: unknown)
        check(!store.isSupervised(unknown), "unreadable cannot be marked")
        store.sessions.append(b)
        store.setSupervision(true, for: b)
        check(!store.isSupervised(b), "ambiguous duplicate id cannot be newly marked")
        store.sessions = []
        check(store.supervisedSessionIDs == Set(["done"]), "temporarily missing conversation retains preference")
        check(store.markedCompletedCount == 0, "missing old selection cannot enable historical start")
        let saved = makeStore()
        check(saved.supervisedSessionIDs == Set(["done"]), "final restart retains exact set")
        store.sessions = [row("historical", "completed", app: "qoder")]
        check(store.supervisionGroups(includeCompleted: false).first?.id == "qoder", "provider switch remains reachable with only historical sessions")
        store.setAppSupervision(true, appID: "qoder")
        check(store.markedCompletedCount == 1, "historical provider rounds need explicit start")
        store.setAppSupervision(false, appID: "qoder")
        var custom = PromptRules(); custom.ragePrompt = "用户自定义文字"
        custom.save(to: defaults)
        check(PromptRules.load(from: defaults).ragePrompt == "用户自定义文字", "new default never replaces custom prompt")
        check(PromptRules.defaultRagePrompt.contains("先完成尚未完成的工作"), "ordinary default continues unfinished work")
        check(PromptRules().ragePrompt(forApp: "grok").contains("请继续当前项目"), "Grok has its own continuation prompt")
        check(PromptRules().ragePrompt(forApp: "workbuddy") == PromptRules.defaultRagePrompt, "other apps retain global prompt")
        custom.appRagePrompts["grok"] = "请继续，用使用者视角优化"
        custom.save(to: defaults)
        check(PromptRules.load(from: defaults).ragePrompt(forApp: "grok") == "请继续，用使用者视角优化", "Grok prompt persists separately")
        custom.rageAlternateEvery = 5
        custom.rageAlternatePrompt = "第五次：从用户角度提出并落实建议"
        custom.save(to: defaults)
        let rotation = PromptRules.load(from: defaults)
        check(rotation.rageAlternateEvery == 5 && rotation.rageAlternatePrompt == "第五次：从用户角度提出并落实建议",
              "interval and special prompt persist across restart")
        check(PromptRules().rageAlternateEvery == 3 && PromptRules().rageAlternatePrompt.contains("真实使用者"),
              "default cadence uses every third send with user-perspective text")
        var invalid = custom
        invalid.rageAlternateEvery = 1
        invalid.rageAlternatePrompt = "  \u{0001}  "
        check(invalid.normalized().rageAlternateEvery == 2 &&
              invalid.normalized().rageAlternatePrompt == PromptRules.defaultRageAlternatePrompt,
              "invalid interval and empty special prompt are normalized")
        invalid.rageAlternateEvery = 999
        check(invalid.normalized().rageAlternateEvery == 100, "interval upper bound is enforced")
        var old = PromptRules()
        old.appRagePrompts["grok"] = "请继续当前项目。先完成上一轮尚未完成的工作，再从真实使用者的角度走查关键体验，指出具体问题，提出可验证的改进建议并实施优化；完成后实际测试，说明改动、验证结果和剩余问题。下一轮继续寻找值得改进的地方，不要只停留在建议。"
        old.save(to: defaults)
        check(PromptRules.load(from: defaults).ragePrompt(forApp: "grok") == PromptRules.defaultGrokRagePrompt,
              "old built-in Grok prompt migrates to ordinary cadence text")
        old.appRagePrompts["grok"] = "我自己的 Grok 长期提示词"
        old.save(to: defaults)
        check(PromptRules.load(from: defaults).ragePrompt(forApp: "grok") == "我自己的 Grok 长期提示词",
              "user-customized Grok prompt survives migration")
        print("Supervision Store: \(count) checks passed")
    }
}

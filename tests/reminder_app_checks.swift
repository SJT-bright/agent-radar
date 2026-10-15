import AppKit
import Foundation

/// Product reminder wiring with isolated preferences and no running collector/sender.
@main struct ReminderAppChecks {
    static var count = 0
    static func check(_ condition: @autoclosure () -> Bool, _ message: String) {
        guard condition() else { fatalError(message) }
        count += 1
    }

    static func main() {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.accessory)
        let suite = "local.agentradar.reminder-app-checks." + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = MonitorStore(defaults: defaults, writeHealthDiagnostics: false)
        check(store.reminderApps.allApps, "legacy preferences retain all-application reminders")
        check(defaults.object(forKey: MonitorStore.reminderAppsDefaultsKey) == nil, "loading does not write a selection")
        var draft = PromptSettingsDraft(rules: store.promptRules)
        draft.reminderApps = ReminderAppSelection(allApps: false, selectedAppIDs: ["codex", "grok", "offline-custom-app"])
        check(draft.hasChanges(comparedTo: store.promptRules), "selection participates in unsaved-draft protection")
        draft.reminderPopupEnabled = false
        store.applySettingsDraft(draft)
        let loaded = MonitorStore(defaults: defaults, writeHealthDiagnostics: false)
        check(loaded.reminderApps == draft.reminderApps, "selected and offline application IDs survive reload")
        check(!loaded.reminderApps.allows("openclaw"), "unselected application is muted")
        check(!loaded.reminderApps.allows("new-application"), "new application remains muted in selected-only mode")
        check(!draft.hasChanges(comparedTo: loaded.promptRules, reminderPopupEnabled: loaded.reminderPopupEnabled,
                                reminderSoundEnabled: loaded.reminderSoundEnabled, reminderApps: loaded.reminderApps), "saved draft is clean")
        check(loaded.reminderAppOptions.contains { $0.id == "offline-custom-app" }, "offline choice remains editable")
        let beforeRules = store.promptRules
        let beforeScope = store.supervisedSessionIDs
        let beforeApps = store.supervisedAppIDs
        let beforeAuto = store.autoContinueEnabled
        let toast = ContinuationToast()
        defer { toast.closeAll() }
        let sound = ReminderSoundPlayer()
        var playbacks = 0
        sound.onPlaybackStarted = { playbacks += 1 }
        let coordinator = ReminderCoordinator(store: store, toast: toast, anchor: { .zero }, soundPlayer: sound)
        defer { withExtendedLifetime(coordinator) {} }

        func notice(_ app: String, _ round: String, completion: Bool = true) -> ContinuationNotice {
            ContinuationNotice(title: app + " · 完成待验收", conversation: "隔离任务", message: "这个项目已完结，请验收",
                               isCompletion: completion, sessionID: app + ":fixture", recoveryKey: round, appID: app)
        }
        store.onRecoveryNotice?(notice("codex", "round-1"))
        store.onRecoveryNotice?(notice("openclaw", "round-1", completion: false))
        store.onRecoveryNotice?(notice("grok", "round-1"))
        check(toast.notices.map(\.appID) == ["codex", "grok"], "popup-off queue includes only chosen apps in arrival order")
        check(playbacks == 0, "hidden popup notices do not bypass the existing audio gate")
        draft.reminderApps = ReminderAppSelection(allApps: false, selectedAppIDs: ["grok"])
        draft.reminderSoundEnabled = false
        draft.reminderPopupEnabled = true
        store.applySettingsDraft(draft)
        check(toast.notices.map(\.appID) == ["grok"], "saving removes the muted current notice and promotes the next allowed one")
        check(NSApp.windows.first { $0.title == "AI 监督提醒" }?.isVisible == true, "allowed reminder is actually displayed")
        draft.reminderApps = ReminderAppSelection(allApps: false)
        store.applySettingsDraft(draft)
        check(toast.notices.isEmpty, "select-none removes current and pending reminders")
        check(NSApp.windows.first { $0.title == "AI 监督提醒" }?.isVisible == false, "select-none immediately hides the product popup")
        draft.reminderApps = ReminderAppSelection(allApps: true)
        store.applySettingsDraft(draft)
        store.onRecoveryNotice?(notice("codex", "round-1"))
        store.onRecoveryNotice?(notice("grok", "round-1"))
        check(toast.notices.isEmpty, "reselecting does not replay completed notices removed by the filter")
        store.onRecoveryNotice?(notice("codex", "round-2"))
        check(toast.notices.count == 1, "new completion round is still eligible after reselecting")
        toast.closeAll()

        draft.reminderApps = ReminderAppSelection(allApps: false, selectedAppIDs: ["codex"])
        draft.reminderPopupEnabled = false
        draft.reminderSoundEnabled = true
        store.applySettingsDraft(draft)
        let now = Date().timeIntervalSince1970
        func row(_ app: String, status: String, start: Double) -> SessionRecord {
            SessionRecord(id: app + ":audio", app_id: app, app_name: app, title: "Audio fixture", project: "/isolated/reminders",
                          status: status, evidence: "explicit lifecycle", updated_at: now, source: "local-session",
                          target: "preview://task", started_at: start, ended_at: status == "completed" ? now : nil, timing_basis: "turn")
        }
        store.onSessionsObserved?([row("codex", status: "running", start: now - 5), row("grok", status: "running", start: now - 5)], true)
        store.onSessionsObserved?([row("codex", status: "completed", start: now - 5), row("grok", status: "completed", start: now - 5)], true)
        check(playbacks == 1, "selected app requests actual audio; unselected completion stays silent even with popups off")
        draft.reminderApps.allApps = true
        store.applySettingsDraft(draft)
        store.onSessionsObserved?([row("codex", status: "completed", start: now - 5), row("grok", status: "completed", start: now - 5)], true)
        check(playbacks == 1, "reselecting does not replay muted historical completion audio")
        check(store.promptRules == beforeRules && store.supervisedSessionIDs == beforeScope &&
              store.supervisedAppIDs == beforeApps && store.autoContinueEnabled == beforeAuto, "reminder saves preserve execution and supervision choices")
        draft.reminderApps = ReminderAppSelection(allApps: false, selectedAppIDs: ["codex"])
        store.applySettingsDraft(draft)
        check(store.shouldRemind(ContinuationNotice(title: "冲突", conversation: "", message: "", isWorkspaceConflict: true,
                                                   relatedAppIDs: ["codex", "grok"])), "conflict involving a chosen app remains eligible")
        check(!store.shouldRemind(ContinuationNotice(title: "冲突", conversation: "", message: "", isWorkspaceConflict: true,
                                                    relatedAppIDs: ["openclaw", "grok"])), "conflict between muted apps remains silent")
        check(!store.shouldRemind(notice("grok", "offline-round")), "explicit app ownership works without a current session snapshot")
        check(store.shouldRemind(ContinuationNotice(title: "提醒预览", conversation: "", message: "")), "explicit generic preview remains available")
        defaults.set(Data("damaged".utf8), forKey: MonitorStore.reminderAppsDefaultsKey)
        let damaged = MonitorStore(defaults: defaults, writeHealthDiagnostics: false)
        check(!damaged.reminderApps.allApps && damaged.reminderApps.selectedAppIDs.isEmpty, "damaged choice cannot silently enable all apps")
        print("reminder app checks: \(count) passed; external sends: 0")
    }
}

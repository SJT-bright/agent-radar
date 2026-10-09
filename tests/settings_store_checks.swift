import Foundation

private final class SettingsStoreBridge: ContinuationSending {
    var isRunning = false
    var requests: [[String: Any]] = []
    var hold = false
    var cancelled = 0

    func cancel() { isRunning = false; cancelled += 1 }
    func run(_ request: [String: Any], completion: @escaping (String, Bool, Double?) -> Void) {
        precondition(request["mode"] as? String == "send", "fixture must never launch real input or collection")
        requests.append(request)
        if hold { isRunning = true; return }
        completion("sent_pending_confirmation", true, nil)
    }
}

private final class SettingsStoreHarness {
    let suite = "local.agentradar.settings-store." + UUID().uuidString
    let defaults: UserDefaults
    let bridge = SettingsStoreBridge()
    var now = 1000.0
    lazy var controller = ContinuationController(bridge: bridge, clock: { [unowned self] in self.now }, inputIdle: { 1000 }, countDefaults: defaults)
    lazy var store = MonitorStore(defaults: defaults, writeHealthDiagnostics: false, continuation: controller)

    init(rage: Bool = false, rate: Bool = false, sends: Int = 0) {
        defaults = UserDefaults(suiteName: suite)!
        defaults.set(["workbuddy:one": sends], forKey: "rageSuccessfulSendsBySession.v1")
        var rules = store.promptRules
        rules.completionMode = rage ? "rage" : "collaboration"
        rules.rateLimitWakeEnabled = rate
        rules.rateLimitWaitSeconds = 1
        rules.interruptText = "original interruption"
        rules.rateLimitText = "original rate limit"
        rules.ragePrompt = "ordinary"
        rules.rageAlternatePrompt = "special"
        store.applyRules(rules)
        store.setAutoContinue(true)
    }

    deinit { defaults.removePersistentDomain(forName: suite) }

    func row(_ status: String, id: String = "one", start: Double = 990, rate: Bool = false) -> SessionRecord {
        SessionRecord(id: "workbuddy:" + id, app_id: "workbuddy", app_name: "WorkBuddy", title: id,
                      project: "/isolated/settings", status: status, evidence: rate ? "HTTP 429" : "HTTP 503",
                      updated_at: now, source: "local-session", target: "workbuddy://chat/" + id,
                      started_at: start, timing_basis: "turn")
    }

    func observe(_ rows: [SessionRecord]) {
        store.sessions = rows
        store.refreshSupervision()
        controller.observe(rows, fresh: true)
    }

    @discardableResult func enqueue(_ status: String = "interrupted", rate: Bool = false, start: Double = 990) -> SessionRecord {
        let running = row("running", start: start, rate: rate)
        observe([running])
        store.setSupervision(true, for: running)
        now += 1
        let terminal = row(status, start: start, rate: rate)
        observe([terminal])
        precondition(controller.pendingSessionIDs.contains(terminal.id), "fixture must queue the intended automatic event")
        return terminal
    }
}

@main private struct SettingsStoreChecks {
    static func main() {
        var checks = 0
        func check(_ condition: Bool, _ name: String) { precondition(condition, name); checks += 1 }

        // All preferences use a unique suite, and no store/controller is started.
        // Exercise the same combined draft-save path as the settings window.
        do {
            let h = SettingsStoreHarness()
            check(h.store.reminderPopupEnabled && h.store.reminderSoundEnabled,
                  "legacy settings without reminder keys default popup and sound to enabled")
            check(h.defaults.object(forKey: MonitorStore.reminderPopupDefaultsKey) == nil &&
                  h.defaults.object(forKey: MonitorStore.reminderSoundDefaultsKey) == nil,
                  "reading legacy reminder settings does not write defaults")
            var callbacks = 0
            h.store.onReminderPreferencesChanged = { callbacks += 1 }
            for (popup, sound) in [(true, true), (false, true), (true, false), (false, false)] {
                let draft = PromptSettingsDraft(rules: h.store.promptRules, reminderPopupEnabled: popup,
                                                reminderSoundEnabled: sound)
                h.store.applySettingsDraft(draft)
                let reloaded = MonitorStore(defaults: h.defaults, writeHealthDiagnostics: false)
                check(reloaded.reminderPopupEnabled == popup && reloaded.reminderSoundEnabled == sound,
                      "each independent reminder preference combination survives reload")
                check(!draft.hasChanges(comparedTo: reloaded.promptRules,
                                        reminderPopupEnabled: reloaded.reminderPopupEnabled,
                                        reminderSoundEnabled: reloaded.reminderSoundEnabled),
                      "reloading saved reminder preferences clears draft changes")
            }
            check(callbacks == 3, "unchanged reminder save is silent and each actual change notifies once")
            var notices = 0, previews = 0
            h.store.onRecoveryNotice = { _ in notices += 1 }
            h.store.onPreviewReminderSound = { previews += 1 }
            h.store.previewContinuationNotice()
            h.store.previewReminderSound()
            check(notices == 1, "popup disable still delivers recovery notices to independent sound handling")
            check(previews == 1, "explicit sound preview remains available when both reminder preferences are disabled")
        }
        do {
            let h = SettingsStoreHarness()
            let interrupted = h.enqueue()
            h.defaults.set("preserve unknown settings", forKey: "futureSettingsMarker")
            let originalDomain = h.defaults.persistentDomain(forName: h.suite) ?? [:]
            let originalRules = h.store.promptRules
            let originalScope = h.store.supervisedSessionIDs
            let originalAppScope = h.store.supervisedAppIDs
            let originalCancels = h.bridge.cancelled
            var callbacks = 0
            h.store.onReminderPreferencesChanged = {
                callbacks += 1
                check(h.defaults.object(forKey: MonitorStore.reminderPopupDefaultsKey) as? Bool == false &&
                      h.defaults.object(forKey: MonitorStore.reminderSoundDefaultsKey) as? Bool == true,
                      "preference callback reads both already-persisted settings")
            }
            let draft = PromptSettingsDraft(rules: h.store.promptRules, reminderPopupEnabled: false,
                                            reminderSoundEnabled: true)
            h.store.applySettingsDraft(draft)
            h.store.applySettingsDraft(draft)
            check(callbacks == 1, "saving the same reminder draft twice sends one change callback")
            check(h.store.promptRules == originalRules && h.store.autoContinueEnabled,
                  "reminder-only save preserves execution rules and the automatic-send opt-in")
            check(h.store.supervisedSessionIDs == originalScope && h.store.supervisedAppIDs == originalAppScope,
                  "reminder-only save preserves per-session and per-software supervision")
            check(h.controller.pendingSessionIDs.contains(interrupted.id) && h.bridge.cancelled == originalCancels,
                  "reminder-only save does not cancel queued continuation work")
            var savedDomain = h.defaults.persistentDomain(forName: h.suite) ?? [:]
            savedDomain.removeValue(forKey: MonitorStore.reminderPopupDefaultsKey)
            savedDomain.removeValue(forKey: MonitorStore.reminderSoundDefaultsKey)
            check(NSDictionary(dictionary: savedDomain).isEqual(to: originalDomain),
                  "reminder-only save preserves all other persisted defaults including unknown keys")
            h.now += 5; h.observe([interrupted])
            check(h.bridge.requests.count == 1 &&
                  h.bridge.requests.first?["text"] as? String == "original interruption",
                  "authorized queued recovery still executes its original prompt after reminder-only save")
        }
        do {
            let h = SettingsStoreHarness()
            h.bridge.hold = true
            let interrupted = h.enqueue()
            h.now += 5; h.observe([interrupted])
            check(h.bridge.isRunning, "fixture has a held in-flight continuation verification")
            let originalCancels = h.bridge.cancelled
            h.store.applySettingsDraft(PromptSettingsDraft(rules: h.store.promptRules,
                                                          reminderPopupEnabled: true, reminderSoundEnabled: false))
            check(h.bridge.isRunning && h.bridge.cancelled == originalCancels &&
                  h.controller.pendingSessionIDs.contains(interrupted.id),
                  "sound-only save preserves an in-flight authorized continuation operation")
        }
        do {
            let h = SettingsStoreHarness()
            var observations: [Bool] = []
            h.store.onSessionsObserved = { rows, fresh in
                check(rows.isEmpty, "pause and stop clear the independent completion observation snapshot")
                observations.append(fresh)
            }
            h.store.togglePause()
            h.store.stop()
            check(observations == [false, false],
                  "pause and stop invalidate completion observations to avoid delayed completion sounds")
        }

        // Exercise the same store.applyRules path used by Save, not a direct
        // controller.configure call that could hide a missing store invalidation.
        do {
            let h = SettingsStoreHarness(rate: true)
            let interrupted = h.enqueue(rate: true)
            var rules = h.store.promptRules; rules.rateLimitWakeEnabled = false
            h.store.applyRules(rules)
            check(h.controller.pendingSessionIDs.isEmpty, "saving rate-limit disable cancels old queued wake")
            h.now += 61; h.observe([interrupted])
            check(h.bridge.requests.isEmpty, "disabled old rate-limit wake never sends at previous deadline")
            check(!PromptRules.load(from: h.defaults).rateLimitWakeEnabled, "disabled rate-limit setting persists")
        }
        do {
            let h = SettingsStoreHarness()
            let interrupted = h.enqueue()
            var rules = h.store.promptRules; rules.interruptText = "replacement interruption"
            h.store.applyRules(rules)
            check(h.controller.pendingSessionIDs.isEmpty, "saving interruption text cancels its old queued message")
            h.now += 6; h.observe([interrupted])
            check(h.bridge.requests.isEmpty, "saving does not re-arm already announced old round")
        }
        do {
            let h = SettingsStoreHarness(rate: true)
            _ = h.enqueue(rate: true)
            var rules = h.store.promptRules; rules.rateLimitWaitSeconds = 20
            h.store.applyRules(rules)
            check(h.controller.pendingSessionIDs.isEmpty, "saving changed wait cancels old deadline")
        }
        do {
            let h = SettingsStoreHarness()
            let interrupted = h.enqueue()
            var rules = h.store.promptRules; rules.followUps["unrelated"] = "legacy only"
            h.store.applyRules(rules)
            check(h.controller.pendingSessionIDs.contains(interrupted.id), "legacy follow-up edit preserves unrelated queue")
            h.store.applyRules(rules)
            check(h.controller.pendingSessionIDs.contains(interrupted.id), "saving unchanged execution settings preserves queue")
            h.now += 5; h.observe([interrupted])
            check(h.bridge.requests.first?["text"] as? String == "original interruption", "preserved queue still executes its authorized current prompt")
        }
        do {
            let h = SettingsStoreHarness(rage: true, sends: 2)
            let completed = h.enqueue("completed")
            var rules = h.store.promptRules; rules.ragePrompt = "updated ordinary"
            h.store.applyRules(rules)
            check(h.controller.pendingSessionIDs.isEmpty, "saving rage prompt cancels old completed queue")
            h.now += 6; h.observe([completed])
            check(h.bridge.requests.isEmpty, "rule change retains old-round announcement watermark")
            let next = h.enqueue("completed", start: h.now + 1)
            h.now += 5; h.observe([next])
            check(h.bridge.requests.first?["text"] as? String == "special", "rule change preserves successful-send cadence")
            check((h.defaults.dictionary(forKey: "rageSuccessfulSendsBySession.v1")?[next.id] as? Int) == 3, "isolated successful-send counter increments only for actual mock receipt")
            var second = h.store.promptRules; second.rageAlternatePrompt = "updated special"
            h.store.applyRules(second)
            h.controller.startRageForCompleted(); h.now += 5; h.observe([next])
            check(h.bridge.requests.count == 1, "rule change retains attempted-key protection for the same round")
        }
        do {
            let h = SettingsStoreHarness()
            h.bridge.hold = true
            let interrupted = h.enqueue()
            h.now += 5; h.observe([interrupted])
            check(h.bridge.isRunning, "fixture has an in-flight input verification")
            var rules = h.store.promptRules; rules.interruptText = "changed during verification"
            h.store.applyRules(rules)
            check(!h.bridge.isRunning && h.controller.pendingSessionIDs.isEmpty, "saving changed execution settings cancels in-flight old request")
            h.controller.requestManualRetry(sessionID: interrupted.id, key: ContinuationPolicy.key(interrupted))
            h.now += 3; h.observe([interrupted])
            check(h.bridge.requests.count == 1, "cancelled in-flight round cannot be retried with unknown send outcome")
        }
        do {
            let h = SettingsStoreHarness()
            let missing = h.row("running"), unreadable = h.row("running", id: "unreadable"), retained = h.row("running", id: "retained")
            h.observe([missing, unreadable, retained])
            for row in [missing, unreadable, retained] { h.store.setSupervision(true, for: row) }
            h.observe([h.row("unknown", id: "unreadable"), retained])
            h.store.removeSupervision(sessionID: missing.id)
            h.store.removeSupervision(sessionID: unreadable.id)
            h.store.removeSupervision(sessionID: "does-not-exist")
            check(h.store.supervisedSessionIDs == [retained.id], "missing and unreadable selection can be removed without touching other choices")
            check(Set(h.defaults.stringArray(forKey: MonitorStore.supervisionDefaultsKey) ?? []) == [retained.id], "selection removal persists exact remaining IDs")
        }
        do {
            let h = SettingsStoreHarness(rage: true)
            let completed = h.enqueue("completed")
            h.store.sessions = []
            h.store.removeSupervision(sessionID: completed.id)
            check(h.controller.pendingSessionIDs.isEmpty, "removing a now-missing selection refreshes and cancels its automatic queue")
        }
        print("Settings Store: \(checks) checks passed")
    }
}

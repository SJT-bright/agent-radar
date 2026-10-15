import AppKit

/// The same presentation/audio wiring is used by the app and its isolated UI preview.
final class ReminderCoordinator {
    private weak var store: MonitorStore?
    private let toast: ContinuationToast
    private let soundPlayer: ReminderSoundPlayer
    private var completionSounds = TaskCompletionSoundTracker()
    private var soundGate = ReminderSoundGate()

    init(store: MonitorStore, toast: ContinuationToast, anchor: @escaping () -> NSRect,
         soundPlayer: ReminderSoundPlayer = ReminderSoundPlayer()) {
        self.store = store
        self.toast = toast
        self.soundPlayer = soundPlayer
        toast.popupEnabled = store.reminderPopupEnabled
        store.onWorkspaceConflictsChanged = { [weak toast] paths in toast?.reconcileWorkspaceConflicts(paths) }
        store.onReminderPreferencesChanged = { [weak self] in
            guard let self = self, let store = self.store else { return }
            if !store.reminderPopupEnabled { self.toast.popupEnabled = false }
            self.toast.reconcileReminders(shouldKeep: store.shouldRemind)
            self.toast.popupEnabled = store.reminderPopupEnabled
        }
        store.onPreviewReminderSound = { [weak soundPlayer] in _ = soundPlayer?.play() }
        store.onSessionsObserved = { [weak self] rows, fresh in
            guard let self = self, let store = self.store else { return }
            for event in self.completionSounds.observe(rows, fresh: fresh, now: Date().timeIntervalSince1970) {
                self.play(key: ReminderSoundGate.roundKey(sessionID: event.session.id, recoveryKey: event.key),
                          allowed: store.reminderApps.allows(event.session.app_id))
            }
        }
        toast.onDidPresentNotice = { [weak self] notice in
            guard let self = self, let store = self.store else { return }
            self.play(key: ReminderSoundGate.roundKey(sessionID: notice.sessionID ?? notice.conversation,
                                                       recoveryKey: notice.recoveryKey ?? notice.title), allowed: store.shouldRemind(notice))
        }
        store.onRecoveryNotice = { [weak self, weak store] notice in
            guard let self = self, let store = store else { return }
            guard store.shouldRemind(notice) else {
                self.play(key: ReminderSoundGate.roundKey(sessionID: notice.sessionID ?? notice.conversation,
                                                         recoveryKey: notice.recoveryKey ?? notice.title), allowed: false)
                return
            }
            self.toast.show(notice, anchor: anchor(),
                        cancel: { [weak store] in store?.cancelContinuation() },
                        permission: { [weak store] in store?.requestPermission() },
                        retry: { [weak store] sessionID, key in store?.retryContinuation(sessionID: sessionID, key: key) },
                        open: { [weak store] sessionID in store?.openNoticeSession(sessionID: sessionID) ?? false },
                        openAfterNavigation: { [weak store] sessionID, done in
                            guard let store = store else { done(false); return }
                            store.openNoticeSession(sessionID: sessionID, completion: done)
                        },
                        cancelRecovery: { [weak store] sessionID, key in store?.cancelContinuation(sessionID: sessionID, key: key) })
        }
    }

    private func play(key: String, allowed: Bool = true) {
        guard let store = store, soundGate.shouldPlay(key: key, enabled: store.reminderSoundEnabled && allowed) else { return }
        soundPlayer.play()
    }
}

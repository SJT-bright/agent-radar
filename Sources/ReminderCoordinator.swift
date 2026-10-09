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
            self.toast.popupEnabled = store.reminderPopupEnabled
        }
        store.onPreviewReminderSound = { [weak soundPlayer] in _ = soundPlayer?.play() }
        store.onSessionsObserved = { [weak self] rows, fresh in
            guard let self = self else { return }
            for event in self.completionSounds.observe(rows, fresh: fresh, now: Date().timeIntervalSince1970) {
                self.play(key: ReminderSoundGate.roundKey(sessionID: event.session.id, recoveryKey: event.key))
            }
        }
        toast.onDidPresentNotice = { [weak self] notice in
            self?.play(key: ReminderSoundGate.roundKey(sessionID: notice.sessionID ?? notice.conversation,
                                                       recoveryKey: notice.recoveryKey ?? notice.title))
        }
        store.onRecoveryNotice = { [weak toast, weak store] notice in
            toast?.show(notice, anchor: anchor(),
                        cancel: { [weak store] in store?.cancelContinuation() },
                        permission: { [weak store] in store?.requestPermission() },
                        retry: { [weak store] sessionID, key in store?.retryContinuation(sessionID: sessionID, key: key) },
                        open: { [weak store] sessionID in store?.openNoticeSession(sessionID: sessionID) ?? false },
                        cancelRecovery: { [weak store] sessionID, key in store?.cancelContinuation(sessionID: sessionID, key: key) })
        }
    }

    private func play(key: String) {
        guard let store = store, soundGate.shouldPlay(key: key, enabled: store.reminderSoundEnabled) else { return }
        soundPlayer.play()
    }
}

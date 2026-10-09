import AppKit

/// Claim a round whether sound is enabled or muted. Changing preferences never
/// replays an old event, and presenting its card cannot sound it a second time.
struct ReminderSoundGate {
    private var claimed = Set<String>()

    static func roundKey(sessionID: String, recoveryKey: String) -> String {
        sessionID + ":" + recoveryKey
    }

    mutating func shouldPlay(key: String, enabled: Bool) -> Bool {
        guard claimed.insert(key).inserted else { return false }
        return enabled
    }
}

/// Local playback bypasses notification-center permission and popup preferences.
/// Retain independent sounds so simultaneous task completions remain audible.
final class ReminderSoundPlayer: NSObject, NSSoundDelegate {
    private var active: [NSSound] = []
    var onPlaybackStarted: (() -> Void)?

    @discardableResult
    func play() -> Bool {
        let path = "/System/Library/Sounds/Ping.aiff"
        guard let sound = NSSound(contentsOfFile: path, byReference: true) else { return false }
        sound.volume = 0.75
        sound.delegate = self
        active.removeAll { !$0.isPlaying }
        // A collector burst should not retain an unlimited number of players.
        if active.count >= 16 { active.removeFirst().stop() }
        active.append(sound)
        guard sound.play() else {
            active.removeAll { $0 === sound }
            return false
        }
        onPlaybackStarted?()
        return true
    }

    func sound(_ sound: NSSound, didFinishPlaying flag: Bool) {
        active.removeAll { $0 === sound }
    }
}

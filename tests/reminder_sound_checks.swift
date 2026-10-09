import Foundation

@main struct ReminderSoundChecks {
    static var checks = 0
    static func check(_ value: Bool, _ message: String) {
        checks += 1
        if !value { fputs("FAIL: \(message)\n", stderr); exit(1) }
    }
    static func row(_ id: String = "codex:one", app: String = "codex", start: Double = 100,
                    status: String = "running", end: Double? = nil) -> SessionRecord {
        SessionRecord(id: id, app_id: app, app_name: app, title: "A real task", project: "/preview/work",
                      status: status, evidence: "explicit lifecycle", updated_at: end ?? start,
                      source: "local-session", target: "preview://task", started_at: start,
                      ended_at: end, timing_basis: "turn")
    }
    static func main() {
        var tracker = TaskCompletionSoundTracker()
        check(tracker.observe([row(status: "completed", end: 120)], fresh: true, now: 200).isEmpty,
              "startup does not sound historical completion")
        check(tracker.observe([row(start: 210)], fresh: true, now: 211).isEmpty, "running does not sound")
        let completed = tracker.observe([row(start: 210, status: "completed", end: 212)], fresh: true, now: 213)
        check(completed.count == 1, "unsupervised completion sounds independently of sending mode")
        check(completed.first?.session.id == "codex:one", "completion retains its stable session identity")
        check(tracker.observe([row(start: 210, status: "completed", end: 212)], fresh: true, now: 214).isEmpty,
              "same round repeated in collector is silent")
        check(tracker.observe([row(start: 210, status: "completed", end: 214)], fresh: true, now: 215).isEmpty,
              "end timestamp refresh cannot replay a round")
        var gate = ReminderSoundGate()
        let firstKey = ReminderSoundGate.roundKey(sessionID: completed[0].session.id, recoveryKey: completed[0].key)
        check(gate.shouldPlay(key: firstKey, enabled: true), "completion requests audio even with no popup")
        check(!gate.shouldPlay(key: firstKey, enabled: true), "presenting its popup does not double sound")
        check(!gate.shouldPlay(key: "muted round", enabled: false), "sound preference mutes the event")
        check(!gate.shouldPlay(key: "muted round", enabled: true), "enabling sound does not replay muted history")
        check(gate.shouldPlay(key: "new notice", enabled: true), "a new interruption popup can sound")
        check(!gate.shouldPlay(key: "new notice", enabled: true), "countdown redraw remains silent")
        let second = tracker.observe([row(start: 216, status: "completed", end: 218)], fresh: true, now: 219)
        check(second.count == 1, "authoritative task finishing between polls sounds")
        check(second.first?.key != completed.first?.key, "a new round has a new sound identity")
        let pair = tracker.observe([
            row("qoder:one", app: "qoder", start: 220, status: "completed", end: 221),
            row("zcode:two", app: "zcode", start: 220, status: "completed", end: 222)
        ], fresh: true, now: 223)
        check(Set(pair.map { $0.session.id }) == ["qoder:one", "zcode:two"], "multiple desktop apps complete independently")
        var pauseTracker = TaskCompletionSoundTracker()
        _ = pauseTracker.observe([row(start: 300)], fresh: true, now: 301)
        check(pauseTracker.observe([], fresh: false, now: 302).isEmpty, "pause or stale scan clears observation")
        check(pauseTracker.observe([row(start: 300, status: "completed", end: 303)], fresh: true, now: 304).isEmpty,
              "resuming does not sound tasks completed during pause")
        _ = pauseTracker.observe([row(start: 310)], fresh: true, now: 311)
        check(pauseTracker.observe([row(start: 310, status: "completed", end: 312)], fresh: true, now: 313).count == 1,
              "fresh work after resume sounds")
        for state in ["idle", "waiting", "unknown", "interrupted", "stalled"] {
            var other = TaskCompletionSoundTracker()
            _ = other.observe([row(start: 400)], fresh: true, now: 401)
            check(other.observe([row(start: 400, status: state, end: 402)], fresh: true, now: 403).isEmpty,
                  "\(state) is not task completion")
        }
        var invalid = TaskCompletionSoundTracker()
        _ = invalid.observe([], fresh: true, now: 500)
        var stopped = row(start: 501, status: "completed", end: 502); stopped.user_stopped = true
        var window = row("window", start: 501, status: "completed", end: 502); window.source = "window"
        var unnamed = row("unnamed", start: 501, status: "completed", end: 502); unnamed.title = "未命名任务"
        check(invalid.observe([stopped, window, unnamed], fresh: true, now: 503).isEmpty,
              "stopped, window-only and unreadable tasks do not sound")
        check(invalid.observe([row(start: 504, status: "completed", end: 510)], fresh: true, now: 505).isEmpty,
              "future end timestamp cannot sound")
        check(invalid.observe([row("ambiguous", start: 504, status: "completed", end: 505),
                               row("ambiguous", start: 504, status: "completed", end: 505)], fresh: true, now: 506).isEmpty,
              "duplicate session identity cannot sound")
        print("\(checks) reminder sound checks passed; no audio or external app operations")
    }
}

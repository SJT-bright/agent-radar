import Foundation
import Combine

@main private struct MonitorRefreshChecks {
    static func main() {
        let suite = "local.agentradar.refresh-checks." + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = MonitorStore(defaults: defaults, writeHealthDiagnostics: false)
        var publications = 0
        let observation = store.objectWillChange.sink { publications += 1 }
        var observations: [Bool] = []
        store.onSessionsObserved = { _, fresh in observations.append(fresh) }
        let now = Date().timeIntervalSince1970
        let row = SessionRecord(id: "codex:refresh", app_id: "codex", app_name: "Codex",
                                title: "isolated refresh", project: "/isolated/refresh",
                                status: "running", evidence: "fixture", updated_at: now,
                                source: "local-session", target: "", started_at: now - 5, timing_basis: "turn")
        let snapshot = CollectorSnapshot(sessions: [row], errors: [], collected_at: now)
        store.acceptSnapshot(snapshot)
        publications = 0
        for _ in 0..<100 { store.acceptSnapshot(snapshot) }
        precondition(publications == 0, "unchanged snapshots must not invalidate the entire SwiftUI tree")
        precondition(observations.count == 101 && observations.allSatisfy { $0 },
                     "deduplication must retain every fresh lifecycle observation")
        store.acceptSnapshot(CollectorSnapshot(sessions: [row], errors: [], collected_at: now + 1))
        precondition(publications == 1, "a heartbeat publishes its timestamp once, without unchanged session/error/conflict updates")
        store.acceptSnapshot(CollectorSnapshot(sessions: [row], errors: [], collected_at: now - 20))
        precondition(store.sessions.first?.status == "unknown" && observations.last == false,
                     "identical rows with an expired heartbeat must still lose active authority")
        store.acceptSnapshot(CollectorSnapshot(sessions: [row], errors: ["source error"], collected_at: now))
        precondition(store.errors == ["source error"] && store.sessions.first?.status == "running",
                     "fresh state and errors must update after expiry")
        withExtendedLifetime(observation) {}

        let entered = DispatchSemaphore(value: 0), release = DispatchSemaphore(value: 0)
        let finished = DispatchSemaphore(value: 0), lock = NSLock()
        var written: [Int] = [], wasMain = false
        let writer = HealthDiagnosticWriter { data in
            let info = try! JSONSerialization.jsonObject(with: data) as! [String: Any]
            let value = info["receipt"] as! Int
            lock.lock()
            written.append(value)
            wasMain = wasMain || Thread.isMainThread
            lock.unlock()
            if value == 0 {
                entered.signal()
                precondition(release.wait(timeout: .now() + 5) == .success)
            } else { finished.signal() }
        }
        writer.submit(["receipt": 0])
        precondition(entered.wait(timeout: .now() + 5) == .success)
        for value in 1...1000 { writer.submit(["receipt": value]) }
        release.signal()
        precondition(finished.wait(timeout: .now() + 5) == .success)
        lock.lock()
        precondition(written == [0, 1000], "a stalled disk keeps only the newest pending receipt and preserves write order")
        precondition(!wasMain, "JSON and disk writes must execute outside the UI thread")
        lock.unlock()
        print("Monitor refresh checks passed: unchanged publications 0/100; heartbeat 1; expiry retained; health backlog bounded to 1")
    }
}

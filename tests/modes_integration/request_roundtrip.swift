import Foundation
final class CaptureBridge: ContinuationSending {
    var isRunning = false
    var requests: [[String: Any]] = []
    func cancel() {}
    func run(_ request: [String: Any], completion: @escaping (String, Bool, Double?) -> Void) {
        requests.append(request); completion("sent_pending_confirmation", true, nil)
    }
}
final class RoundtripJudge: ContinuationSending {
    var isRunning = false
    func cancel() {}
    func run(_ request: [String: Any], completion: @escaping (String, Bool, Double?) -> Void) {
        completion("judged_complete", false, nil)
    }
}
@main struct RequestRoundtrip {
    static func main() throws {
        var now = 1_000_000_000.0
        let bridge = CaptureBridge()
        let defaults = UserDefaults(suiteName: "agentradar.request-roundtrip.\(UUID().uuidString)")!
        let tested = ContinuationController(bridge: bridge, judgeBridge: RoundtripJudge(), clock: { now }, inputIdle: { 1000 }, countDefaults: defaults)
        var rules = PromptRules(); rules.completionMode = "rage"; rules.ragePrompt = "继续优化并验证"
        rules.rageAlternateEvery = 3; rules.rageAlternatePrompt = "以用户视角优化并验证"
        tested.rulesProvider = { rules }; tested.configure(enabled: true, paused: false)
        tested.setSupervisedIDs(["workbuddy:integration"])
        var row = SessionRecord(id: "workbuddy:integration", app_id: "workbuddy", app_name: "WorkBuddy", title: "Isolation",
            project: "/isolated-test", status: "running", evidence: "explicit lifecycle", updated_at: now,
            source: "local-session", target: "workbuddy://chat/integration", started_at: now - 2, timing_basis: "turn")
        tested.observe([row], fresh: true); row.status = "completed"; tested.observe([row], fresh: true)
        for round in 0..<12 {
            now += 5; tested.tick()
            precondition(bridge.requests.count == round + 1, "each fresh completed round must enqueue exactly once")
            if round != 11 {
                now += 1; row.started_at = now; row.updated_at = now
                // No running snapshot between: completion is the fresh receipt.
                tested.observe([row], fresh: true)
            }
        }
        let data = try JSONSerialization.data(withJSONObject: bridge.requests, options: [.sortedKeys])
        print(String(decoding: data, as: UTF8.self))
    }
}

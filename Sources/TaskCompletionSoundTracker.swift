import Foundation

/// Observe completion independently of supervision, popup visibility and sending mode.
/// The lifecycle policy keeps the per-session round watermark when observation resets.
struct TaskCompletionSoundTracker {
    private var policy = ContinuationPolicy()
    private var monitoringBeganAt: Double?

    mutating func observe(_ rows: [SessionRecord], fresh: Bool, now: Double) -> [ContinuationEvent] {
        guard fresh, now.isFinite else {
            policy.resetObservation()
            monitoringBeganAt = nil
            return []
        }
        if monitoringBeganAt == nil { monitoringBeganAt = now }
        // Ambiguous snapshots cannot identify which task finished.
        let included = Dictionary(grouping: rows, by: \.id).values.compactMap {
            $0.count == 1 ? $0.first : nil
        }
        // A short task can start and end between collector snapshots. Only an
        // authoritative, recent end after this observation period can arm it.
        for row in included where row.status == "completed" {
            guard ContinuationPolicy.identityRestriction(row) == nil,
                  row.timing_basis == "turn",
                  let start = row.started_at, let end = row.ended_at,
                  end.isFinite, end >= start, end > (monitoringBeganAt ?? now),
                  now - end >= -2, now - end <= 120 else { continue }
            policy.observeConfirmedRound(row, now: now)
        }
        return policy.observe(included, now: now, completionMode: "collaboration")
            .filter { $0.kind == "completion" }
    }
}

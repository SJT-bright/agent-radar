import Foundation

private struct Notice: SessionNoticeEntry {
    let sessionKey: String
    let round: String
    let state: String
    var completionKey: String? { state == "completion" ? sessionKey + ":" + round : nil }
}

@main struct SessionNoticeQueueChecks {
    static func main() {
        var queue = SessionNoticeQueue<Notice>()
        func notice(_ session: String, _ state: String, _ round: String = "1") -> Notice {
            Notice(sessionKey: session, round: round, state: state)
        }
        // Consecutive failures from different sessions must remain reachable.
        queue.show(notice("zcode:a", "route_failed"))
        queue.show(notice("zcode:b", "input_failed"))
        queue.show(notice("qoder:c", "recovering"))
        precondition(queue.current?.sessionKey == "zcode:a")
        precondition(queue.pendingCount == 2)
        precondition(queue.pending.map(\.sessionKey) == ["zcode:b", "qoder:c"])

        // Both current and queued cards retain the latest status and round.
        queue.show(notice("zcode:a", "recovering", "2"))
        queue.show(notice("zcode:b", "completion", "2"))
        queue.show(notice("zcode:b", "input_failed", "3"))
        precondition(queue.pendingCount == 2)
        precondition(queue.current?.round == "2")
        precondition(queue.pending[0].round == "3" && queue.pending[0].state == "input_failed")
        queue.close()
        precondition(queue.current?.sessionKey == "zcode:b" && queue.pendingCount == 1)
        queue.close()
        precondition(queue.current?.sessionKey == "qoder:c" && queue.pendingCount == 0)
        // Dismissing the session acknowledges its earlier unread completions,
        // even if a later recovery result had replaced that card.
        precondition(!queue.show(notice("zcode:b", "completion", "2")))
        precondition(queue.show(notice("zcode:b", "completion", "4")))
        precondition(queue.pendingCount == 1)

        queue.show(notice("grok:d", "completion"))
        queue.closeAll()
        precondition(queue.current == nil && queue.pendingCount == 0)
        precondition(!queue.show(notice("zcode:b", "completion", "4")))
        precondition(!queue.show(notice("grok:d", "completion")))
        precondition(queue.show(notice("zcode:a", "route_failed", "5")))
        queue.updateCurrent { $0 = notice("zcode:a", "open_failed", "5") }
        precondition(queue.current?.state == "open_failed")
        queue.close()
        precondition(queue.current == nil)
        queue.show(notice("workspace:a", "warning"))
        queue.show(notice("zcode:valid", "completion"))
        queue.show(notice("workspace:b", "warning"))
        queue.remove { $0.state == "warning" }
        precondition(queue.current?.sessionKey == "zcode:valid" && queue.pendingCount == 0)
        queue.close()
        precondition(!queue.show(notice("zcode:valid", "completion")))
        queue.show(notice("zcode:pending", "recovering"))
        queue.show(notice("workspace:c", "warning"))
        queue.remove { $0.state == "warning" }
        precondition(queue.current?.sessionKey == "zcode:pending" && queue.pendingCount == 0)
        print("Session notice queue checks passed: failures, progress, deduplication, order, acknowledgement, dismissal")
    }
}

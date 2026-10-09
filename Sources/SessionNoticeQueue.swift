/// Keep every unread session, including recovery failures, in arrival order.
/// New rounds and progress replace that session's card without adding a task.
protocol SessionNoticeEntry {
    var sessionKey: String { get }
    var completionKey: String? { get }
}

struct SessionNoticeQueue<Entry: SessionNoticeEntry> {
    private(set) var current: Entry?
    private(set) var pending: [Entry] = []
    private var acknowledgedCompletions = Set<String>()
    private var unreadCompletions: [String: Set<String>] = [:]

    var pendingCount: Int { pending.count }

    @discardableResult
    mutating func show(_ entry: Entry) -> Bool {
        if let key = entry.completionKey {
            guard !acknowledgedCompletions.contains(key) else { return false }
            unreadCompletions[entry.sessionKey, default: []].insert(key)
        }
        if current == nil || current?.sessionKey == entry.sessionKey {
            current = entry
        } else if let index = pending.firstIndex(where: { $0.sessionKey == entry.sessionKey }) {
            pending[index] = entry
        } else {
            pending.append(entry)
        }
        return true
    }

    mutating func updateCurrent(_ update: (inout Entry) -> Void) {
        guard var entry = current else { return }
        update(&entry)
        current = entry
    }

    mutating func close() {
        if let entry = current { acknowledge(entry.sessionKey) }
        current = pending.isEmpty ? nil : pending.removeFirst()
    }

    mutating func closeAll() {
        if let entry = current { acknowledge(entry.sessionKey) }
        for entry in pending { acknowledge(entry.sessionKey) }
        current = nil
        pending.removeAll()
    }

    mutating func remove(where shouldRemove: (Entry) -> Bool) {
        pending.removeAll(where: shouldRemove)
        if let entry = current, shouldRemove(entry) {
            current = pending.isEmpty ? nil : pending.removeFirst()
        }
    }

    private mutating func acknowledge(_ session: String) {
        if let keys = unreadCompletions.removeValue(forKey: session) {
            acknowledgedCompletions.formUnion(keys)
        }
    }
}

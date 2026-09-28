import Foundation

/// Radar-owned exclusions. Never writes to the source application's history.
struct RemovedSession: Codable, Identifiable, Equatable {
    let id: String
    let appName: String
    let title: String
    let removedAt: Date

    init(_ session: SessionRecord) {
        id = Self.key(for: session)
        appName = session.app_name
        title = session.title
        removedAt = Date()
    }

    static func key(for session: SessionRecord) -> String {
        // Native window IDs contain transient PIDs. Until an app exposes a
        // conversation key, match its exact title/project across relaunches.
        let parts: [String]
        if session.source == "window" {
            if let navigation = session.navigation_key, !navigation.isEmpty {
                parts = [session.app_id, "navigation", navigation]
            } else {
                parts = [session.app_id, "window-title", session.project,
                         session.title.trimmingCharacters(in: .whitespacesAndNewlines)]
            }
        } else {
            parts = [session.app_id, "session", session.id]
        }
        return String(data: try! JSONEncoder().encode(parts), encoding: .utf8)!
    }
}

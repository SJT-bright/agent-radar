import Foundation

@main struct WorkspaceConflictChecks {
    static func main() throws {
        var checks = 0
        func check(_ value: Bool) { precondition(value); checks += 1 }
        func row(_ id: String, _ app: String, _ path: String, _ status: String = "running") -> SessionRecord {
            SessionRecord(id: id, app_id: app, app_name: app, title: "task-" + id,
                          project: path, status: status, evidence: "", updated_at: 0,
                          source: "local", target: "")
        }
        let a = row("a", "Codex", "/work/project"), b = row("b", "ZCode", "/work/project/")
        check(WorkspaceConflict.detect([a, b]).count == 1)
        check(WorkspaceConflict.detect([a, row("c", "Codex", a.project)]).isEmpty)
        check(WorkspaceConflict.detect([a, row("c", "ZCode", "/other/project")]).isEmpty)
        check(WorkspaceConflict.detect([a, row("c", "ZCode", "/work/other/../project")]).count == 1)
        for state in ["completed", "idle", "unknown", "interrupted"] {
            check(WorkspaceConflict.detect([a, row("c", "ZCode", a.project, state)]).isEmpty)
        }
        for state in ["waiting", "stalled"] {
            check(WorkspaceConflict.detect([a, row("c", "ZCode", a.project, state)]).count == 1)
        }
        for path in ["", "project", "zcode://workspace", " /tmp/invalid\0path"] {
            check(WorkspaceConflict.canonicalPath(path) == nil)
        }
        var unnamed = b; unnamed.title = "未命名任务"
        check(WorkspaceConflict.detect([a, unnamed]).isEmpty)
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let link = dir.appendingPathComponent("alias")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: dir)
        check(WorkspaceConflict.detect([row("a", "Codex", dir.path), row("b", "ZCode", link.path)]).count == 1)
        var tracker = WorkspaceConflictTracker()
        check(tracker.observe([a, b], fresh: false).isEmpty)
        check(tracker.observe([a, b], fresh: true).count == 1)
        check(tracker.observe([b, a], fresh: true).isEmpty)
        check(tracker.observe([a, b, row("c", "Codex", a.project)], fresh: true).isEmpty)
        check(tracker.observe([], fresh: false).isEmpty && tracker.current.isEmpty)
        check(tracker.observe([a, b], fresh: true).isEmpty)
        check(tracker.observe([a, b, row("c", "Qoder", a.project)], fresh: true).count == 1)
        check(tracker.observe([a], fresh: true).isEmpty && tracker.current.isEmpty)
        check(tracker.observe([a, b], fresh: true).count == 1)
        check(tracker.current.first?.appNames == ["Codex", "ZCode"])
        print("Workspace conflicts: \(checks) checks passed")
    }
}

import Foundation
import AppKit

enum FrostLevel: Int, CaseIterable {
    case off = 0, light, medium, strong
    var label: String { ["关闭", "轻薄", "适中", "浓郁"][rawValue] }
    // Allocate the selected opacity between live blur and a dark text scrim.
    // Keeping the total budget tied to transparency lets the desktop show through.
    var materialShare: Double { [0, 0.45, 0.70, 0.90][rawValue] }
}

enum AccessibilityCheckState {
    case idle, checking, passed, failed
}

struct SessionRecord: Codable, Identifiable, Equatable {
    var id: String
    var app_id: String
    var app_name: String
    var title: String
    var project: String
    var status: String
    var evidence: String
    var updated_at: Double
    var source: String
    var target: String
    var pid: Int32?
    var window_id: Int?
    var started_at: Double? = nil
    var ended_at: Double? = nil
    var last_activity_at: Double? = nil
    var timing_basis: String? = nil
    var status_reason: String? = nil
    var timing_reason: String? = nil
    var navigation_title: String? = nil
    var navigation_key: String? = nil
    var user_stopped: Bool? = nil
    var hasDisplayIdentity: Bool {
        let name = title.trimmingCharacters(in: .whitespacesAndNewlines)
        return !name.isEmpty && name != app_name &&
            !name.contains("会话待识别") && !name.contains("未命名窗口") &&
            !["未命名会话", "未命名任务", "Claude 会话"].contains(name) &&
            !(app_id == "autoclaw" && name.hasPrefix("会话 "))
    }
    var isReadable: Bool { status != "unknown" && hasDisplayIdentity }
    /// A verified Codex identity survives uncertain lifecycle evidence. Display
    /// does not grant automation or count an unknown conversation as running.
    var isDisplayable: Bool {
        isReadable || (hasDisplayIdentity && app_id == "codex" && source == "local-session" &&
            id.hasPrefix("codex:") && UUID(uuidString: String(id.dropFirst(6))) != nil &&
            target == "codex://threads/" + String(id.dropFirst(6)))
    }
    var statusReason: String { status_reason ?? evidence }
    var timingExplanation: String {
        if timing_basis == "observed" {
            return "应用未提供可用的本轮起点；从雷达首次观察到活动开始计时，不代表完整时长。" + (timing_reason ?? "")
        }
        return timing_reason ?? (started_at == nil ?
            (source == "window" ? "窗口只暴露当前状态，没有本轮开始时间" : "数据源没有提供本轮开始事件，无法从更新时间推算完整时长") :
            "按数据源明确记录的本轮开始和结束事件计算")
    }
    var displayProject: String {
        Self.folderName(for: project) ?? "未识别工作文件夹"
    }
    static func folderName(for project: String) -> String? {
        let path = project.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !path.isEmpty else { return nil }
        let name = URL(fileURLWithPath: path).lastPathComponent
        return name == "/" ? "根目录" : (name.isEmpty ? nil : name)
    }
    var primaryDisplayName: String { Self.folderName(for: project) ?? title }
    var secondaryDisplayName: String? {
        guard Self.folderName(for: project) != nil, title != primaryDisplayName else { return nil }
        return title
    }
    var displayIdentityDetail: String {
        "工作文件夹：\(project.isEmpty ? "未识别" : project)\n对话：\(title)"
    }
    /// 手动停止不算中断：不提醒、不计数、不置顶，只作中性展示。
    var userStoppedInterruption: Bool { status == "interrupted" && user_stopped == true }
    var statusLabel: String {
        switch status {
        case "running": return "进行中"
        case "waiting": return "待处理"
        case "interrupted": return user_stopped == true ? "已手动停止" : "已中断"
        case "stalled": return "疑似停滞"
        case "completed": return "本轮完成"
        case "idle": return "空闲"
        default: return "待确认"
        }
    }
    var priority: Int {
        ["interrupted": 0, "stalled": 1, "waiting": 2, "running": 3, "unknown": 4, "idle": 5, "completed": 6][status] ?? 4
    }
    var isActive: Bool { ["running", "waiting", "stalled"].contains(status) }
    func elapsedLabel(at now: Double) -> String {
        guard let start = started_at, start > 0 else { return "未提供开始时间" }
        let end = ended_at ?? (isActive ? now : last_activity_at ?? updated_at)
        guard end >= start else { return "时间戳顺序异常" }
        let prefix = timing_basis == "observed" ? "已观察 " : (timing_basis == "last-confirmed" ? "已确认 " : "本轮 ")
        return prefix + Self.duration(end - start)
    }
    static func duration(_ interval: Double) -> String {
        let seconds = max(0, Int(interval))
        if seconds >= 3600 { return "\(seconds / 3600)时\((seconds % 3600) / 60)分" }
        if seconds >= 60 { return "\(seconds / 60)分\(seconds % 60)秒" }
        return "\(seconds)秒"
    }
}

struct WorkspaceConflict: Identifiable {
    let id: String
    let sessions: [SessionRecord]
    var folderName: String { SessionRecord.folderName(for: id) ?? id }
    var appNames: [String] {
        Dictionary(grouping: sessions, by: \.app_id).values.compactMap { $0.first?.app_name }.sorted()
    }
    var fingerprint: String { id + "\n" + Set(sessions.map(\.app_id)).sorted().joined(separator: "\n") }
    var detail: String {
        "多个软件正在同一工作文件夹中工作，可能修改相同文件。\n工作文件夹：\(id)\n" +
        sessions.map { "\($0.app_name)：\($0.title)" }.joined(separator: "\n")
    }
    static func detect(_ rows: [SessionRecord]) -> [WorkspaceConflict] {
        let candidates = rows.filter { $0.isActive && $0.isReadable && !$0.app_id.isEmpty }
        let groups = Dictionary(grouping: candidates.filter { canonicalPath($0.project) != nil },
                                by: { canonicalPath($0.project)! })
        return groups.compactMap { path, sessions in
            guard Set(sessions.map(\.app_id)).count > 1 else { return nil }
            return WorkspaceConflict(id: path, sessions: sessions.sorted { $0.id < $1.id })
        }.sorted { $0.id < $1.id }
    }
    static func canonicalPath(_ path: String) -> String? {
        let value = path.trimmingCharacters(in: .whitespacesAndNewlines)
        guard value.hasPrefix("/"), !value.contains("\0") else { return nil }
        return URL(fileURLWithPath: value).standardizedFileURL.resolvingSymlinksInPath().path
    }
}

struct WorkspaceConflictTracker {
    private var previous = Set<String>()
    private(set) var current: [WorkspaceConflict] = []
    mutating func observe(_ rows: [SessionRecord], fresh: Bool) -> [WorkspaceConflict] {
        // A stale/unreadable scan cannot prove that a conflict has ended.
        guard fresh else { current = []; return [] }
        current = WorkspaceConflict.detect(rows)
        let new = current.filter { !previous.contains($0.fingerprint) }
        previous = Set(current.map(\.fingerprint))
        return new
    }
}

/// Keep observation timing separate from authoritative turn timestamps.
/// Unknown or unreadable sources freeze the clock instead of implying progress.
struct SessionClocks {
    private var previous: [String: SessionRecord] = [:]
    private var observedAt: [String: Double] = [:]
    mutating func update(_ rows: [SessionRecord], now: Double) -> [SessionRecord] {
        var next: [String: SessionRecord] = [:]
        let result = rows.map { raw -> SessionRecord in
            var row = raw
            let old = previous[row.id]
            if row.started_at == nil {
                if let old = old, let start = old.started_at,
                   !(row.isActive && ["completed", "idle", "interrupted"].contains(old.status)) {
                    row.started_at = start
                    row.timing_basis = old.timing_basis
                    row.timing_reason = old.timing_reason
                } else if row.isActive {
                    row.started_at = now
                    row.timing_basis = "observed"
                }
            }
            if !row.isActive && row.ended_at == nil, let start = row.started_at {
                if let old = old, !old.isActive, old.started_at == start {
                    row.ended_at = old.ended_at
                } else {
                    if row.status == "unknown" {
                        row.ended_at = max(start, observedAt[row.id] ?? min(now, row.last_activity_at ?? row.updated_at))
                    } else if row.timing_basis == "turn" {
                        row.ended_at = max(start, min(now, row.last_activity_at ?? row.updated_at))
                        row.timing_basis = "last-confirmed"
                        row.timing_reason = "没有明确结束事件，计时截止到最后确认的会话记录"
                    } else {
                        row.ended_at = now
                    }
                }
            }
            if row.isActive { observedAt[row.id] = now }
            next[row.id] = row
            return row
        }
        previous = next
        observedAt = observedAt.filter { next[$0.key] != nil }
        return result
    }
}

struct AppRecord: Identifiable, Equatable {
    var id: String
    var name: String
    var bundleID: String
    var pid: Int32
    var path: String
}

struct NativeScan {
    var sessions: [SessionRecord]
    var apps: [AppRecord]
    var accessibility: Bool
}

struct CollectorSnapshot: Codable {
    var sessions: [SessionRecord]
    var errors: [String]
    var collected_at: Double
}

using System;
using System.Collections.Generic;
using System.Linq;
using AgentRadar.Core;

namespace AgentRadar.Windows.Ui;

internal static class PanelMetrics
{
    public const double ExpandedWidth = 238;
    public const double CompactWidth = 60;
    public const double CompactHeight = 32;
    public const double MaxHeight = 548;
    public const double MinHeight = 112;
    public const double Header = 40;
    public const double Footer = 28;
    public const double Access = 30;
    public const double Action = 24;
    public const double Control = 30;
    public const double Corner = 18;
}

internal static class SessionText
{
    public static string? Folder(string project)
    {
        if (string.IsNullOrWhiteSpace(project) || project.IndexOf('\0') >= 0) return null;
        var trimmed = project.Trim().TrimEnd('/', '\\');
        if (trimmed.Length == 0) return "根目录";
        var parts = trimmed.Split('/', '\\');
        var name = parts[parts.Length - 1];
        return name.Length == 0 ? null : name;
    }

    public static string Primary(SessionRecord session)
    {
        var folder = Folder(session.Project);
        if (folder != null) return folder;
        if (!string.IsNullOrWhiteSpace(session.Title)) return session.Title;
        return string.IsNullOrWhiteSpace(session.AppName) ? session.Id : session.AppName;
    }

    public static string? Secondary(SessionRecord session)
    {
        var folder = Folder(session.Project);
        if (folder == null || string.IsNullOrWhiteSpace(session.Title) || session.Title == folder) return null;
        return session.Title;
    }

    public static string Elapsed(SessionRecord session, double now)
    {
        if (session.StartedAt is not double start || start <= 0) return "未提供开始时间";
        var active = session.Status is "running" or "waiting" or "stalled";
        var end = session.EndedAt ?? (active ? now : session.LastActivityAt ?? session.UpdatedAt);
        if (end < start) return "时间戳顺序异常";
        var prefix = session.TimingBasis switch
        {
            "observed" => "已观察 ",
            "last-confirmed" => "已确认 ",
            _ => "本轮 "
        };
        return prefix + Duration(end - start);
    }

    public static string Duration(double interval)
    {
        var seconds = Math.Max(0, (int)interval);
        if (seconds >= 3600) return $"{seconds / 3600}时{(seconds % 3600) / 60}分";
        if (seconds >= 60) return $"{seconds / 60}分{seconds % 60}秒";
        return $"{seconds}秒";
    }

    public static string TimingExplanation(SessionRecord session)
    {
        if (session.TimingBasis == "observed")
            return "应用未提供可用的本轮起点；从雷达首次观察到活动开始计时，不代表完整时长。";
        if (session.StartedAt == null)
        {
            return session.Source == "window"
                ? "窗口只暴露当前状态，没有本轮开始时间"
                : "数据源没有提供本轮开始事件，无法从更新时间推算完整时长";
        }
        return "按数据源明确记录的本轮开始和结束事件计算";
    }

    public static string Detail(SessionRecord session, double now)
    {
        var project = string.IsNullOrWhiteSpace(session.Project) ? "未识别" : session.Project;
        var reason = string.IsNullOrWhiteSpace(session.StatusReason) ? session.Evidence : session.StatusReason;
        return $"工作文件夹：{project}\n对话：{session.Title}\n{session.StatusLabel} · {reason}\n{TimingExplanation(session)}\n{Elapsed(session, now)}";
    }

    public static bool RecentInterrupt(SessionRecord session, double now) =>
        session.Status == "interrupted" && session.UserStopped != true && now - (session.EndedAt ?? session.UpdatedAt) < 600;

    public static double Now() => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds() / 1000d;
}

internal sealed class WorkspaceConflict
{
    public required string Path { get; init; }
    public required SessionRecord[] Sessions { get; init; }
    public string Folder => SessionText.Folder(Path) ?? Path;
    public string Fingerprint => Path + "\n" + string.Join("\n", Sessions.Select(session => session.AppId).Distinct().OrderBy(id => id, StringComparer.Ordinal));
    public string AppNames => string.Join("、", Sessions.GroupBy(session => session.AppId).Select(group => group.First().AppName).OrderBy(name => name, StringComparer.Ordinal));
    public string Detail =>
        "多个软件正在同一工作文件夹中工作，可能修改相同文件。\n工作文件夹：" + Path + "\n" +
        string.Join("\n", Sessions.Select(session => session.AppName + "：" + session.Title));
}

internal static class ConflictFinder
{
    public static WorkspaceConflict[] Detect(IEnumerable<SessionRecord> rows)
    {
        var groups = new Dictionary<string, List<SessionRecord>>(StringComparer.Ordinal);
        foreach (var session in rows)
        {
            if (!session.IsReadable || string.IsNullOrWhiteSpace(session.AppId)) continue;
            if (session.Status is not ("running" or "waiting" or "stalled")) continue;
            var path = Canonical(session.Project);
            if (path == null) continue;
            if (!groups.TryGetValue(path, out var list)) groups[path] = list = new List<SessionRecord>();
            list.Add(session);
        }
        return groups
            .Where(pair => pair.Value.Select(session => session.AppId).Distinct().Count() > 1)
            .OrderBy(pair => pair.Key, StringComparer.Ordinal)
            .Select(pair => new WorkspaceConflict
            {
                Path = pair.Key,
                Sessions = pair.Value.OrderBy(session => session.Id, StringComparer.Ordinal).ToArray()
            })
            .ToArray();
    }

    public static string? Canonical(string project)
    {
        if (string.IsNullOrWhiteSpace(project) || project.IndexOf('\0') >= 0) return null;
        var value = project.Trim().Replace('/', '\\');
        while (value.Length > 3 && value.EndsWith('\\')) value = value[..^1];
        var drive = value.Length >= 3 && char.IsLetter(value[0]) && value[1] == ':' && value[2] == '\\';
        var unc = value.StartsWith("\\\\", StringComparison.Ordinal) && value.Count(character => character == '\\') >= 3;
        if (!drive && !unc) return null;
        return value.ToLowerInvariant();
    }
}

internal sealed class ConversationGroup
{
    public required string Id { get; init; }
    public required SessionRecord[] Rows { get; init; }
    public SessionRecord Primary => Rows[0];
}

internal static class SessionGroups
{
    public static ConversationGroup[] Build(IReadOnlyList<SessionRecord> visible, IReadOnlyList<SessionRecord> all, ISet<string> removed, ISet<string> expanded)
    {
        var order = new List<string>();
        var grouped = new Dictionary<string, List<SessionRecord>>(StringComparer.Ordinal);
        foreach (var session in visible)
        {
            if (removed.Contains(session.Id)) continue;
            if (!grouped.ContainsKey(session.AppId))
            {
                order.Add(session.AppId);
                grouped[session.AppId] = new List<SessionRecord>();
            }
            grouped[session.AppId].Add(session);
        }
        var shown = new HashSet<string>(grouped.Values.SelectMany(rows => rows.Select(session => session.Id)), StringComparer.Ordinal);
        var history = all
            .Where(session => grouped.ContainsKey(session.AppId) && session.IsReadable && !removed.Contains(session.Id) && !shown.Contains(session.Id) && IsToday(session))
            .OrderByDescending(session => session.UpdatedAt)
            .Take(12);
        foreach (var session in history) grouped[session.AppId].Add(session);
        return order.Select(id => new ConversationGroup { Id = id, Rows = grouped[id].ToArray() }).ToArray();
    }

    public static double ContentHeight(IReadOnlyList<ConversationGroup> groups, ISet<string> expanded)
    {
        if (groups.Count == 0) return 120;
        double height = 8;
        foreach (var group in groups)
        {
            height += RowHeight(group.Primary, primary: true) + 4;
            if (!expanded.Contains(group.Id)) continue;
            foreach (var session in group.Rows.Skip(1)) height += RowHeight(session, primary: false) + 2;
        }
        return height;
    }

    public static double RowHeight(SessionRecord session, bool primary)
    {
        var reason = session.Status is "interrupted" or "stalled";
        var secondary = SessionText.Secondary(session) != null;
        return primary
            ? (reason ? 76 : 62) + (secondary ? 14 : 0)
            : (reason ? 54 : 40) + (secondary ? 14 : 0);
    }

    private static bool IsToday(SessionRecord session)
    {
        var unix = session.EndedAt ?? session.UpdatedAt;
        if (!double.IsFinite(unix) || unix <= 0 || unix > 4_000_000_000d) return false;
        try
        {
            var local = DateTimeOffset.FromUnixTimeMilliseconds((long)Math.Round(unix * 1000d)).LocalDateTime;
            return local.Date == DateTime.Today;
        }
        catch (ArgumentOutOfRangeException) { return false; }
    }

    public static double PanelHeight(double contentHeight, bool hasAction, double workHeight)
    {
        var height = PanelMetrics.Header + contentHeight + PanelMetrics.Footer + PanelMetrics.Access + (hasAction ? PanelMetrics.Action : 0);
        var limit = Math.Max(PanelMetrics.MinHeight, workHeight - 80);
        return Math.Min(limit, Math.Min(PanelMetrics.MaxHeight, Math.Max(PanelMetrics.MinHeight, height)));
    }
}

internal static class AppNames
{
    private static readonly Dictionary<string, string> Names = new(StringComparer.Ordinal)
    {
        ["workbuddy"] = "WorkBuddy",
        ["workbuddy-ai"] = "WorkBuddy AI",
        ["zcode"] = "ZCode",
        ["autoclaw"] = "AutoClaw",
        ["grok"] = "Grok",
        ["codex"] = "Codex",
        ["qoder"] = "Qoder",
        ["qoder-cn"] = "Qoder CN",
        ["openclaw"] = "OpenClaw",
        ["claude-code"] = "Claude Code",
        ["cline"] = "Cline"
    };

    public static string Display(string id) => Names.TryGetValue(id, out var name) ? name : id;

    public static string[] Known { get; } = Names.Keys.OrderBy(id => id, StringComparer.Ordinal).ToArray();
}

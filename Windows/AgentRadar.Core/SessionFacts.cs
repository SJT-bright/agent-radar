using System.Globalization;
using System.Security.Cryptography;
using System.Text;

namespace AgentRadar.Core;

/// <summary>Value copy and display rules. SessionRecord is a class; callers may reuse instances.</summary>
public static class SessionFacts {
    public static SessionRecord Clone(SessionRecord source) {
        return new SessionRecord {
            Id = source.Id,
            AppId = source.AppId,
            AppName = source.AppName,
            Title = source.Title,
            Project = source.Project,
            Status = source.Status,
            Evidence = source.Evidence,
            UpdatedAt = source.UpdatedAt,
            Source = source.Source,
            Target = source.Target,
            StartedAt = source.StartedAt,
            EndedAt = source.EndedAt,
            LastActivityAt = source.LastActivityAt,
            TimingBasis = source.TimingBasis,
            StatusReason = source.StatusReason,
            NavigationTitle = source.NavigationTitle,
            NavigationKey = source.NavigationKey,
            LifecycleRevision = source.LifecycleRevision,
            RunId = source.RunId,
            ManualOnly = source.ManualOnly,
            ContinuousAuthorized = source.ContinuousAuthorized,
            CareRetryAt = source.CareRetryAt,
            UserStopped = source.UserStopped,
            Pid = source.Pid,
        };
    }

    public static string RoundKey(SessionRecord row) {
        var start = row.StartedAt is double value && double.IsFinite(value) ? value : 0;
        return row.Id + ":" + ((long)(start * 1000)).ToString(CultureInfo.InvariantCulture);
    }

    public static bool HasDisplayIdentity(SessionRecord row) {
        var name = row.Title.Trim();
        if (name.Length == 0 || name == row.AppName) return false;
        if (name.Contains("会话待识别", StringComparison.Ordinal) || name.Contains("未命名窗口", StringComparison.Ordinal)) return false;
        if (name is "未命名会话" or "未命名任务" or "Claude 会话") return false;
        if (row.AppId == "autoclaw" && name.StartsWith("会话 ", StringComparison.Ordinal)) return false;
        return true;
    }

    public static bool AutomationReadable(SessionRecord row) {
        return row.Status != "unknown" && HasDisplayIdentity(row);
    }

    public static bool IsDisplayable(SessionRecord row) {
        if(row.Source=="window"&&HasDisplayIdentity(row))return true;
        if (AutomationReadable(row) || row.IsReadable) return row.Status != "unknown" || CodexPending(row) || OpenClawPending(row);
        return CodexPending(row) || OpenClawPending(row);
    }

    public static bool CodexPending(SessionRecord row) {
        if (!HasDisplayIdentity(row) || row.AppId != "codex" || row.Source != "local-session") return false;
        if (!row.Id.StartsWith("codex:", StringComparison.Ordinal)) return false;
        var uuid = row.Id[6..];
        return Guid.TryParse(uuid, out _) && row.Target == "codex://threads/" + uuid;
    }

    public static bool OpenClawPending(SessionRecord row) {
        return HasDisplayIdentity(row) && row.AppId == "openclaw" && row.Source == "local-db"
            && row.Id.StartsWith("openclaw:", StringComparison.Ordinal)
            && row.NavigationKey?.StartsWith("agent:", StringComparison.Ordinal) == true;
    }

    public static string FolderName(string project) {
        var path = project.Trim();
        if (path.Length == 0) return "";
        var trimmed = path.TrimEnd('/', '\\');
        if (trimmed.Length == 0) return "根目录";
        var index = Math.Max(trimmed.LastIndexOf('/'), trimmed.LastIndexOf('\\'));
        var name = index >= 0 ? trimmed[(index + 1)..] : trimmed;
        return name.Length == 0 ? "" : name;
    }

    public static string CanonicalProject(string project) {
        var value = project.Trim().Replace('\\', '/');
        if (value.Length == 0 || value.Contains('\0', StringComparison.Ordinal)) return "";
        var rooted = value.StartsWith('/') || (value.Length >= 3 && char.IsLetter(value[0]) && value[1] == ':' && value[2] == '/');
        if (!rooted) return "";
        var parts = new List<string>();
        foreach (var part in value.Split('/', StringSplitOptions.RemoveEmptyEntries)) {
            if (part == ".") continue;
            if (part == "..") {
                if (parts.Count == 0) return "";
                parts.RemoveAt(parts.Count - 1);
                continue;
            }
            parts.Add(part);
        }
        var prefix = value.StartsWith('/') ? "/" : "";
        return prefix + string.Join('/', parts);
    }

    public static string Sha256(string value) {
        var bytes = SHA256.HashData(Encoding.UTF8.GetBytes(value));
        return Convert.ToHexString(bytes).ToLowerInvariant();
    }

    public static int ScalarCount(string text) {
        var count = 0;
        foreach (var _ in text.EnumerateRunes()) count++;
        return count;
    }
}

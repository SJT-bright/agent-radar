using System.Globalization;
using System.Text.RegularExpressions;

namespace AgentRadar.Core;

public sealed record WindowIdentity(int Pid, string Title, string ProjectHint);

public readonly record struct WindowMatch(bool Matched, string Code, int Pid);

/// <summary>Pure routing and mutation gates shared by Core tests and the Windows helper.</summary>
public static class StagePolicy {
    public const string SupervisorEnvironment = "AGENT_RADAR_BRIDGE_SUPERVISOR";
    public const string SupervisorToken = "verified-python-supervisor";
    public const int MaxTextScalars = 2000;
    public static readonly Regex OpenClawId = new(
        @"^openclaw:[A-Za-z0-9_-]{1,80}:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
        RegexOptions.CultureInvariant | RegexOptions.Compiled);
    static readonly Regex BlockedKey = new(
        @"^agent:[^:]+:(?:subagent|cron|acp|evolution-check):",
        RegexOptions.CultureInvariant | RegexOptions.Compiled);
    static readonly Regex ControlChars = new(@"[\u0000-\u0008\u000B-\u001F\u007F]", RegexOptions.CultureInvariant | RegexOptions.Compiled);

    public static bool IsBlockedOpenClawKey(string? key) => !string.IsNullOrEmpty(key) && BlockedKey.IsMatch(key);

    public static string? ValidateSendText(string? text) {
        if (text is null) return null;
        if (ControlChars.IsMatch(text)) return "text_invalid";
        var trimmed = text.Trim();
        if (trimmed.Length == 0) return null;
        if (SessionFacts.ScalarCount(trimmed) > MaxTextScalars) return "text_invalid";
        return null;
    }

    public static string CanonicalText(string value) => value.Replace("\r\n", "\n", StringComparison.Ordinal).Replace('\r', '\n');

    public static bool MutationAuthorized(bool nativeBridge, string? supervisorEnvironment, bool requestHasToken,
        string? id, string? appId, string? title, string? project, string? navigationKey, string? target, int? pid) {
        if (!nativeBridge) return false;
        if (supervisorEnvironment != SupervisorToken || !requestHasToken) return false;
        if (string.IsNullOrWhiteSpace(id) || string.IsNullOrWhiteSpace(appId) || string.IsNullOrWhiteSpace(title)) return false;
        var hasWindow = pid is > 0 || !string.IsNullOrWhiteSpace(project) || !string.IsNullOrWhiteSpace(navigationKey) || SafeLink(target, appId) != null;
        return hasWindow && !IsAmbiguousGlobalName(title, AppCatalog.DisplayName(appId));
    }

    public static bool IsAmbiguousGlobalName(string? title, string? appName) {
        if (string.IsNullOrWhiteSpace(title)) return true;
        var trimmed = title.Trim();
        if (appName != null && trimmed.Equals(appName.Trim(), StringComparison.OrdinalIgnoreCase)) return true;
        return AppCatalog.IsBareAppName(trimmed);
    }

    public static string? SafeLink(string? target, string? appId) {
        if (string.IsNullOrWhiteSpace(target) || string.IsNullOrWhiteSpace(appId)) return null;
        if (!Uri.TryCreate(target, UriKind.Absolute, out var uri)) return null;
        if (!string.IsNullOrEmpty(uri.UserInfo) || uri.Fragment.Length > 0) return null;
        var marker = uri.Scheme + "://";
        var rest = target.StartsWith(marker, StringComparison.OrdinalIgnoreCase) ? target[marker.Length..] : "";
        var host = rest.Split(['/', '?'], 2)[0];
        if (host.Contains('@', StringComparison.Ordinal) || host.Contains(':', StringComparison.Ordinal)) return null;
        if (appId == "codex" && uri.Scheme == "codex" && uri.Host == "threads" && string.IsNullOrEmpty(uri.Query)) {
            var path = uri.AbsolutePath;
            if (path.Count(static c => c == '/') != 1 || !path.StartsWith('/')) return null;
            return Guid.TryParse(path[1..], out _) ? uri.AbsoluteUri : null;
        }
        if (appId == "workbuddy" && uri.Scheme == "workbuddy" && uri.Host == "chat" && string.IsNullOrEmpty(uri.Query)) {
            var id = uri.AbsolutePath.Length > 1 ? uri.AbsolutePath[1..] : "";
            if (id.Length is 0 or > 256 || id is "." or ".." || id.Contains('/') || ControlChars.IsMatch(id)) return null;
            return uri.AbsoluteUri;
        }
        if (appId == "zcode" && uri.Scheme == "zcode" && uri.Host == "workspace" && uri.AbsolutePath == "/open") {
            var query = uri.Query.StartsWith('?') ? uri.Query[1..] : uri.Query;
            var parts = query.Split('&', StringSplitOptions.RemoveEmptyEntries);
            if (parts.Length != 1 || !parts[0].StartsWith("path=", StringComparison.Ordinal)) return null;
            var workspace = Uri.UnescapeDataString(parts[0][5..]);
            if (!LocalPath(workspace,false,null) || workspace.Length > 512 || workspace.Contains('\0') || ControlChars.IsMatch(workspace)) return null;
            if (workspace.Split('/', StringSplitOptions.RemoveEmptyEntries).Any(part => part is "." or "..")) return null;
            return uri.AbsoluteUri;
        }
        return null;
    }

    public static bool LoopbackGateway(string? url) {
        if (string.IsNullOrWhiteSpace(url) || !Uri.TryCreate(url, UriKind.Absolute, out var uri)) return false;
        if (uri.Scheme != "ws" || !string.IsNullOrEmpty(uri.UserInfo)) return false;
        return (uri.Host is "127.0.0.1" or "localhost" or "[::1]" or "::1") && uri.Port>0 && uri.Query.Length==0 && uri.Fragment.Length==0 && uri.AbsolutePath=="/";
    }

    public static bool LocalPath(string? path, bool allowWslUnc, string? distribution) {
        if (string.IsNullOrWhiteSpace(path)) return true;
        if (path.Contains('\0') || path.Contains("..", StringComparison.Ordinal)) return false;
        if (path.StartsWith(@"\\", StringComparison.Ordinal) || path.StartsWith("//", StringComparison.Ordinal)) {
            if (!allowWslUnc || string.IsNullOrWhiteSpace(distribution)) return false;
            var normalized = path.Replace('/', '\\');
            var prefixes = new[] { @"\\wsl$\" + distribution + @"\", @"\\wsl.localhost\" + distribution + @"\" };
            return prefixes.Any(prefix => normalized.StartsWith(prefix, StringComparison.OrdinalIgnoreCase));
        }
        if (path.StartsWith('/') || (path.Length >= 3 && char.IsLetter(path[0]) && path[1] == ':' && (path[2] == '\\' || path[2] == '/'))) return true;
        return false;
    }

    public static WindowMatch Match(IReadOnlyList<WindowIdentity> windows, string? title, string? project, string? appName, int? pid) {
        if (IsAmbiguousGlobalName(title, appName)) return new WindowMatch(false, "window_ambiguous", 0);
        var wanted = title!.Trim();
        var folder = SessionFacts.FolderName(project ?? "");
        var candidates = windows.Where(window => TitleEquals(window.Title, wanted)).ToList();
        if (pid is > 0) candidates = candidates.Where(window => window.Pid == pid.Value).ToList();
        if (candidates.Count > 1 && folder.Length > 0) {
            var narrowed = candidates.Where(window => TitleEquals(window.Title, wanted) && ProjectAgrees(window, folder, project)).ToList();
            if (narrowed.Count == 1) candidates = narrowed;
        }
        if (candidates.Count == 1) {
            var only = candidates[0];
            if (folder.Length > 0 && !ProjectAgrees(only, folder, project) && !TitleEquals(only.Title, wanted))
                return new WindowMatch(false, "header_unverified", 0);
            return new WindowMatch(true, "ready", only.Pid);
        }
        if (candidates.Count > 1) return new WindowMatch(false, "window_ambiguous", 0);
        return new WindowMatch(false, windows.Count == 0 ? "target_unverified" : "header_unverified", 0);
    }

    static bool TitleEquals(string live, string wanted) => live.Trim().Equals(wanted, StringComparison.Ordinal);

    static bool ProjectAgrees(WindowIdentity window, string folder, string? project) {
        if (folder.Length == 0) return true;
        if (window.Title.Contains(folder, StringComparison.Ordinal)) return true;
        var hint = SessionFacts.CanonicalProject(window.ProjectHint);
        var wanted = SessionFacts.CanonicalProject(project ?? "");
        return hint.Length > 0 && hint == wanted;
    }

    public static bool MetadataCarriesThread(string name,string automationId,string helpText,IEnumerable<string> keys,bool windowTitle) {
        // Document/editor names can contain drafts or historical messages.
        // Only a window title or non-content identifiers may carry identity.
        return keys.Where(k=>k.Length>=16).Any(k=>(windowTitle&&name.Length<=512&&name.Contains(k,StringComparison.Ordinal))
            || automationId.Length<=512&&automationId.Contains(k,StringComparison.Ordinal)
            || helpText.Length<=512&&helpText.StartsWith("codex://threads/",StringComparison.Ordinal)&&helpText.Contains(k,StringComparison.Ordinal));
    }

    public static string FormatSeconds(double value) => value.ToString("0.###", CultureInfo.InvariantCulture);
}

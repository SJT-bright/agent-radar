namespace AgentRadar.Core;

public static class AppCatalog {
    public static readonly (string Process, string Id, string Name)[] Processes = [
        ("codex", "codex", "Codex"),
        ("chatgpt", "chatgpt", "ChatGPT"),
        ("claude", "claude", "Claude"),
        ("cursor", "cursor", "Cursor"),
        ("gemini", "gemini", "Gemini"),
        ("antigravity", "antigravity", "Antigravity"),
        ("trae", "trae", "TRAE"),
        ("qoder", "qoder", "Qoder"),
        ("qodercn", "qoder-cn", "Qoder CN"),
        ("qoder-cn", "qoder-cn", "Qoder CN"),
        ("cline", "cline", "Cline"),
        ("doubao", "doubao", "豆包"),
        ("kimi", "kimi", "Kimi"),
        ("grok", "grok", "Grok"),
        ("qianwen", "qianwen", "千问"),
        ("yuanbao", "yuanbao", "元宝"),
        ("openclaw", "openclaw", "OpenClaw"),
        ("ollama", "ollama", "Ollama"),
        ("hermes", "hermes", "Hermes"),
        ("coze", "coze", "扣子"),
        ("windsurf", "windsurf", "Windsurf"),
        ("lmstudio", "lmstudio", "LM Studio"),
        ("workbuddy", "workbuddy", "WorkBuddy"),
        ("zcode", "zcode", "ZCode"),
        ("autoclaw", "autoclaw", "AutoClaw"),
    ];

    public static readonly string[] ProtocolSchemes = [
        "codex", "workbuddy", "zcode", "openclaw", "claude", "cursor", "grok", "qoder", "windsurf",
    ];

    public static bool TryMatchProcess(string? processName, out string id, out string name) {
        id = "";
        name = "";
        if (string.IsNullOrWhiteSpace(processName)) return false;
        var token = processName.Trim();
        if (token.EndsWith(".exe", StringComparison.OrdinalIgnoreCase)) token = token[..^4];
        if (token.Contains("helper", StringComparison.OrdinalIgnoreCase)) return false;
        foreach (var entry in Processes) {
            if (token.Equals(entry.Process, StringComparison.OrdinalIgnoreCase)) {
                id = entry.Id;
                name = entry.Name;
                return true;
            }
        }
        return false;
    }

    public static bool HasAiMetadata(string? bundleOrName) {
        if (string.IsNullOrWhiteSpace(bundleOrName)) return false;
        var lower = bundleOrName.ToLowerInvariant();
        if (lower.StartsWith("com.apple.", StringComparison.Ordinal)) return false;
        var tokens = lower.Split([' ', '.', '_', '-', '/', '\\'], StringSplitOptions.RemoveEmptyEntries);
        var set = new HashSet<string>(tokens);
        if (set.Contains("helper") || (set.Contains("computer") && set.Contains("use")) || (set.Contains("dock") && set.Contains("extra"))) return false;
        if (set.Overlaps(["ai", "llm", "gpt", "chatgpt", "copilot", "agent", "assistant"])) return true;
        return bundleOrName.Contains("智能体", StringComparison.Ordinal)
            || bundleOrName.Contains("AI助手", StringComparison.Ordinal)
            || bundleOrName.Contains("AI 助手", StringComparison.Ordinal)
            || bundleOrName.Contains("大模型", StringComparison.Ordinal);
    }

    public static string DisplayName(string? appId) {
        foreach (var entry in Processes) if (entry.Id == appId) return entry.Name;
        return appId ?? "";
    }

    public static bool IsBareAppName(string title) {
        foreach (var entry in Processes) {
            if (title.Equals(entry.Name, StringComparison.OrdinalIgnoreCase) || title.Equals(entry.Id, StringComparison.OrdinalIgnoreCase)) return true;
        }
        return false;
    }

    public static string? SchemeApp(string scheme) {
        return scheme.ToLowerInvariant() switch {
            "codex" => "codex",
            "workbuddy" => "workbuddy",
            "zcode" => "zcode",
            "openclaw" => "openclaw",
            "claude" => "claude",
            "cursor" => "cursor",
            "grok" => "grok",
            "qoder" => "qoder",
            "windsurf" => "windsurf",
            _ => null,
        };
    }
}

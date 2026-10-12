using System.Globalization;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace AgentRadar.Core;

public static class ReceiptLog {
    static readonly Regex Code = new("^[a-z][a-z0-9_]{0,63}$", RegexOptions.CultureInvariant | RegexOptions.Compiled);
    static readonly Regex Hash = new("^[0-9a-f]{64}$", RegexOptions.CultureInvariant | RegexOptions.Compiled);
    static readonly HashSet<string> Apps = ["autoclaw", "codex", "workbuddy", "workbuddy-ai", "zcode", "grok", "qoder-cn", "qoder", "openclaw"];
    static readonly HashSet<string> Stages = ["queued", "preflight", "route", "identity", "write", "readback", "send", "new-round", "timeout", "cancel"];

    public static void Append(string directory, string operationId, string sessionId, string key, string appId, string stage, string code, bool attempted, double time, double detectedAt) {
        if (!double.IsFinite(time) || time <= 0 || time >= 1e11 || !double.IsFinite(detectedAt)) return;
        if (!Stages.Contains(stage) || !Code.IsMatch(code) || !Apps.Contains(appId)) return;
        var canonical = Canonical(operationId);
        if (canonical == null) return;
        var elapsed = (int)Math.Min(86_400_000, Math.Max(0, (time - detectedAt) * 1000));
        var record = new SortedDictionary<string, object?> {
            ["app_id"] = appId,
            ["attempted"] = attempted,
            ["code"] = code,
            ["elapsed_ms"] = elapsed,
            ["key_hash"] = SessionFacts.Sha256(key),
            ["operation_id"] = canonical,
            ["session_hash"] = SessionFacts.Sha256(sessionId),
            ["stage"] = stage,
            ["time"] = time,
        };
        if (!Hash.IsMatch((string)record["session_hash"]!) || !Hash.IsMatch((string)record["key_hash"]!)) return;
        var line = JsonSerializer.Serialize(record);
        if (EncodingBytes(line) > 1024) return;
        var path = Path.Combine(directory, "operation-receipts.jsonl");
        var gate = Path.Combine(directory, "operation-receipts.lock");
        Directory.CreateDirectory(directory);
        try {
            using var locked = new FileStream(gate, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
            var rows = File.Exists(path) && new FileInfo(path).Length <= 1024 * 1024
                ? File.ReadAllLines(path).Where(item => item.Length <= 1024).TakeLast(2048).ToList()
                : [];
            rows.Add(line);
            var bounded = rows.TakeLast(128 * 16).ToList();
            AtomicFile.Write(directory, "operation-receipts.jsonl", System.Text.Encoding.UTF8.GetBytes(string.Join('\n', bounded) + "\n"));
        } catch (IOException) {
            // Another writer holds the receipt lock. Losing one metadata row is reported by its absence.
        }
    }

    public static string Summary(string directory) {
        var path = Path.Combine(directory, "operation-receipts.jsonl");
        if (!File.Exists(path)) return "尚无操作回执";
        var info = new FileInfo(path);
        if (info.Length > 262_144) return "尚无操作回执";
        var events = new Dictionary<string, List<string>>(StringComparer.Ordinal);
        var order = new List<string>();
        foreach (var line in File.ReadLines(path).TakeLast(2048)) {
            if (line.Length > 1024) continue;
            try {
                using var document = JsonDocument.Parse(line);
                var row = document.RootElement;
                var id = row.TryGetProperty("operation_id", out var idValue) ? idValue.GetString() : null;
                var app = row.TryGetProperty("app_id", out var appValue) ? appValue.GetString() : null;
                var stage = row.TryGetProperty("stage", out var stageValue) ? stageValue.GetString() : null;
                var code = row.TryGetProperty("code", out var codeValue) ? codeValue.GetString() : null;
                if (id == null || id.Length != 32 || app == null || stage == null || code == null) continue;
                if (!Apps.Contains(app) || !Stages.Contains(stage) || !Code.IsMatch(code)) continue;
                if (!events.ContainsKey(id)) { order.Add(id); events[id] = []; }
                events[id].Add(app + " · " + stage + " · " + code);
            } catch (JsonException) { }
        }
        if (order.Count == 0) return "尚无操作回执";
        return string.Join('\n', order.TakeLast(8).Select(id => "操作 " + id[..8] + "：" + string.Join(" → ", events[id].TakeLast(8))));
    }

    static string? Canonical(string value) {
        var text = value.Trim().ToLowerInvariant();
        if (text.Length == 32 && text.All(Uri.IsHexDigit)) return text;
        if (Guid.TryParse(text, out var guid)) return guid.ToString("N");
        return null;
    }

    static int EncodingBytes(string line) => System.Text.Encoding.UTF8.GetByteCount(line);
}

public static class HealthLog {
    public static void Write(string directory, IReadOnlyDictionary<string, object?> fields) {
        using var stream = new MemoryStream();
        using (var writer = new Utf8JsonWriter(stream)) {
            writer.WriteStartObject();
            foreach (var pair in fields.OrderBy(pair => pair.Key, StringComparer.Ordinal)) {
                switch (pair.Value) {
                    case bool flag: writer.WriteBoolean(pair.Key, flag); break;
                    case int number: writer.WriteNumber(pair.Key, number); break;
                    case double number when double.IsFinite(number): writer.WriteNumber(pair.Key, number); break;
                    case string text when !text.Contains('/') && !text.Contains('\\') && !text.Contains('@'):
                        writer.WriteString(pair.Key, text.Length > 500 ? text[..500] : text); break;
                    case IEnumerable<string> items:
                        writer.WriteStartArray(pair.Key);
                        foreach (var item in items.Take(32)) {
                            if (!item.Contains('/') && !item.Contains('\\')) writer.WriteStringValue(item);
                        }
                        writer.WriteEndArray();
                        break;
                }
            }
            writer.WriteEndObject();
        }
        try { AtomicFile.Write(directory, "health.json", stream.ToArray()); }
        catch (IOException) { }
    }
}

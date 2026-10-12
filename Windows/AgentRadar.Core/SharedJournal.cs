using System.Globalization;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace AgentRadar.Core;

public readonly record struct JournalVerdict(string? Code, double? RetryAfter);

/// <summary>Same continuation-attempts.json shape as supervisor/bridge.py Journal.</summary>
public sealed class SharedJournal {
    public const int MaxRecords = 10000;
    public const int MaxBytes = 4 * 1024 * 1024;
    readonly string _directory;
    static readonly Regex Hash = new("^[0-9a-f]{64}$", RegexOptions.CultureInvariant | RegexOptions.Compiled);
    static readonly Regex LegacyKey = new(@"^(.+):([0-9]{12,16})$", RegexOptions.CultureInvariant | RegexOptions.Compiled);

    public SharedJournal(string directory) { _directory = directory; }

    public JournalVerdict Check(IReadOnlyDictionary<string, object?> request, double now) {
        return WithLock(state => Check(state, request, now));
    }

    public JournalVerdict Reserve(IReadOnlyDictionary<string, object?> request, double now) {
        return WithLock(state => {
            var verdict = Check(state, request, now);
            if (verdict.Code != null) return verdict;
            var sid = SessionFacts.Sha256(Text(request, "id"));
            var key = SessionFacts.Sha256(Text(request, "key"));
            var start = Start(request);
            if (state.Rows.Any(row => row.Key == key) || state.Latest.TryGetValue(sid, out var existing) && existing.Key == key)
                return new JournalVerdict(null, null);
            var rows = state.Rows.Where(row => now - row.Time < 7 * 86400).TakeLast(4095).ToList();
            rows.Add(new Row(key, sid, now, start));
            if (start is double started) state.Latest[sid] = new Mark(key, started);
            else state.Legacy.Add(key);
            if (state.Latest.Count + state.Legacy.Count > MaxRecords) return new JournalVerdict("journal_capacity", null);
            var payload = Encode(rows, state.Latest, state.Legacy);
            if (payload.Length > MaxBytes) return new JournalVerdict("journal_capacity", null);
            AtomicFile.Write(_directory, "continuation-attempts.json", payload);
            return new JournalVerdict(null, null);
        });
    }

    JournalVerdict WithLock(Func<State, JournalVerdict> action) {
        Directory.CreateDirectory(_directory);
        var lockPath = Path.Combine(_directory, "continuation.lock");
        FileStream? locked = null;
        try {
            locked = new FileStream(lockPath, FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None);
            var state = Load();
            if (state.Unreadable) return new JournalVerdict("journal_unreadable", null);
            return action(state);
        } catch (IOException) {
            return new JournalVerdict("another_recovery", null);
        } finally {
            locked?.Dispose();
        }
    }

    static JournalVerdict Check(State state, IReadOnlyDictionary<string, object?> request, double now) {
        if (state.Unreadable) return new JournalVerdict("journal_unreadable", null);
        var id = Text(request, "id");
        var keyText = Text(request, "key");
        if (id.Length == 0 || keyText.Length == 0) return new JournalVerdict("invalid_request", null);
        var sid = SessionFacts.Sha256(id);
        var key = SessionFacts.Sha256(keyText);
        var start = Start(request);
        var rage = Text(request, "automation_mode") == "rage";
        if (rage && start is null) return new JournalVerdict("invalid_request", null);
        state.Latest.TryGetValue(sid, out var mark);
        if (state.Legacy.Contains(key) || state.Rows.Any(row => row.Key == key) || mark != null && (mark.Key == key || start is double seen && seen <= mark.Start))
            return new JournalVerdict("already_attempted", null);
        var needsRecord = start is null ? !state.Legacy.Contains(key) : !state.Latest.ContainsKey(sid);
        if (needsRecord && state.Latest.Count + state.Legacy.Count >= MaxRecords) return new JournalVerdict("journal_capacity", null);
        var recent = state.Rows.Where(row => now - row.Time < 3600).ToList();
        var same = recent.Where(row => row.Session == sid).ToList();
        var waits = new List<double>();
        if (rage) {
            var seconds = Text(request, "kind") == "followup" ? 5 : 0;
            waits.AddRange(same.Select(row => seconds - (now - row.Time)));
        } else if(request.TryGetValue("continuous_authorized",out var care)&&care is true&&Text(request,"app_id")=="openclaw") {
            if(recent.Count>=8)waits.Add(3600-(now-recent.Select(r=>r.Time).Order().ElementAt(recent.Count-8)));
        } else {
            waits.AddRange(same.Select(row => 300 - (now - row.Time)));
            if (recent.Count >= 8) waits.Add(3600 - (now - recent.Select(row => row.Time).Order().ElementAt(recent.Count - 8)));
            if (same.Count >= 2) waits.Add(3600 - (now - same.Select(row => row.Time).Order().ElementAt(same.Count - 2)));
        }
        var wait = waits.Count == 0 ? 0 : waits.Max();
        if (wait > 0) return new JournalVerdict("cooldown", Math.Max(1, Math.Ceiling(wait)));
        return new JournalVerdict(null, null);
    }

    State Load() {
        var path = Path.Combine(_directory, "continuation-attempts.json");
        var state = new State();
        if (!File.Exists(path)) return state;
        try {
            if (File.ResolveLinkTarget(path, false) != null) return State.Bad();
            if (new FileInfo(path).Length > MaxBytes) return State.Bad();
            using var document = JsonDocument.Parse(File.ReadAllBytes(path));
            var root = document.RootElement;
            JsonElement rows;
            if (root.ValueKind == JsonValueKind.Array) rows = root;
            else if (root.ValueKind == JsonValueKind.Object && root.TryGetProperty("version", out var version) && version.GetInt32() == 2 && root.TryGetProperty("rows", out rows)) {
                if (root.TryGetProperty("legacy", out var legacy)) {
                    if (legacy.ValueKind != JsonValueKind.Array) return State.Bad();
                    foreach (var item in legacy.EnumerateArray()) {
                        var text = item.GetString();
                        if (text == null || !Hash.IsMatch(text)) return State.Bad();
                        state.Legacy.Add(text);
                    }
                }
                if (root.TryGetProperty("latest", out var latest)) {
                    if (latest.ValueKind != JsonValueKind.Object) return State.Bad();
                    foreach (var property in latest.EnumerateObject()) {
                        if (!Hash.IsMatch(property.Name) || property.Value.ValueKind != JsonValueKind.Object) return State.Bad();
                        var key = property.Value.TryGetProperty("key", out var keyValue) ? keyValue.GetString() : null;
                        if (key == null || !Hash.IsMatch(key) || !property.Value.TryGetProperty("start", out var startValue) || !ValidStart(startValue)) return State.Bad();
                        state.Latest[property.Name] = new Mark(key, startValue.GetDouble());
                    }
                }
            } else return State.Bad();
            if (rows.ValueKind != JsonValueKind.Array) return State.Bad();
            foreach (var row in rows.EnumerateArray()) {
                if (row.ValueKind != JsonValueKind.Object || !row.TryGetProperty("time", out var time) || !time.TryGetDouble(out var stamp) || !double.IsFinite(stamp)) return State.Bad();
                var key = row.TryGetProperty("key", out var keyValue) ? keyValue.GetString() : null;
                var session = row.TryGetProperty("session", out var sessionValue) ? sessionValue.GetString() : null;
                double? start = null;
                if (row.TryGetProperty("start", out var startValue)) {
                    if (!ValidStart(startValue)) return State.Bad();
                    start = startValue.GetDouble();
                }
                if (key == null || !Hash.IsMatch(key)) {
                    var match = key == null ? null : LegacyKey.Match(key);
                    if (match == null || !match.Success || session != match.Groups[1].Value && session != SessionFacts.Sha256(match.Groups[1].Value)) return State.Bad();
                    start = int.Parse(match.Groups[2].Value, CultureInfo.InvariantCulture) / 1000.0;
                    if (start is not > 0) return State.Bad();
                    key = SessionFacts.Sha256(key!);
                    session = SessionFacts.Sha256(match.Groups[1].Value);
                }
                if (session == null || !Hash.IsMatch(session)) return State.Bad();
                state.Rows.Add(new Row(key, session, stamp, start));
                if (start is double started && (!state.Latest.TryGetValue(session, out var mark) || started > mark.Start))
                    state.Latest[session] = new Mark(key, started);
            }
            foreach (var row in state.Rows.ToList()) {
                state.Latest.TryGetValue(row.Session, out var mark);
                var covered = mark != null && (row.Key == mark.Key || row.Start is double rowStart && rowStart <= mark.Start);
                if (!covered) state.Legacy.Add(row.Key);
            }
            if (state.Latest.Count + state.Legacy.Count > MaxRecords) return State.Bad();
            return state;
        } catch (Exception ex) when (ex is IOException or JsonException or InvalidOperationException or FormatException) {
            return State.Bad();
        }
    }

    static byte[] Encode(List<Row> rows, Dictionary<string, Mark> latest, HashSet<string> legacy) {
        using var stream = new MemoryStream();
        using (var writer = new Utf8JsonWriter(stream)) {
            writer.WriteStartObject();
            writer.WriteNumber("version", 2);
            writer.WriteStartArray("rows");
            foreach (var row in rows) {
                writer.WriteStartObject();
                writer.WriteString("key", row.Key);
                writer.WriteString("session", row.Session);
                writer.WriteNumber("time", row.Time);
                if (row.Start is double start) writer.WriteNumber("start", start);
                writer.WriteEndObject();
            }
            writer.WriteEndArray();
            writer.WriteStartObject("latest");
            foreach (var pair in latest.OrderBy(pair => pair.Key, StringComparer.Ordinal)) {
                writer.WriteStartObject(pair.Key);
                writer.WriteString("key", pair.Value.Key);
                writer.WriteNumber("start", pair.Value.Start);
                writer.WriteEndObject();
            }
            writer.WriteEndObject();
            writer.WriteStartArray("legacy");
            foreach (var item in legacy.Order(StringComparer.Ordinal)) writer.WriteStringValue(item);
            writer.WriteEndArray();
            writer.WriteEndObject();
        }
        return stream.ToArray();
    }

    static bool ValidStart(JsonElement value) => value.ValueKind == JsonValueKind.Number && value.TryGetDouble(out var number) && double.IsFinite(number) && number > 0;
    static double? Start(IReadOnlyDictionary<string, object?> request) {
        if (!request.TryGetValue("started_at", out var value) || value is not double number || !double.IsFinite(number) || number <= 0) return null;
        return number;
    }
    static string Text(IReadOnlyDictionary<string, object?> request, string key) => request.TryGetValue(key, out var value) && value is string text ? text : "";

    sealed class State {
        public List<Row> Rows { get; } = [];
        public Dictionary<string, Mark> Latest { get; } = [];
        public HashSet<string> Legacy { get; } = [];
        public bool Unreadable { get; private set; }
        public static State Bad() => new() { Unreadable = true };
    }

    sealed record Row(string Key, string Session, double Time, double? Start);
    sealed record Mark(string Key, double Start);
}

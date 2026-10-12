using System.Globalization;
using System.Text.Json;

namespace AgentRadar.Core;

public sealed class SettingsRepository {
    readonly string _directory;
    public string DataDirectory => _directory;
    public bool LastLoadFailed { get; private set; }

    public SettingsRepository(string directory) {
        if (string.IsNullOrWhiteSpace(directory)) throw new ArgumentException("directory");
        _directory = Path.GetFullPath(directory);
    }

    public RadarSettings Load() {
        var path = Path.Combine(_directory, "settings.json");
        LastLoadFailed = false;
        if (!File.Exists(path)) return new RadarSettings();
        try {
            if (File.ResolveLinkTarget(path, false) != null) {
                LastLoadFailed = true;
                return new RadarSettings();
            }
            var info = new FileInfo(path);
            if (info.Length > 1024 * 1024) {
                LastLoadFailed = true;
                return new RadarSettings();
            }
            using var document = JsonDocument.Parse(File.ReadAllBytes(path));
            if (document.RootElement.ValueKind != JsonValueKind.Object) {
                LastLoadFailed = true;
                return new RadarSettings();
            }
            return Normalize(Read(document.RootElement));
        } catch (Exception ex) when (ex is IOException or JsonException or InvalidOperationException) {
            LastLoadFailed = true;
            return new RadarSettings();
        }
    }

    public void Save(RadarSettings settings) {
        var clean = Normalize(Clone(settings));
        CopyInto(settings, clean);
        using var stream = new MemoryStream();
        using (var writer = new Utf8JsonWriter(stream, new JsonWriterOptions { Indented = true })) Write(writer, clean);
        AtomicFile.Write(_directory, "settings.json", stream.ToArray());
    }

    public void WriteCareRegistry(RadarSettings settings) {
        var care = ValidIds(settings.CareIds);
        if(care.Count>40)throw new IOException("care registry capacity");
        var existing = ReadRegistryIds();
        foreach (var id in care) existing.Add(id);
        var kept = existing.Where(id => care.Contains(id) || ValidIds([id]).Count == 1).ToList();
        if(existing.Union(care).Count()>40)throw new IOException("care registry capacity");
        var ids = kept.Where(id => StagePolicy.OpenClawId.IsMatch(id)).Distinct(StringComparer.Ordinal).Order(StringComparer.Ordinal).ToList();
        var careIds = care.Order(StringComparer.Ordinal).ToList();
        using var stream = new MemoryStream();
        using (var writer = new Utf8JsonWriter(stream)) {
            writer.WriteStartObject();
            writer.WriteNumber("version", 1);
            WriteArray(writer, "ids", ids);
            WriteArray(writer, "care_ids", careIds);
            writer.WriteEndObject();
        }
        AtomicFile.Write(_directory, "selected-openclaw-sessions.json", stream.ToArray());
    }

    public HashSet<string> ReadCareIds() {
        var path = Path.Combine(_directory, "selected-openclaw-sessions.json");
        if (!File.Exists(path)) return [];
        if(new FileInfo(path).Length>65536 || File.ResolveLinkTarget(path,false)!=null)throw new IOException("care registry invalid");
        try {
            using var document = JsonDocument.Parse(File.ReadAllBytes(path));
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object || !root.TryGetProperty("version", out var version) || version.GetInt32() != 1) return [];
            var ids=ReadIdArray(root,"ids");var care=ReadIdArray(root,"care_ids");if(!care.IsSubsetOf(ids))return [];return care;
        } catch (Exception ex) when (ex is IOException or JsonException or InvalidOperationException) {
            return [];
        }
    }

    HashSet<string> ReadRegistryIds() {
        var path=Path.Combine(_directory,"selected-openclaw-sessions.json");if(!File.Exists(path))return [];
        if(new FileInfo(path).Length>65536||File.ResolveLinkTarget(path,false)!=null)throw new IOException("care registry invalid");
        using var d=JsonDocument.Parse(File.ReadAllBytes(path));var r=d.RootElement;
        if(!r.TryGetProperty("version",out var v)||v.GetInt32()!=1)throw new IOException("care registry invalid");
        return ReadIdArray(r,"ids");
    }

    static HashSet<string> ReadIdArray(JsonElement root, string name) {
        var result = new HashSet<string>(StringComparer.Ordinal);
        if (!root.TryGetProperty(name, out var array) || array.ValueKind != JsonValueKind.Array) return result;
        if(array.GetArrayLength()>40)throw new IOException("care registry capacity");
        foreach (var item in array.EnumerateArray()) {
            if (item.ValueKind != JsonValueKind.String) continue;
            var value = item.GetString();
            if (value != null && StagePolicy.OpenClawId.IsMatch(value)) result.Add(value);
        }
        return result;
    }

    static HashSet<string> ValidIds(IEnumerable<string> values) {
        var result = new HashSet<string>(StringComparer.Ordinal);
        foreach (var value in values) if (StagePolicy.OpenClawId.IsMatch(value)) result.Add(value);
        return result;
    }

    public static RadarSettings Clone(RadarSettings source) {
        return new RadarSettings {
            AutoSend = source.AutoSend,
            Paused = source.Paused,
            Mode = source.Mode,
            Popup = source.Popup,
            Sound = source.Sound,
            KeepAwake = source.KeepAwake,
            StartWithWindows = source.StartWithWindows,
            Expanded = source.Expanded,
            X = source.X,
            Y = source.Y,
            Transparency = source.Transparency,
            Frost = source.Frost,
            ResumeText = source.ResumeText,
            InterruptText = source.InterruptText,
            ContinueText = source.ContinueText,
            AlternateText = source.AlternateText,
            AlternateEvery = source.AlternateEvery,
            RateLimitWake = source.RateLimitWake,
            RateLimitSeconds = source.RateLimitSeconds,
            RateLimitText = source.RateLimitText,
            AppContinueTexts = new Dictionary<string, string>(source.AppContinueTexts),
            AppResumeTexts = new Dictionary<string, string>(source.AppResumeTexts),
            SupervisedIds = new HashSet<string>(source.SupervisedIds),
            SupervisedApps = new HashSet<string>(source.SupervisedApps),
            CareIds = new HashSet<string>(source.CareIds),
            RemovedIds = new HashSet<string>(source.RemovedIds),
            SendCounts = new Dictionary<string, int>(source.SendCounts),
            Labels = new Dictionary<string, string>(source.Labels),
            Pins = new HashSet<string>(source.Pins),
            OpenClawGateway = source.OpenClawGateway,
            OpenClawHome = source.OpenClawHome,
            WslDistribution = source.WslDistribution,
            WslExecutable = source.WslExecutable,
        };
    }

    static void CopyInto(RadarSettings target, RadarSettings source) {
        target.AutoSend = source.AutoSend;
        target.Paused = source.Paused;
        target.Mode = source.Mode;
        target.Popup = source.Popup;
        target.Sound = source.Sound;
        target.KeepAwake = source.KeepAwake;
        target.StartWithWindows = source.StartWithWindows;
        target.Expanded = source.Expanded;
        target.X = source.X;
        target.Y = source.Y;
        target.Transparency = source.Transparency;
        target.Frost = source.Frost;
        target.ResumeText = source.ResumeText;
        target.InterruptText = source.InterruptText;
        target.ContinueText = source.ContinueText;
        target.AlternateText = source.AlternateText;
        target.AlternateEvery = source.AlternateEvery;
        target.RateLimitWake = source.RateLimitWake;
        target.RateLimitSeconds = source.RateLimitSeconds;
        target.RateLimitText = source.RateLimitText;
        target.AppContinueTexts = source.AppContinueTexts;
        target.AppResumeTexts = source.AppResumeTexts;
        target.SupervisedIds = source.SupervisedIds;
        target.SupervisedApps = source.SupervisedApps;
        target.CareIds = source.CareIds;
        target.RemovedIds = source.RemovedIds;
        target.SendCounts = source.SendCounts;
        target.Labels = source.Labels;
        target.Pins = source.Pins;
        target.OpenClawGateway = source.OpenClawGateway;
        target.OpenClawHome = source.OpenClawHome;
        target.WslDistribution = source.WslDistribution;
        target.WslExecutable = source.WslExecutable;
    }

    static RadarSettings Normalize(RadarSettings settings) {
        settings.Mode = settings.Mode == "rage" ? "rage" : "collaboration";
        settings.RateLimitSeconds = Math.Clamp(settings.RateLimitSeconds, 1, 10);
        settings.AlternateEvery = Math.Clamp(settings.AlternateEvery == 0 ? 3 : settings.AlternateEvery, 2, 100);
        if (!double.IsFinite(settings.Transparency)) settings.Transparency = 0.75;
        settings.Transparency = Math.Clamp(settings.Transparency, 0.70, 0.80);
        settings.Frost = settings.Frost is "关闭" or "轻薄" or "适中" or "浓郁" ? settings.Frost : "适中";
        if (!StagePolicy.LoopbackGateway(settings.OpenClawGateway)) settings.OpenClawGateway = "ws://127.0.0.1:18789";
        if (!StagePolicy.LocalPath(settings.OpenClawHome, allowWslUnc: true, settings.WslDistribution)) settings.OpenClawHome = "";
        if (!StagePolicy.LocalPath(settings.WslExecutable, allowWslUnc: false, null)) settings.WslExecutable = "/usr/local/bin/openclaw";
        settings.CareIds = ValidIds(settings.CareIds);
        return settings;
    }

    static RadarSettings Read(JsonElement root) {
        var settings = new RadarSettings();
        settings.AutoSend = Bool(root, "AutoSend", false);
        settings.Paused = Bool(root, "Paused", false);
        settings.Mode = Text(root, "Mode", "collaboration");
        settings.Popup = Bool(root, "Popup", true);
        settings.Sound = Bool(root, "Sound", true);
        settings.KeepAwake = Bool(root, "KeepAwake", false);
        settings.StartWithWindows = Bool(root, "StartWithWindows", false);
        settings.Expanded = Bool(root, "Expanded", false);
        settings.X = Finite(root, "X");
        settings.Y = Finite(root, "Y");
        settings.Transparency = Number(root, "Transparency", 0.75);
        settings.Frost = Text(root, "Frost", "适中");
        settings.ResumeText = Text(root, "ResumeText", "");
        settings.InterruptText = Text(root, "InterruptText", settings.InterruptText);
        settings.ContinueText = Text(root, "ContinueText", settings.ContinueText);
        settings.AlternateText = Text(root, "AlternateText", settings.AlternateText);
        settings.AlternateEvery = (int)Number(root, "AlternateEvery", 3);
        settings.RateLimitWake = Bool(root, "RateLimitWake", false);
        settings.RateLimitSeconds = (int)Number(root, "RateLimitSeconds", 10);
        settings.RateLimitText = Text(root, "RateLimitText", settings.RateLimitText);
        settings.AppContinueTexts = Map(root, "AppContinueTexts");
        settings.AppResumeTexts = Map(root, "AppResumeTexts");
        settings.SupervisedIds = Set(root, "SupervisedIds");
        settings.SupervisedApps = Set(root, "SupervisedApps");
        settings.CareIds = Set(root, "CareIds");
        settings.RemovedIds = Set(root, "RemovedIds");
        settings.SendCounts = IntMap(root, "SendCounts");
        settings.Labels = Map(root, "Labels");
        settings.Pins = Set(root, "Pins");
        settings.OpenClawGateway = Text(root, "OpenClawGateway", settings.OpenClawGateway);
        settings.OpenClawHome = Text(root, "OpenClawHome", "");
        settings.WslDistribution = Text(root, "WslDistribution", "");
        settings.WslExecutable = Text(root, "WslExecutable", settings.WslExecutable);
        return settings;
    }

    static void Write(Utf8JsonWriter writer, RadarSettings settings) {
        writer.WriteStartObject();
        writer.WriteBoolean("AutoSend", settings.AutoSend);
        writer.WriteBoolean("Paused", settings.Paused);
        writer.WriteString("Mode", settings.Mode);
        writer.WriteBoolean("Popup", settings.Popup);
        writer.WriteBoolean("Sound", settings.Sound);
        writer.WriteBoolean("KeepAwake", settings.KeepAwake);
        writer.WriteBoolean("StartWithWindows", settings.StartWithWindows);
        writer.WriteBoolean("Expanded", settings.Expanded);
        WriteFinite(writer, "X", settings.X);
        WriteFinite(writer, "Y", settings.Y);
        writer.WriteNumber("Transparency", settings.Transparency);
        writer.WriteString("Frost", settings.Frost);
        writer.WriteString("ResumeText", settings.ResumeText);
        writer.WriteString("InterruptText", settings.InterruptText);
        writer.WriteString("ContinueText", settings.ContinueText);
        writer.WriteString("AlternateText", settings.AlternateText);
        writer.WriteNumber("AlternateEvery", settings.AlternateEvery);
        writer.WriteBoolean("RateLimitWake", settings.RateLimitWake);
        writer.WriteNumber("RateLimitSeconds", settings.RateLimitSeconds);
        writer.WriteString("RateLimitText", settings.RateLimitText);
        WriteMap(writer, "AppContinueTexts", settings.AppContinueTexts);
        WriteMap(writer, "AppResumeTexts", settings.AppResumeTexts);
        WriteArray(writer, "SupervisedIds", settings.SupervisedIds.Order(StringComparer.Ordinal));
        WriteArray(writer, "SupervisedApps", settings.SupervisedApps.Order(StringComparer.Ordinal));
        WriteArray(writer, "CareIds", settings.CareIds.Order(StringComparer.Ordinal));
        WriteArray(writer, "RemovedIds", settings.RemovedIds.Order(StringComparer.Ordinal));
        WriteIntMap(writer, "SendCounts", settings.SendCounts);
        WriteMap(writer, "Labels", settings.Labels);
        WriteArray(writer, "Pins", settings.Pins.Order(StringComparer.Ordinal));
        writer.WriteString("OpenClawGateway", settings.OpenClawGateway);
        writer.WriteString("OpenClawHome", settings.OpenClawHome);
        writer.WriteString("WslDistribution", settings.WslDistribution);
        writer.WriteString("WslExecutable", settings.WslExecutable);
        writer.WriteEndObject();
    }

    static void WriteFinite(Utf8JsonWriter writer, string name, double value) {
        if (double.IsFinite(value)) writer.WriteNumber(name, value);
        else writer.WriteNull(name);
    }

    static void WriteArray(Utf8JsonWriter writer, string name, IEnumerable<string> values) {
        writer.WriteStartArray(name);
        foreach (var value in values) writer.WriteStringValue(value);
        writer.WriteEndArray();
    }

    static void WriteMap(Utf8JsonWriter writer, string name, Dictionary<string, string> values) {
        writer.WriteStartObject(name);
        foreach (var pair in values.OrderBy(pair => pair.Key, StringComparer.Ordinal)) writer.WriteString(pair.Key, pair.Value);
        writer.WriteEndObject();
    }

    static void WriteIntMap(Utf8JsonWriter writer, string name, Dictionary<string, int> values) {
        writer.WriteStartObject(name);
        foreach (var pair in values.OrderBy(pair => pair.Key, StringComparer.Ordinal)) writer.WriteNumber(pair.Key, pair.Value);
        writer.WriteEndObject();
    }

    static bool Bool(JsonElement root, string name, bool fallback) {
        return root.TryGetProperty(name, out var value) && value.ValueKind is JsonValueKind.True or JsonValueKind.False
            ? value.GetBoolean() : fallback;
    }

    static string Text(JsonElement root, string name, string fallback) {
        return root.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() ?? fallback : fallback;
    }

    static double Number(JsonElement root, string name, double fallback) {
        if (!root.TryGetProperty(name, out var value) || value.ValueKind != JsonValueKind.Number) return fallback;
        var number = value.GetDouble();
        return double.IsFinite(number) ? number : fallback;
    }

    static double Finite(JsonElement root, string name) {
        if (!root.TryGetProperty(name, out var value) || value.ValueKind == JsonValueKind.Null) return double.NaN;
        if (value.ValueKind != JsonValueKind.Number) return double.NaN;
        var number = value.GetDouble();
        return double.IsFinite(number) ? number : double.NaN;
    }

    static HashSet<string> Set(JsonElement root, string name) {
        var result = new HashSet<string>(StringComparer.Ordinal);
        if (!root.TryGetProperty(name, out var value) || value.ValueKind != JsonValueKind.Array) return result;
        foreach (var item in value.EnumerateArray()) if (item.ValueKind == JsonValueKind.String && item.GetString() is string text) result.Add(text);
        return result;
    }

    static Dictionary<string, string> Map(JsonElement root, string name) {
        var result = new Dictionary<string, string>(StringComparer.Ordinal);
        if (!root.TryGetProperty(name, out var value) || value.ValueKind != JsonValueKind.Object) return result;
        foreach (var property in value.EnumerateObject()) if (property.Value.ValueKind == JsonValueKind.String && property.Value.GetString() is string text) result[property.Name] = text;
        return result;
    }

    static Dictionary<string, int> IntMap(JsonElement root, string name) {
        var result = new Dictionary<string, int>(StringComparer.Ordinal);
        if (!root.TryGetProperty(name, out var value) || value.ValueKind != JsonValueKind.Object) return result;
        foreach (var property in value.EnumerateObject()) {
            if (property.Value.ValueKind == JsonValueKind.Number && property.Value.TryGetInt32(out var number) && number >= 0) result[property.Name] = number;
        }
        return result;
    }
}

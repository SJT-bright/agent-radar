using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text;
using AgentRadar.Core;

namespace AgentRadar.Windows.Ui;

internal readonly record struct EffectivePrompt(string Role, string Text, string Source, string? AppId);

internal sealed class PromptDraft
{
    public const string GrokContinue = "请继续当前项目。先完成尚未完成的工作，再检查已有改动，找出下一项有价值的优化并实施；完成后实际测试，说明改动、验证结果和剩余问题。";
    public const string GrokResume = "刚才中断了，请继续未完成的工作；恢复后从真实使用者的角度检查体验，提出可验证的建议并实施优化，完成后说明验证结果。";
    public const string FixedResume = "刚才中断了，请继续";
    public const int MaxLength = 2000;

    public bool Popup = true;
    public bool Sound = true;
    public string ResumeText = "";
    public string InterruptText = "";
    public string ContinueText = "";
    public string AlternateText = "";
    public string AlternateEveryText = "4";
    public bool RateLimitWake;
    public string RateLimitSecondsText = "10";
    public string RateLimitText = "";
    public Dictionary<string, string> AppContinue = new(StringComparer.Ordinal);
    public Dictionary<string, string> AppResume = new(StringComparer.Ordinal);
    public string Gateway = "";
    public string Home = "";
    public string Distribution = "";
    public string Executable = "";
    public string SelectedApp = "grok";

    public static PromptDraft Capture(RadarSettings settings)
    {
        var draft = new PromptDraft
        {
            Popup = settings.Popup,
            Sound = settings.Sound,
            ResumeText = settings.ResumeText,
            InterruptText = settings.InterruptText ?? "",
            ContinueText = settings.ContinueText ?? "",
            AlternateText = settings.AlternateText ?? "",
            AlternateEveryText = settings.AlternateEvery.ToString(CultureInfo.InvariantCulture),
            RateLimitWake = settings.RateLimitWake,
            RateLimitSecondsText = settings.RateLimitSeconds.ToString(CultureInfo.InvariantCulture),
            RateLimitText = settings.RateLimitText ?? "",
            Gateway = settings.OpenClawGateway ?? "",
            Home = settings.OpenClawHome ?? "",
            Distribution = settings.WslDistribution ?? "",
            Executable = settings.WslExecutable ?? ""
        };
        foreach (var pair in settings.AppContinueTexts) draft.AppContinue[pair.Key] = pair.Value ?? "";
        foreach (var pair in settings.AppResumeTexts) draft.AppResume[pair.Key] = pair.Value ?? "";
        return draft;
    }

    public bool HasChanges(RadarSettings settings)
    {
        if(ResumeText!=settings.ResumeText)return true;
        if (Popup != settings.Popup || Sound != settings.Sound) return true;
        if (InterruptText != (settings.InterruptText ?? "") || ContinueText != (settings.ContinueText ?? "")) return true;
        if (AlternateText != (settings.AlternateText ?? "") || RateLimitText != (settings.RateLimitText ?? "")) return true;
        if (RateLimitWake != settings.RateLimitWake) return true;
        if (AlternateEveryText.Trim() != settings.AlternateEvery.ToString(CultureInfo.InvariantCulture)) return true;
        if (RateLimitSecondsText.Trim() != settings.RateLimitSeconds.ToString(CultureInfo.InvariantCulture)) return true;
        if (Gateway != (settings.OpenClawGateway ?? "") || Home != (settings.OpenClawHome ?? "")) return true;
        if (Distribution != (settings.WslDistribution ?? "") || Executable != (settings.WslExecutable ?? "")) return true;
        if (MapDirty(AppContinue, settings.AppContinueTexts)) return true;
        if (MapDirty(AppResume, settings.AppResumeTexts)) return true;
        return false;
    }

    public bool HasOverlength => PromptFields().Any(item => ScalarCount(item.Text) > MaxLength);

    public IReadOnlyList<string> ValidationMessages()
    {
        var messages = new List<string>();
        foreach (var field in PromptFields()) AppendText(messages, field.Label, field.Text, field.EmptyUsesDefault, field.EmptyAppNote);
        if (!int.TryParse(RateLimitSecondsText.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out var seconds) || seconds < 1 || seconds > 10)
            messages.Add("限流等待时间须为 1–10 秒，完整输入已保留。");
        if (!int.TryParse(AlternateEveryText.Trim(), NumberStyles.Integer, CultureInfo.InvariantCulture, out var every) || every < 2 || every > 100)
            messages.Add("特别提示词间隔须为 2–100 次，完整输入已保留。");
        if (!IsLoopbackWebSocket(Gateway))
            messages.Add("OpenClaw 网关只接受本机 ws 地址，例如 ws://127.0.0.1:18789。");
        if (IsForbiddenUnc(Home) || HasControl(Home))
            messages.Add("OpenClaw 主目录只接受本机路径或本机 WSL 路径，外部共享路径不会保存。");
        if (Distribution.IndexOfAny(['/', '\\']) >= 0 || HasControl(Distribution))
            messages.Add("WSL 发行版名称不能包含斜杠或控制字符。");
        if (string.IsNullOrWhiteSpace(Executable) || IsForbiddenUnc(Executable) || HasControl(Executable))
            messages.Add("WSL 可执行文件须是本机或 WSL 内路径，不能是外部共享。");
        return messages;
    }

    public bool TryApply(RadarSettings settings, IList<string> notes)
    {
        if (HasOverlength || ValidationMessages().Any(IsBlocking)) return false;
        if (string.IsNullOrWhiteSpace(ContinueText))
        {
            settings.ContinueText = new RadarSettings().ContinueText;
            notes.Add("普通续接为空，已使用默认提示词。");
        }
        else settings.ContinueText = ContinueText;
        if (string.IsNullOrWhiteSpace(AlternateText))
        {
            settings.AlternateText = new RadarSettings().AlternateText;
            notes.Add("特别提示词为空，已使用默认提示词。");
        }
        else settings.AlternateText = AlternateText;
        settings.ResumeText=ResumeText;
        settings.InterruptText = InterruptText;
        settings.RateLimitText = RateLimitText;
        settings.RateLimitWake = RateLimitWake;
        settings.Popup = Popup;
        settings.Sound = Sound;
        settings.AlternateEvery = int.Parse(AlternateEveryText.Trim(), CultureInfo.InvariantCulture);
        settings.RateLimitSeconds = int.Parse(RateLimitSecondsText.Trim(), CultureInfo.InvariantCulture);
        settings.OpenClawGateway = Gateway.Trim();
        settings.OpenClawHome = Home;
        settings.WslDistribution = Distribution.Trim();
        settings.WslExecutable = Executable.Trim();
        settings.AppContinueTexts = Merge(settings.AppContinueTexts, AppContinue);
        settings.AppResumeTexts = Merge(settings.AppResumeTexts, AppResume);
        return true;
    }

    public static EffectivePrompt Continuation(RadarSettings rules, string appId)
    {
        if (rules.AppContinueTexts.TryGetValue(appId, out var dedicated) && !string.IsNullOrWhiteSpace(dedicated))
            return new EffectivePrompt("续接", dedicated, "专属", appId);
        if (appId == "grok") return new EffectivePrompt("续接", GrokContinue, "Grok 软件默认", appId);
        var fallback = new RadarSettings().ContinueText;
        var text = string.IsNullOrWhiteSpace(rules.ContinueText) ? fallback : rules.ContinueText;
        var source = text == fallback ? "通用默认" : "通用";
        return new EffectivePrompt("续接", text, source, appId);
    }

    public static EffectivePrompt Recovery(RadarSettings rules, string appId)
    {
        if (rules.AppResumeTexts.TryGetValue(appId, out var dedicated) && !string.IsNullOrWhiteSpace(dedicated))
            return new EffectivePrompt("恢复", dedicated, "专属", appId);
        if (rules.Mode!="rage" && !string.IsNullOrWhiteSpace(rules.InterruptText))
            return new EffectivePrompt("恢复", rules.InterruptText, "意外中断提示词", appId);
        if(!string.IsNullOrWhiteSpace(rules.ResumeText))return new EffectivePrompt("恢复",rules.ResumeText,"通用恢复",appId);
        if (appId == "grok") return new EffectivePrompt("恢复", GrokResume, "Grok 软件默认", appId);
        return new EffectivePrompt("恢复", FixedResume, "固定默认", appId);
    }

    public static EffectivePrompt GenericContinuation(RadarSettings rules)
    {
        var fallback = new RadarSettings().ContinueText;
        var text = string.IsNullOrWhiteSpace(rules.ContinueText) ? fallback : rules.ContinueText;
        return new EffectivePrompt("通用续接", text, text == fallback ? "通用默认" : "通用", null);
    }

    public static EffectivePrompt Alternate(RadarSettings rules)
    {
        var fallback = new RadarSettings().AlternateText;
        var text = string.IsNullOrWhiteSpace(rules.AlternateText) ? fallback : rules.AlternateText;
        return new EffectivePrompt("特别", text, text == fallback ? "通用默认" : "通用", null);
    }

    public static EffectivePrompt RateLimit(RadarSettings rules) =>
        new("限流", rules.RateLimitText ?? "", "限流提示词", null);

    public static int ScalarCount(string text) => text.EnumerateRunes().Count();

    public RadarSettings Preview(RadarSettings saved)
    {
        var copy = Copy(saved);
        var notes = new List<string>();
        if (!TryApply(copy, notes))
        {
            copy.InterruptText = InterruptText;
            copy.ContinueText = ContinueText;
            copy.AlternateText = AlternateText;
            copy.RateLimitText = RateLimitText;
            copy.AppContinueTexts = new Dictionary<string, string>(AppContinue, StringComparer.Ordinal);
            copy.AppResumeTexts = new Dictionary<string, string>(AppResume, StringComparer.Ordinal);
        }
        return copy;
    }

    public static RadarSettings Copy(RadarSettings source)
    {
        var copy = new RadarSettings
        {
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
            ResumeText=source.ResumeText,
            InterruptText = source.InterruptText,
            ContinueText = source.ContinueText,
            AlternateText = source.AlternateText,
            AlternateEvery = source.AlternateEvery,
            RateLimitWake = source.RateLimitWake,
            RateLimitSeconds = source.RateLimitSeconds,
            RateLimitText = source.RateLimitText,
            OpenClawGateway = source.OpenClawGateway,
            OpenClawHome = source.OpenClawHome,
            WslDistribution = source.WslDistribution,
            WslExecutable = source.WslExecutable
        };
        foreach (var pair in source.AppContinueTexts) copy.AppContinueTexts[pair.Key] = pair.Value;
        foreach (var pair in source.AppResumeTexts) copy.AppResumeTexts[pair.Key] = pair.Value;
        foreach (var id in source.SupervisedIds) copy.SupervisedIds.Add(id);
        foreach (var id in source.SupervisedApps) copy.SupervisedApps.Add(id);
        foreach (var id in source.CareIds) copy.CareIds.Add(id);
        foreach (var id in source.RemovedIds) copy.RemovedIds.Add(id);
        return copy;
    }

    private IEnumerable<(string Label, string Text, bool EmptyUsesDefault, string? EmptyAppNote)> PromptFields()
    {
        yield return ("通用恢复提示词",ResumeText,false,null);
        yield return ("中断提示词", InterruptText, false, null);
        yield return ("限流提示词", RateLimitText, false, null);
        yield return ("普通提示词", ContinueText, true, null);
        yield return ("特别提示词", AlternateText, true, null);
        foreach (var pair in AppContinue.OrderBy(item => item.Key, StringComparer.Ordinal))
        {
            var name = AppNames.Display(pair.Key);
            var note = pair.Key == "grok"
                ? $"{name} 专属提示词为空或清理后为空，保存后不写入专属覆盖；下次普通续接使用 Grok 软件默认，不使用通用续接提示词。"
                : $"{name} 专属提示词为空或清理后为空，保存后该软件使用通用续接提示词。";
            yield return ($"{name} 专属提示词", pair.Value ?? "", false, string.IsNullOrWhiteSpace(pair.Value) ? note : null);
        }
        foreach (var pair in AppResume.OrderBy(item => item.Key, StringComparer.Ordinal))
            yield return ($"{AppNames.Display(pair.Key)} 恢复提示词", pair.Value ?? "", false, null);
    }

    private static void AppendText(List<string> messages, string label, string text, bool emptyUsesDefault, string? emptyAppNote)
    {
        if (ScalarCount(text) > MaxLength)
            messages.Add($"{label}超过 {MaxLength} 字符；完整内容已保留，请删减后保存。");
        if (text.EnumerateRunes().Any(IsRemovedControl))
            messages.Add($"{label}包含控制字符，请删除后再保存；换行和制表符会保留。");
        if (emptyUsesDefault && string.IsNullOrWhiteSpace(text))
            messages.Add($"{label}为空或清理后为空，保存时会使用默认提示词。");
        if (!string.IsNullOrEmpty(emptyAppNote)) messages.Add(emptyAppNote);
    }

    public static bool IsBlocking(string message) =>
        message.Contains("完整内容已保留", StringComparison.Ordinal)
        || message.Contains("完整输入已保留", StringComparison.Ordinal)
        || message.Contains("请删除后再保存", StringComparison.Ordinal)
        || message.Contains("只接受", StringComparison.Ordinal)
        || message.Contains("不能包含", StringComparison.Ordinal)
        || message.Contains("不能是外部共享", StringComparison.Ordinal)
        || message.Contains("不会保存", StringComparison.Ordinal);

    private static bool MapDirty(Dictionary<string, string> draft, Dictionary<string, string> saved)
    {
        foreach (var pair in draft)
        {
            saved.TryGetValue(pair.Key, out var current);
            current ??= "";
            if (string.IsNullOrWhiteSpace(pair.Value))
            {
                if (!string.IsNullOrWhiteSpace(current)) return true;
            }
            else if (pair.Value != current) return true;
        }
        return false;
    }

    private static Dictionary<string, string> Merge(Dictionary<string, string> saved, Dictionary<string, string> draft)
    {
        var result = new Dictionary<string, string>(saved, StringComparer.Ordinal);
        foreach (var pair in draft)
        {
            if (string.IsNullOrWhiteSpace(pair.Value)) result.Remove(pair.Key);
            else result[pair.Key] = pair.Value;
        }
        return result;
    }

    public static bool IsLoopbackWebSocket(string value)=>StagePolicy.LoopbackGateway(value);

    public static bool IsForbiddenUnc(string value)
    {
        var path = value.Trim();
        if (!path.StartsWith("\\\\", StringComparison.Ordinal)) return false;
        if (path.StartsWith("\\\\wsl$\\", StringComparison.OrdinalIgnoreCase)) return false;
        if (path.StartsWith("\\\\wsl.localhost\\", StringComparison.OrdinalIgnoreCase)) return false;
        if (path.StartsWith("\\\\?\\", StringComparison.Ordinal)) return false;
        return true;
    }

    private static bool HasControl(string text) => text.EnumerateRunes().Any(IsRemovedControl);

    private static bool IsRemovedControl(Rune rune)
    {
        if (rune.Value is '\n' or '\t') return false;
        return rune.Value < 32 || rune.Value == 127;
    }
}

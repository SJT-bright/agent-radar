namespace AgentRadar.Core;

public sealed class ContinuationWork {
    public SessionRecord Session { get; set; } = new();
    public string Key { get; set; } = "";
    public double DetectedAt { get; set; }
    public bool CanContinue { get; set; }
    public string Explanation { get; set; } = "";
    public string Kind { get; set; } = "interrupt";
    public string Text { get; set; } = "刚才中断了，请继续";
    public double Delay { get; set; } = 5;
    public double Patience { get; set; } = 600;
    public bool BypassRestriction { get; set; }
    public string AutomationMode { get; set; } = "collaboration";
    public bool Manual { get; set; }
    public double NotBefore { get; set; }
    public bool RecoveryDelayReported { get; set; }
    public int RouteRetries { get; set; }
    public string WaitMessage { get; set; } = "";
    public string OperationId { get; set; } = Guid.NewGuid().ToString("N");
    public bool Care { get; set; }
}

public sealed class ContinuationPolicy {
    public const string ResumeText = "刚才中断了，请继续";
    public const string GrokResumeText = "刚才中断了，请继续未完成的工作；恢复后从真实使用者的角度检查体验，提出可验证的建议并实施优化，完成后说明验证结果。";
    public const string ManualContinueText = "请继续";
    public const string GrokContinueText = "请继续当前项目。先完成尚未完成的工作，再检查已有改动，找出下一项有价值的优化并实施；完成后实际测试，说明改动、验证结果和剩余问题。";
    readonly Dictionary<string, (double Start, double Seen)> _running = [];
    readonly Dictionary<string, Dictionary<string, double>> _announced = [];
    readonly Dictionary<string, List<double>> _attempts = [];

    public void ResetObservation() => _running.Clear();
    public void Forget(IEnumerable<string> ids) { foreach (var id in ids) _running.Remove(id); }

    public static string? IdentityRestriction(SessionRecord row) {
        if (row.UserStopped == true) return "你已主动停止，本轮保持停止";
        if (!SessionFacts.AutomationReadable(row) || row.Source is not ("local-session" or "local-db" or "local-log"))
            return "只有窗口状态或会话不可读，无法确认具体轮次；请手动检查";
        if (row.StartedAt is not double start || !double.IsFinite(start) || start <= 0 || row.TimingBasis is not ("turn" or "last-confirmed"))
            return "缺少真实轮次起点，暂不能自动继续";
        return null;
    }

    public static string? InputRestriction(SessionRecord row) {
        if (row.AppId == "claude-code") return "终端内的具体标签页尚不能核验，请手动继续";
        if (row.AppId == "autoclaw" && (row.NavigationKey == null || row.NavigationTitle == null))
            return "未找到唯一的 AutoClaw 会话映射，暂不能自动定位";
        if (row.AppId == "openclaw" && (row.NavigationKey == null || row.Source != "local-db"))
            return "未核验本地 OpenClaw 原会话键，暂不能恢复";
        if (row.AppId is not ("codex" or "workbuddy" or "workbuddy-ai" or "zcode" or "autoclaw" or "grok" or "qoder-cn" or "qoder" or "openclaw"))
            return "此应用尚未适配可靠的输入与发送定位";
        if (StagePolicy.IsBlockedOpenClawKey(row.NavigationKey)) return "定时或子任务不会自动恢复";
        return null;
    }

    public static string? Restriction(SessionRecord row, bool allowRateLimit = false, bool manual = false) {
        var identity = IdentityRestriction(row);
        if (identity != null) return identity;
        var reason = row.StatusReason ?? row.Evidence ?? "";
        if (reason.Contains("用户主动", StringComparison.Ordinal) || reason.Contains("用户取消", StringComparison.Ordinal))
            return "你已主动停止，本轮保持停止";
        if (!manual && (reason.Contains("中止事件", StringComparison.Ordinal) || reason.Contains("未记录触发者", StringComparison.Ordinal) || reason.Contains("触发者待确认", StringComparison.Ordinal)))
            return "无法排除主动停止，本轮只提醒";
        if (reason.Contains("认证", StringComparison.Ordinal) || reason.Contains("访问权限", StringComparison.Ordinal) || reason.Contains("上下文超过", StringComparison.Ordinal)
            || (!allowRateLimit && IsRateLimited(row)))
            return "需要先解决配额、认证或上下文限制；重复发送不能恢复。" + reason;
        return InputRestriction(row);
    }

    public static bool IsRateLimited(SessionRecord row) {
        var reason = row.StatusReason ?? row.Evidence ?? "";
        var limited = reason.Contains("429", StringComparison.Ordinal) || reason.Contains("配额", StringComparison.Ordinal) || reason.Contains("频率", StringComparison.Ordinal);
        var blocked = reason.Contains("认证", StringComparison.Ordinal) || reason.Contains("访问权限", StringComparison.Ordinal) || reason.Contains("上下文超过", StringComparison.Ordinal);
        return limited && !blocked;
    }

    public static string? FollowupRestriction(SessionRecord row) => IdentityRestriction(row) ?? InputRestriction(row);

    public static string Resume(string appId, RadarSettings settings) {
        if (settings.AppResumeTexts.TryGetValue(appId, out var custom) && custom.Trim().Length > 0) return custom.Trim();
        if (settings.Mode!="rage" && settings.InterruptText.Trim().Length > 0) return settings.InterruptText.Trim();
        if (settings.ResumeText.Trim().Length > 0) return settings.ResumeText.Trim();
        return appId == "grok" ? GrokResumeText : ResumeText;
    }

    public static string Continue(string appId, RadarSettings settings) {
        if (settings.AppContinueTexts.TryGetValue(appId, out var custom) && custom.Trim().Length > 0) return custom.Trim();
        return appId == "grok" ? GrokContinueText : settings.ContinueText;
    }

    bool Claim(SessionRecord row, string kind) {
        if (row.StartedAt is not double start) return false;
        _announced.TryGetValue(row.Id, out var kinds);
        if (start <= (kinds != null && kinds.TryGetValue(kind, out var seen) ? seen : 0)) return false;
        kinds ??= [];
        kinds[kind] = start;
        _announced[row.Id] = kinds;
        return true;
    }

    public void ObserveConfirmedRound(SessionRecord row, double now) {
        if (IdentityRestriction(row) == null && row.StartedAt is double start) _running[row.Id] = (start, now);
    }

    public List<ContinuationWork> StartCompleted(IReadOnlyList<SessionRecord> rows, double now, IReadOnlySet<string> supervised) {
        var events = new List<ContinuationWork>();
        foreach (var row in rows) {
            if (!supervised.Contains(row.Id) || row.ManualOnly == true || row.Status != "completed" || FollowupRestriction(row) != null || !Claim(row, "explicit-start")) continue;
            events.Add(new ContinuationWork {
                Session = SessionFacts.Clone(row), Key = SessionFacts.RoundKey(row), DetectedAt = now, CanContinue = true,
                Explanation = "已排定本轮持续优化", Kind = "followup", Text = "", AutomationMode = "rage",
            });
        }
        return events;
    }

    public List<ContinuationWork> Observe(IReadOnlyList<SessionRecord> rows, double now, RadarSettings settings, IReadOnlySet<string> supervised) {
        var events = new List<ContinuationWork>();
        foreach (var id in _running.Keys.Where(id => now - _running[id].Seen >= 120).ToList()) _running.Remove(id);
        var rage = settings.Mode == "rage";
        foreach (var row in rows) {
            if (row.Status == "running" && IdentityRestriction(row) == null && row.StartedAt is double runningStart) {
                _running[row.Id] = (runningStart, now);
                continue;
            }
            if (!SessionFacts.AutomationReadable(row)) {
                if (row.Status != "unknown" || row.UserStopped == true || row.StartedAt != (_running.TryGetValue(row.Id, out var held) ? held.Start : (double?)null))
                    _running.Remove(row.Id);
                continue;
            }
            if (!_running.TryGetValue(row.Id, out var old) || row.StartedAt != old.Start) continue;
            if (row.Status == "completed") {
                _running.Remove(row.Id);
                if (IdentityRestriction(row) != null || !Claim(row, "completed")) continue;
                var restriction = FollowupRestriction(row);
                var marked = supervised.Contains(row.Id);
                var care = false;
                events.Add(new ContinuationWork {
                    Session = SessionFacts.Clone(row), Key = SessionFacts.RoundKey(row), DetectedAt = now,
                    CanContinue = rage && marked && row.ManualOnly != true && restriction == null,
                    Explanation = rage ? (marked ? restriction ?? "本轮已结束，继续持续优化" : "本轮已完成；未标记监督，仅提醒") : "这个项目已完结，请验收",
                    Kind = rage && marked ? "followup" : "completion", Text = "", AutomationMode = rage ? "rage" : "collaboration", Care = care,
                });
                continue;
            }
            if (row.Status is not ("interrupted" or "stalled")) {
                _running.Remove(row.Id);
                continue;
            }
            if (row.Status == "stalled") _running[row.Id] = (old.Start, now);
            if (!Claim(row, row.Status)) continue;
            if (row.UserStopped == true) {
                _running.Remove(row.Id);
                continue;
            }
            var rate = row.Status == "interrupted" && settings.RateLimitWake && IsRateLimited(row);
            var block = row.Status == "stalled" ? "尚无明确中断证据，只提醒，不自动发送" : Restriction(row, rate);
            var times = _attempts.TryGetValue(row.Id, out var history) ? history.Where(stamp => now - stamp < 3600).ToList() : [];
            var cooldown = !rage && (times.Count >= 2 || (times.Count > 0 && now - times[^1] < 300));
            var delay = rate ? Math.Min(10, Math.Max(1, settings.RateLimitSeconds)) : rage ? 1 : 5;
            events.Add(new ContinuationWork {
                Session = SessionFacts.Clone(row), Key = SessionFacts.RoundKey(row), DetectedAt = now,
                CanContinue = row.ManualOnly != true && block == null && !cooldown && (!rage || supervised.Contains(row.Id)),
                Explanation = row.ManualOnly == true ? "这是后台子任务；仅支持手动恢复该条原会话"
                    : rage && !supervised.Contains(row.Id) ? "会话未标记监督，本轮只提醒"
                    : block ?? (cooldown ? "本会话已触发恢复冷却，先检查连续失败原因" : "检测到本轮意外中断，即将返回原会话继续"),
                Text = rage ? Resume(row.AppId, settings) : rate ? settings.RateLimitText : settings.InterruptText,
                Delay = delay, Patience = Math.Max(600, delay + 600), BypassRestriction = rate,
                AutomationMode = rage ? "rage" : "collaboration",
            });
        }
        return events;
    }

    public void RecordAttempt(string sessionId, double now) {
        var times = _attempts.TryGetValue(sessionId, out var history) ? history.Where(stamp => now - stamp < 3600).ToList() : [];
        times.Add(now);
        _attempts[sessionId] = times;
    }
}

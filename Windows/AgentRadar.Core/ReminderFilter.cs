namespace AgentRadar.Core;

public static class ReminderFilter {
    public static bool Allows(RadarSettings settings, string appId) =>
        settings.AllReminderApps || settings.ReminderAppIds.Contains(appId);

    public static bool Allows(RadarSettings settings, NoticeRecord notice, IReadOnlyList<SessionRecord> sessions) {
        if (notice.RelatedAppIds is { Count: > 0 })
            return notice.RelatedAppIds.Any(id => Allows(settings, id));
        if (!string.IsNullOrEmpty(notice.AppId)) return Allows(settings, notice.AppId);
        if (string.IsNullOrEmpty(notice.SessionId)) return true;
        var matches = sessions.Where(row => row.Id == notice.SessionId).ToList();
        return matches.Count == 1 ? Allows(settings, matches[0].AppId) : settings.AllReminderApps;
    }
}

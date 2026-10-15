using System;
using System.Collections.Generic;
using AgentRadar.Core;

namespace AgentRadar.Windows.Ui;

internal sealed class NoticeQueue
{
    private readonly List<NoticeRecord> _pending = new();
    private readonly HashSet<string> _acknowledgedCompletions = new();

    public NoticeRecord? Current { get; private set; }
    public int PendingCount => _pending.Count;

    public bool Show(NoticeRecord notice)
    {
        if (IsCompletion(notice) && _acknowledgedCompletions.Contains(notice.Id)) return false;
        var session = SessionKey(notice);
        if (Current == null || SessionKey(Current) == session) Current = notice;
        else
        {
            var index = _pending.FindIndex(item => SessionKey(item) == session);
            if (index >= 0) _pending[index] = notice;
            else _pending.Add(notice);
        }
        return true;
    }

    public void Close()
    {
        Acknowledge(Current);
        Current = _pending.Count == 0 ? null : Take();
    }

    public void CloseAll()
    {
        Acknowledge(Current);
        foreach (var notice in _pending) Acknowledge(notice);
        Current = null;
        _pending.Clear();
    }

    public void RemoveWhere(Predicate<NoticeRecord> remove)
    {
        if (Current != null && remove(Current)) { Acknowledge(Current); Current = null; }
        foreach (var notice in _pending) if (remove(notice)) Acknowledge(notice);
        _pending.RemoveAll(remove);
        if (Current == null && _pending.Count > 0) Current = Take();
    }

    public static string SessionKey(NoticeRecord notice) =>
        string.IsNullOrEmpty(notice.SessionId) ? "notice:" + notice.Id : notice.SessionId;

    public static bool IsCompletion(NoticeRecord notice) =>
        notice.Kind.Contains("complete", StringComparison.OrdinalIgnoreCase)
        || notice.Kind.Contains("acceptance", StringComparison.OrdinalIgnoreCase);

    public static bool IsConflict(NoticeRecord notice) =>
        notice.Kind.Contains("conflict", StringComparison.OrdinalIgnoreCase);

    private void Acknowledge(NoticeRecord? notice)
    {
        if (notice != null && IsCompletion(notice)) _acknowledgedCompletions.Add(notice.Id);
    }

    private NoticeRecord Take()
    {
        var notice = _pending[0];
        _pending.RemoveAt(0);
        return notice;
    }
}

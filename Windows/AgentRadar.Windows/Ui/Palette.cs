using System.Windows.Media;

namespace AgentRadar.Windows.Ui;

internal static class Palette
{
    public static readonly Color Ink = Color.FromArgb(255, 255, 255, 255);
    public static readonly Color Muted = Color.FromArgb(184, 255, 255, 255);
    public static readonly Color Danger = Color.FromRgb(255, 97, 102);
    public static readonly Color Ok = Color.FromRgb(71, 240, 145);
    public static readonly Color Rage = Color.FromRgb(255, 92, 87);
    public static readonly Color Collaboration = Color.FromRgb(97, 173, 255);
    public static readonly Color Warn = Color.FromRgb(255, 149, 0);
    public static readonly Color Sheet = Color.FromArgb(240, 22, 22, 24);

    public static SolidColorBrush Brush(Color color) => new(color);

    public static Color Status(AgentRadar.Core.SessionRecord session)
    {
        if (session.Status == "completed") return Ok;
        if (session.Status == "interrupted" && session.UserStopped != true) return Danger;
        return Ink;
    }
}

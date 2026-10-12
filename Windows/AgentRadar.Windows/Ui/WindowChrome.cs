using System;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Interop;
using System.Windows.Media;
using FormsScreen = System.Windows.Forms.Screen;

namespace AgentRadar.Windows.Ui;

internal static class WindowChrome
{
    public static void Paint(System.Windows.Controls.Border shell, double transparency, string? frost)
    {
        var alpha = FrostAlpha(transparency, frost);
        shell.Background = new LinearGradientBrush(
            Color.FromArgb(alpha, 28, 28, 32),
            Color.FromArgb((byte)Math.Min(255, alpha + 16), 8, 8, 10),
            new Point(0.5, 0),
            new Point(0.5, 1));
        shell.BorderBrush = new SolidColorBrush(Color.FromArgb(0x6B, 255, 255, 255));
        shell.BorderThickness = new Thickness(0.7);
        shell.CornerRadius = new CornerRadius(PanelMetrics.Corner);
    }

    public static void Clip(System.Windows.Controls.Border shell)
    {
        if (shell.ActualWidth <= 0 || shell.ActualHeight <= 0) return;
        shell.Clip = new RectangleGeometry(new Rect(0, 0, shell.ActualWidth, shell.ActualHeight), PanelMetrics.Corner, PanelMetrics.Corner);
    }

    public static byte FrostAlpha(double transparency, string? frost)
    {
        var t = double.IsFinite(transparency) ? Math.Clamp(transparency, 0.70, 0.80) : 0.75;
        var share = frost switch
        {
            "关闭" => 0d,
            "轻薄" => 0.45,
            "浓郁" => 0.90,
            _ => 0.70
        };
        var alpha = 214 - (t - 0.70) * 160 + share * 18;
        return (byte)Math.Clamp(alpha, 168, 232);
    }

    public static void TryAcrylic(Window window)
    {
        try
        {
            var hwnd = new WindowInteropHelper(window).Handle;
            if (hwnd == IntPtr.Zero) return;
            var backdrop = 3;
            _ = DwmSetWindowAttribute(hwnd, 38, ref backdrop, sizeof(int));
            var dark = 1;
            _ = DwmSetWindowAttribute(hwnd, 20, ref dark, sizeof(int));
            var corner = 2;
            _ = DwmSetWindowAttribute(hwnd, 33, ref corner, sizeof(int));
        }
        catch (DllNotFoundException) { }
        catch (EntryPointNotFoundException) { }
    }

    [DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int size);
}

internal static class WorkAreas
{
    public static Rect Containing(double x, double y, Visual? visual = null)
    {
        Rect? best = null;
        double bestDistance = double.MaxValue;
        foreach (var screen in FormsScreen.AllScreens)
        {
            var area = ToDip(screen.WorkingArea, visual);
            var contains = x >= area.Left && x <= area.Right && y >= area.Top && y <= area.Bottom;
            if (contains) return area;
            var dx = Math.Max(area.Left - x, Math.Max(0, x - area.Right));
            var dy = Math.Max(area.Top - y, Math.Max(0, y - area.Bottom));
            var distance = dx * dx + dy * dy;
            if (distance < bestDistance)
            {
                bestDistance = distance;
                best = area;
            }
        }
        return best ?? SystemParameters.WorkArea;
    }

    private static Rect ToDip(System.Drawing.Rectangle pixels, Visual? visual)
    {
        if (pixels.Width <= 0 || pixels.Height <= 0) return SystemParameters.WorkArea;
        var source = visual == null ? null : PresentationSource.FromVisual(visual);
        if (source?.CompositionTarget != null)
        {
            var matrix = source.CompositionTarget.TransformFromDevice;
            var topLeft = matrix.Transform(new Point(pixels.Left, pixels.Top));
            var bottomRight = matrix.Transform(new Point(pixels.Right, pixels.Bottom));
            return new Rect(topLeft, bottomRight);
        }
        var screen = FormsScreen.PrimaryScreen;
        var scale = screen == null || screen.Bounds.Width <= 0 ? 1d : SystemParameters.PrimaryScreenWidth / screen.Bounds.Width;
        if (!double.IsFinite(scale) || scale <= 0) scale = 1;
        return new Rect(pixels.Left * scale, pixels.Top * scale, pixels.Width * scale, pixels.Height * scale);
    }
}

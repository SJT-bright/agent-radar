using System;
using System.Drawing;
using System.Runtime.InteropServices;
using Forms = System.Windows.Forms;

namespace AgentRadar.Windows;

internal sealed class TrayIcon : IDisposable
{
    private readonly Forms.NotifyIcon _icon;
    private readonly Bitmap _bitmap;
    private readonly IntPtr _handle;
    private bool _disposed;

    public TrayIcon(Action show, Action exit)
    {
        _bitmap = new Bitmap(32, 32);
        using (var graphics = Graphics.FromImage(_bitmap))
        {
            graphics.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            graphics.Clear(Color.Transparent);
            using var fill = new SolidBrush(Color.FromArgb(230, 28, 28, 32));
            graphics.FillEllipse(fill, 1, 1, 30, 30);
            using var pen = new Pen(Color.White, 2);
            graphics.DrawEllipse(pen, 8, 8, 16, 16);
            graphics.DrawLine(pen, 20, 20, 27, 27);
        }
        _handle = _bitmap.GetHicon();
        var menu = new Forms.ContextMenuStrip();
        var showItem = new Forms.ToolStripMenuItem("显示悬浮窗");
        showItem.Click += (_, _) => show();
        var exitItem = new Forms.ToolStripMenuItem("退出任务雷达");
        exitItem.Click += (_, _) => exit();
        menu.Items.Add(showItem);
        menu.Items.Add(exitItem);
        _icon = new Forms.NotifyIcon
        {
            Icon = Icon.FromHandle(_handle),
            Text = "任务雷达",
            Visible = true,
            ContextMenuStrip = menu
        };
        _icon.MouseClick += (_, args) =>
        {
            if (args.Button == Forms.MouseButtons.Left) show();
        };
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        _icon.Visible = false;
        _icon.Dispose();
        DestroyIcon(_handle);
        _bitmap.Dispose();
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool DestroyIcon(IntPtr handle);
}

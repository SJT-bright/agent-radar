using System.Windows.Automation;
using System;
using System.Collections.Generic;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace AgentRadar.Windows.Ui;

internal static class TextLimit
{
    public static string OneLine(string? text)
    {
        if (string.IsNullOrWhiteSpace(text)) return "";
        var line = text.Replace('\r', ' ').Replace('\n', ' ').Trim();
        return line.Length > 180 ? line[..180] : line;
    }

    public static string PublicBlock(string? text)
    {
        if (string.IsNullOrWhiteSpace(text)) return "没有可显示的状态。";
        var kept = new List<string>();
        foreach (var raw in text.Replace("\r\n", "\n").Split('\n'))
        {
            var line = raw.TrimEnd();
            var trimmed = line.TrimStart();
            if (trimmed.StartsWith("at ", StringComparison.Ordinal)) continue;
            if (line.Contains(".cs:line", StringComparison.Ordinal) || line.Contains(".cs(", StringComparison.Ordinal)) continue;
            kept.Add(line);
        }
        var joined = string.Join("\n", kept).Trim();
        return joined.Length == 0 ? "没有可显示的状态。" : joined;
    }
}

internal enum CloseChoice { Save, Continue, Discard }

internal static class ProductDialog
{
    public static void Show(string title, string body)
    {
        var window = Create(title, 440, 240);
        var close = new Button { Content = "关闭", MinWidth = 72, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 12, 0, 0) };
        AutomationProperties.SetAutomationId(close, "dialog.close");
        close.Click += (_, _) => window.Close();
        var stack = new StackPanel();
        stack.Children.Add(Body(body));
        stack.Children.Add(close);
        window.Content = Pad(stack);
        window.ShowDialog();
    }

    public static CloseChoice AskSave(string details)
    {
        var window = Create("保存未保存的修改？", 480, 320);
        var result = CloseChoice.Continue;
        var text = "提醒、提示词与限流设置尚未保存。运行开关和监督选择已即时生效。";
        if (!string.IsNullOrWhiteSpace(details)) text += "\n\n" + details;
        var row = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 12, 0, 0) };
        row.Children.Add(Choice("保存", "dialog.save", () => { result = CloseChoice.Save; window.Close(); }));
        row.Children.Add(Choice("继续编辑", "dialog.continue", () => { result = CloseChoice.Continue; window.Close(); }));
        row.Children.Add(Choice("不保存", "dialog.discard", () => { result = CloseChoice.Discard; window.Close(); }));
        var stack = new StackPanel();
        stack.Children.Add(Body(text));
        stack.Children.Add(row);
        window.Content = Pad(stack);
        window.ShowDialog();
        return result;
    }

    public static bool TryAsk(string title, string initial, out string value)
    {
        var window = Create(title, 440, 220);
        var box = new TextBox { Text = initial ?? "", AcceptsReturn = true, TextWrapping = TextWrapping.Wrap, MinHeight = 72, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        AutomationProperties.SetAutomationId(box, "dialog.prompt");
        var saved = false;
        var result = initial ?? "";
        var row = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 12, 0, 0) };
        row.Children.Add(Choice("保存", "dialog.prompt.save", () => { result = box.Text; saved = true; window.Close(); }));
        row.Children.Add(Choice("取消", "dialog.prompt.cancel", () => window.Close()));
        var stack = new StackPanel();
        stack.Children.Add(box);
        stack.Children.Add(row);
        window.Content = Pad(stack);
        window.ShowDialog();
        value = result;
        return saved;
    }

    private static Window Create(string title, double width, double height) => new()
    {
        Title = title,
        Width = width,
        Height = height,
        MinWidth = 360,
        MinHeight = 180,
        WindowStartupLocation = WindowStartupLocation.CenterScreen,
        WindowStyle = WindowStyle.ToolWindow,
        ResizeMode = ResizeMode.CanResize,
        Topmost = true,
        Background = new SolidColorBrush(Palette.Sheet),
        Foreground = Brushes.White,
        FontFamily = new FontFamily("Segoe UI, Microsoft YaHei UI"),
        ShowInTaskbar = false
    };

    private static UIElement Pad(UIElement child) => new Border { Padding = new Thickness(16), Child = (UIElement)child };

    private static TextBox Body(string text) => new()
    {
        Text = text,
        IsReadOnly = true,
        BorderThickness = new Thickness(0),
        Background = Brushes.Transparent,
        Foreground = Brushes.White,
        TextWrapping = TextWrapping.Wrap,
        AcceptsReturn = true,
        MinHeight = 80,
        MaxHeight = 220,
        VerticalScrollBarVisibility = ScrollBarVisibility.Auto
    };

    private static Button Choice(string title, string id, Action click)
    {
        var button = new Button { Content = title, MinWidth = 76, Margin = new Thickness(8, 0, 0, 0) };
        AutomationProperties.SetAutomationId(button, id);
        button.Click += (_, _) => click();
        return button;
    }
}

internal static class StatusWindow
{
    public static void Show(string title, IReadOnlyList<(string Label, string Value)> rows)
    {
        var window = new Window
        {
            Title = title,
            Width = 460,
            Height = 420,
            MinWidth = 360,
            MinHeight = 240,
            WindowStartupLocation = WindowStartupLocation.CenterScreen,
            Background = new SolidColorBrush(Palette.Sheet),
            Foreground = Brushes.White,
            FontFamily = new FontFamily("Segoe UI, Microsoft YaHei UI"),
            Topmost = true
        };
        AutomationProperties.SetAutomationId(window, "status." + title);
        var stack = new StackPanel();
        foreach (var row in rows)
        {
            stack.Children.Add(new TextBlock { Text = row.Label, FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 8, 0, 2) });
            stack.Children.Add(new TextBox
            {
                Text = row.Value ?? "",
                IsReadOnly = true,
                TextWrapping = TextWrapping.Wrap,
                AcceptsReturn = true,
                Background = new SolidColorBrush(Color.FromArgb(18, 255, 255, 255)),
                BorderThickness = new Thickness(0)
            });
        }
        var close = new Button { Content = "关闭", MinWidth = 72, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 12, 0, 0) };
        close.Click += (_, _) => window.Close();
        stack.Children.Add(close);
        window.Content = new ScrollViewer { Content = new Border { Padding = new Thickness(16), Child = stack }, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        window.Show();
    }
}

internal sealed class AccessRepairWindow : Window
{
    private readonly Func<System.Threading.Tasks.Task<string>> _query;
    private readonly Action _openSettings;
    private readonly TextBox _result;
    private readonly Button _queryButton;
    private bool _busy;

    public AccessRepairWindow(Func<System.Threading.Tasks.Task<string>> query, Action openSettings)
    {
        _query = query;
        _openSettings = openSettings;
        Title = "辅助功能自助修复";
        Width = 460;
        Height = 420;
        MinWidth = 380;
        MinHeight = 280;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        Background = new SolidColorBrush(Palette.Sheet);
        Foreground = Brushes.White;
        FontFamily = new FontFamily("Segoe UI, Microsoft YaHei UI");
        Topmost = true;
        AutomationProperties.SetAutomationId(this, "access.repair");
        _result = new TextBox
        {
            Text = "还没有查询。",
            IsReadOnly = true,
            TextWrapping = TextWrapping.Wrap,
            AcceptsReturn = true,
            MinHeight = 120,
            VerticalScrollBarVisibility = ScrollBarVisibility.Auto
        };
        AutomationProperties.SetAutomationId(_result, "access.result");
        _queryButton = new Button { Content = "查询", MinWidth = 72, Margin = new Thickness(0, 0, 8, 0) };
        AutomationProperties.SetAutomationId(_queryButton, "access.query");
        _queryButton.Click += async (_, _) => await RunQuery();
        var settings = new Button { Content = "打开系统设置", MinWidth = 120, Margin = new Thickness(0, 0, 8, 0) };
        AutomationProperties.SetAutomationId(settings, "access.settings");
        settings.Click += (_, _) => _openSettings();
        var close = new Button { Content = "关闭", MinWidth = 72 };
        close.Click += (_, _) => Close();
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 12, 0, 0) };
        buttons.Children.Add(_queryButton);
        buttons.Children.Add(settings);
        buttons.Children.Add(close);
        var stack = new StackPanel();
        stack.Children.Add(new TextBlock { Text = "查询调用本机平台检查，并原样显示返回。这里不会重置系统权限，也不会改证书或信任。", TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 8) });
        stack.Children.Add(new TextBlock { Text = "Windows 没有与 macOS 相同的辅助功能开关。若结果不可用，打开轻松使用设置，确认当前正式程序后回到这里再查询。窗口读取和输入组件由同一次平台检查返回。", TextWrapping = TextWrapping.Wrap, Foreground = new SolidColorBrush(Palette.Muted), Margin = new Thickness(0, 0, 0, 8) });
        stack.Children.Add(_result);
        stack.Children.Add(buttons);
        Content = new Border { Padding = new Thickness(16), Child = stack };
    }

    public void ShowResult(string text) => _result.Text = text;

    private async System.Threading.Tasks.Task RunQuery()
    {
        if (_busy) return;
        _busy = true;
        _queryButton.Content = "查询中";
        _queryButton.IsEnabled = false;
        _result.Text = "正在查询…";
        try { _result.Text = await _query(); }
        catch (Exception ex) { _result.Text = "查询没有完成：" + TextLimit.OneLine(ex.Message); }
        finally
        {
            _busy = false;
            _queryButton.Content = "查询";
            _queryButton.IsEnabled = true;
        }
    }
}

using System.Windows.Automation;
using System;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Media.Animation;
using AgentRadar.Core;
using AgentRadar.Windows.Ui;

namespace AgentRadar.Windows;

public partial class ReminderWindow : Window
{
    private NoticeRecord? _notice;
    private int _pending;
    private double _idle = double.NaN;
    private bool _userMoved;
    private Point? _dragStart;
    private Point _dragOrigin;
    private Button? _recover;
    private Button? _open;
    private TextBox? _message;
    private TextBlock? _countdown;
    private TextBlock? _pendingText;
    private StackPanel? _pendingRow;
    private RotateTransform? _spin;
    private string _structure = "";

    public event Action? Acknowledged;
    public event Action? AcknowledgedAll;
    public event Action? OpenRequested;
    public event Action? RecoverRequested;

    public ReminderWindow()
    {
        InitializeComponent();
        SourceInitialized += (_, _) => WindowChrome.TryAcrylic(this);
        SizeChanged += (_, _) => WindowChrome.Clip(Shell);
        WindowChrome.Paint(Shell, 0.75, "浓郁");
        PreviewMouseMove += OnDragMove;
        PreviewMouseLeftButtonUp += (_, _) => EndDrag();
        MouseLeave += (_, _) => { if (Mouse.LeftButton != MouseButtonState.Pressed) EndDrag(); };
    }

    public NoticeRecord? Current => _notice;

    public void Render(NoticeRecord notice, int pending, bool popup, double idle, double anchorRight, double anchorTop, double anchorWidth, double anchorHeight)
    {
        _pending = pending;
        _idle = idle;
        var structure = notice.Id + "|" + notice.Kind + "|" + notice.Title + "|" + notice.Conversation + "|" + notice.Message + "|" + notice.CanRecover + "|" + (notice.RemainingSeconds != null);
        _notice = notice;
        if (structure != _structure)
        {
            _structure = structure;
            Rebuild();
        }
        else UpdateDynamic();
        if (!popup)
        {
            if (IsVisible) Hide();
            return;
        }
        if (!_userMoved) Place(anchorRight, anchorTop, anchorWidth, anchorHeight);
        if (!IsVisible) Show();
    }

    public void SetStatus(string text)
    {
        if (_message != null) _message.Text = text;
    }

    public void Reclamp()
    {
        _userMoved = false;
        if (_notice == null || !IsVisible) return;
        var area = WorkAreas.Containing(Left, Top, this);
        Left = Math.Max(area.Left, Math.Min(Left, area.Right - ActualWidth));
        Top = Math.Max(area.Top, Math.Min(Top, area.Bottom - ActualHeight));
        _userMoved = true;
    }

    private void Rebuild()
    {
        if (_notice == null) return;
        Root.Children.Clear();
        var conflict = NoticeQueue.IsConflict(_notice);
        var header = new Grid();
        if (conflict)
        {
            header.Children.Add(new TextBlock { Text = "!", Foreground = new SolidColorBrush(Palette.Warn), FontSize = 18, Width = 28, HorizontalAlignment = HorizontalAlignment.Left });
        }
        else
        {
            _spin = new RotateTransform();
            var icon = new TextBlock { Text = "↻", FontSize = 16, HorizontalAlignment = HorizontalAlignment.Center, VerticalAlignment = VerticalAlignment.Center, RenderTransform = _spin, RenderTransformOrigin = new Point(0.5, 0.5) };
            _recover = new Button { Content = icon, Width = 28, Height = 28, HorizontalAlignment = HorizontalAlignment.Left };
            AutomationProperties.SetAutomationId(_recover, "reminder.recover");
            AutomationProperties.SetName(_recover, "重试当前会话");
            _recover.Click += (_, _) => RecoverRequested?.Invoke();
            header.Children.Add(_recover);
        }
        var title = new TextBlock { Text = _notice.Title, FontWeight = FontWeights.SemiBold, FontSize = 11, Margin = new Thickness(34, 6, conflict ? 0 : 32, 0), TextTrimming = TextTrimming.CharacterEllipsis };
        HookDrag(title);
        header.Children.Add(title);
        if (!conflict)
        {
            _open = new Button { Content = "↗", Width = 26, Height = 26, HorizontalAlignment = HorizontalAlignment.Right, FontSize = 14 };
            AutomationProperties.SetAutomationId(_open, "reminder.open");
            AutomationProperties.SetName(_open, "打开原对话");
            _open.ToolTip = "打开这条提醒对应的原对话";
            _open.IsEnabled = !string.IsNullOrEmpty(_notice.SessionId);
            _open.Click += (_, _) => OpenRequested?.Invoke();
            header.Children.Add(_open);
        }
        Root.Children.Add(header);
        var primary = new TextBlock { Text = string.IsNullOrWhiteSpace(_notice.Conversation) ? _notice.Title : _notice.Conversation, FontSize = 13, FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 5, 0, 0), TextTrimming = TextTrimming.CharacterEllipsis };
        HookDrag(primary);
        Root.Children.Add(primary);
        _message = new TextBox
        {
            Text = _notice.Message,
            IsReadOnly = true,
            BorderThickness = new Thickness(0),
            Background = Brushes.Transparent,
            TextWrapping = TextWrapping.Wrap,
            MaxHeight = conflict ? 52 : 42,
            Margin = new Thickness(0, 4, 0, 0)
        };
        AutomationProperties.SetAutomationId(_message, "reminder.message");
        Root.Children.Add(_message);
        _countdown = new TextBlock { Foreground = new SolidColorBrush(Palette.Warn), FontWeight = FontWeights.SemiBold, FontSize = 12, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 4, 0, 0) };
        Root.Children.Add(_countdown);
        _pendingRow = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 6, 0, 0) };
        _pendingText = new TextBlock { FontSize = 10, Foreground = new SolidColorBrush(Palette.Muted), VerticalAlignment = VerticalAlignment.Center };
        var all = new Button { Content = "全部知道了", Margin = new Thickness(8, 0, 0, 0) };
        AutomationProperties.SetAutomationId(all, "reminder.ackAll");
        all.Click += (_, _) => AcknowledgedAll?.Invoke();
        _pendingRow.Children.Add(_pendingText);
        _pendingRow.Children.Add(all);
        Root.Children.Add(_pendingRow);
        var actions = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right, Margin = new Thickness(0, 6, 0, 0) };
        var ack = new Button { Content = "知道了", MinWidth = NoticeQueue.IsCompletion(_notice) && _pending == 0 ? 86 : 64, MinHeight = 28 };
        AutomationProperties.SetAutomationId(ack, "reminder.ack");
        ack.Click += (_, _) => Acknowledged?.Invoke();
        actions.Children.Add(ack);
        Root.Children.Add(actions);
        UpdateDynamic();
    }

    private void UpdateDynamic()
    {
        if (_notice == null) return;
        if (_message != null && !Equals(_message.Text, _notice.Message)) _message.Text = _notice.Message;
        if (_countdown != null)
        {
            _countdown.Text = Countdown(_notice, _idle);
            _countdown.Visibility = string.IsNullOrEmpty(_countdown.Text) ? Visibility.Collapsed : Visibility.Visible;
        }
        if (_pendingText != null) _pendingText.Text = _pending > 0 ? "另有 " + _pending + " 项待验收" : "";
        if (_pendingRow != null) _pendingRow.Visibility = _pending > 0 ? Visibility.Visible : Visibility.Collapsed;
        if (_recover != null)
        {
            _recover.IsEnabled = _notice.CanRecover && !_notice.Recovering;
            _recover.ToolTip = _notice.Recovering
                ? "正在继续当前会话"
                : (_notice.CanRecover ? "恢复此条会话，不更改自动发送开关" : _notice.Message);
            AutomationProperties.SetName(_recover, _notice.CanRecover ? "重试当前会话" : "当前不能恢复");
        }
        Spin(_notice.Recovering);
    }

    private static string Countdown(NoticeRecord notice, double idle)
    {
        var idleText = !double.IsFinite(idle)
            ? "键鼠需连续静止 2 秒"
            : "键鼠还需连续静止 " + Math.Max(0, (int)Math.Ceiling(Math.Max(0, 2 - idle))) + " 秒";
        if (notice.RemainingSeconds is int seconds)
            return (seconds > 0 ? seconds + " 秒后准备操作电脑" : "倒计时 0 秒") + " · " + idleText;
        if (notice.Recovering) return "正在操作原会话 · " + idleText;
        return "";
    }

    private void Spin(bool active)
    {
        if (_spin == null) return;
        if (!active || MotionPreference.Reduce)
        {
            _spin.BeginAnimation(RotateTransform.AngleProperty, null);
            _spin.Angle = 0;
            return;
        }
        if (_spin.HasAnimatedProperties) return;
        var animation = new DoubleAnimation(0, 360, TimeSpan.FromSeconds(1)) { RepeatBehavior = RepeatBehavior.Forever };
        _spin.BeginAnimation(RotateTransform.AngleProperty, animation);
    }

    private void Place(double anchorRight, double anchorTop, double anchorWidth, double anchorHeight)
    {
        UpdateLayout();
        var width = ActualWidth > 1 ? ActualWidth : Width;
        var height = ActualHeight > 1 ? ActualHeight : 180;
        var area = WorkAreas.Containing(anchorRight, anchorTop, this);
        var x = anchorRight - anchorWidth - width - 12;
        if (x < area.Left) x = anchorRight + 12;
        var y = anchorTop;
        Left = Math.Max(area.Left, Math.Min(x, area.Right - width));
        Top = Math.Max(area.Top, Math.Min(y, area.Bottom - height));
    }

    private void HookDrag(FrameworkElement element)
    {
        element.Cursor = Cursors.SizeAll;
        element.MouseLeftButtonDown += (_, args) =>
        {
            _dragStart = PointToScreen(args.GetPosition(this));
            _dragOrigin = new Point(Left, Top);
            CaptureMouse();
            args.Handled = true;
        };
    }

    private void OnDragMove(object sender, MouseEventArgs args)
    {
        if (_dragStart == null || Mouse.LeftButton != MouseButtonState.Pressed) return;
        var now = PointToScreen(args.GetPosition(this));
        Left = _dragOrigin.X + now.X - _dragStart.Value.X;
        Top = _dragOrigin.Y + now.Y - _dragStart.Value.Y;
    }

    private void EndDrag()
    {
        if (_dragStart == null) return;
        _dragStart = null;
        _userMoved = true;
        if (IsMouseCaptured) ReleaseMouseCapture();
    }
}

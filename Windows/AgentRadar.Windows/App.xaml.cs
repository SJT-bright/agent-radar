using System;
using System.IO;
using System.Threading;
using AgentRadar.Core;
using AgentRadar.Windows.Services;
using AgentRadar.Windows.Ui;

namespace AgentRadar.Windows;

public partial class App : System.Windows.Application
{
    private const string MutexName = "Local\\AgentRadar.SingleInstance";
    private const string EventName = "Local\\AgentRadar.Activate";

    private Mutex? _mutex;
    private bool _ownsMutex;
    private EventWaitHandle? _activate;
    private RegisteredWaitHandle? _registration;
    private RadarRuntime? _runtime;
    private IPlatformServices? _platform;
    private MainWindow? _window;
    private TrayIcon? _tray;
    private System.Windows.Threading.DispatcherTimer? _tick;
    private CancellationTokenSource? _lifetime;
    private int _disposed;
    private int _tickFailures;

    public App()
    {
        InitializeComponent();
        DispatcherUnhandledException += (_, args) =>
        {
            args.Handled = true;
            try { ProductDialog.Show("任务雷达", "界面发生错误，即将退出。\n" + TextLimit.OneLine(args.Exception.Message)); }
            catch (InvalidOperationException) { }
            Cleanup();
            Shutdown(1);
        };
        SessionEnding += (_, _) => Cleanup();
    }

    protected override void OnStartup(System.Windows.StartupEventArgs e)
    {
        base.OnStartup(e);
        if (Has(e.Args, "--native-bridge"))
        {
            var code = NativeBridgeHost.Run();
            Shutdown(code);
            return;
        }
        var smoke=Array.IndexOf(e.Args,"--smoke-test");
        if(smoke>=0&&smoke+1<e.Args.Length){Acceptance.SmokeHarness.Run(e.Args[smoke+1]);return;}
        try { StartProduct(); }
        catch (Exception ex)
        {
            try { ProductDialog.Show("任务雷达", "任务雷达没有启动：" + TextLimit.OneLine(ex.Message)); }
            catch (InvalidOperationException) { }
            Cleanup();
            Shutdown(1);
        }
    }

    protected override void OnExit(System.Windows.ExitEventArgs e)
    {
        Cleanup();
        base.OnExit(e);
    }

    private void StartProduct()
    {
        _mutex = new Mutex(false, MutexName);
        try
        {
            if (!_mutex.WaitOne(0))
            {
                SignalExisting();
                Shutdown(0);
                return;
            }
            _ownsMutex = true;
        }
        catch (AbandonedMutexException) { _ownsMutex = true; }

        _activate = new EventWaitHandle(false, EventResetMode.AutoReset, EventName);
        _registration = ThreadPool.RegisterWaitForSingleObject(_activate, (_, _) => Dispatcher.BeginInvoke(ShowExisting), null, Timeout.Infinite, false);

        var root = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
        if (string.IsNullOrWhiteSpace(root)) throw new InvalidOperationException("没有可用的应用数据目录。");
        var directory = Path.Combine(root, "AgentRadar");
        var native = Environment.ProcessPath ?? Path.Combine(AppContext.BaseDirectory, "AgentRadar.exe");
        Environment.SetEnvironmentVariable("AGENT_RADAR_NATIVE_HELPER", native);
        var repository = new SettingsRepository(directory);
        var worker = new WorkerClient(WorkerExecutable(), native);
        var platform = new WindowsPlatform();
        _platform = platform;
        var runtime = new RadarRuntime(repository, worker, platform);
        _runtime = runtime;
        _lifetime = new CancellationTokenSource();
        _window = new MainWindow(runtime);
        _window.AttachPlatform(platform);
        _window.ExitRequested += ExitProduct;
        _window.Show();
        ApplyPlatform(runtime.Settings.KeepAwake, runtime.Settings.StartWithWindows, report: true);
        _tray = new TrayIcon(ShowExisting, ExitProduct);
        var start = runtime.StartAsync(_lifetime.Token);
        _ = start.ContinueWith(task =>
        {
            if (task.IsFaulted)
            {
                var message = TextLimit.OneLine(task.Exception?.GetBaseException().Message);
                Dispatcher.BeginInvoke(() => _window?.Report("监控没有启动：" + message));
            }
        }, System.Threading.Tasks.TaskScheduler.Default);
        _tick = new System.Windows.Threading.DispatcherTimer { Interval = TimeSpan.FromMilliseconds(250) };
        _tick.Tick += (_, _) => OnTick();
        _tick.Start();
    }

    private void OnTick()
    {
        if (_runtime == null) return;
        try
        {
            _runtime.Tick();
            _tickFailures = 0;
        }
        catch (Exception ex)
        {
            _tickFailures++;
            if (_tickFailures == 1) _window?.Report("刷新没有完成：" + TextLimit.OneLine(ex.Message));
            if (_tickFailures >= 3)
            {
                _tick?.Stop();
                _window?.Report("连续刷新失败，已停止定时刷新。");
            }
        }
    }

    private void ApplyPlatform(bool keepAwake, bool startup, bool report)
    {
        if (_platform == null || _runtime == null) return;
        try { _platform.SetKeepAwake(keepAwake); }
        catch (Exception ex)
        {
            _runtime.UpdateSettings(settings => settings.KeepAwake = false);
            if (report) _window?.Report("夜间常亮没有生效：" + TextLimit.OneLine(ex.Message));
        }
        try { _platform.SetStartup(startup); }
        catch (Exception ex)
        {
            _runtime.UpdateSettings(settings => settings.StartWithWindows = false);
            if (report) _window?.Report("登录时启动没有生效：" + TextLimit.OneLine(ex.Message));
        }
    }

    private void ShowExisting()
    {
        if (_window == null) return;
        _window.Show();
        _window.SetExpanded(true);
        _window.Activate();
    }

    private void ExitProduct()
    {
        var settings = _window?.CurrentSettingsWindow;
        if (settings != null && !settings.ConfirmClose()) return;
        settings?.CloseWithoutPrompt();
        Cleanup();
        Shutdown(0);
    }

    private void SignalExisting()
    {
        try
        {
            using var handle = EventWaitHandle.OpenExisting(EventName);
            handle.Set();
        }
        catch (WaitHandleCannotBeOpenedException) { }
    }

    private void Cleanup()
    {
        if (Interlocked.Exchange(ref _disposed, 1) == 1) return;
        try { _tick?.Stop(); } catch (InvalidOperationException) { }
        try { _window?.PrepareForExit(); } catch (InvalidOperationException) { }
        try { _lifetime?.Cancel(); } catch (ObjectDisposedException) { }
        try { _runtime?.Dispose(); } catch (ObjectDisposedException) { }
        catch (InvalidOperationException) { }
        if (_platform is IDisposable disposable)
        {
            try { disposable.Dispose(); }
            catch (ObjectDisposedException) { }
        }
        try { _tray?.Dispose(); } catch (InvalidOperationException) { }
        try { _registration?.Unregister(null); } catch (InvalidOperationException) { }
        try { _activate?.Dispose(); } catch (ObjectDisposedException) { }
        if (_ownsMutex)
        {
            try { _mutex?.ReleaseMutex(); }
            catch (ApplicationException) { }
        }
        try { _mutex?.Dispose(); } catch (ObjectDisposedException) { }
        try { _lifetime?.Dispose(); } catch (ObjectDisposedException) { }
    }

    private static string WorkerExecutable()
    {
        var directory = AppContext.BaseDirectory;
        foreach (var name in new[] { "Resources/radar-worker/radar-worker.exe", "AgentRadar.Worker.exe", "agent-radar-worker.exe" })
        {
            var path = Path.Combine(directory, name);
            if (File.Exists(path)) return path;
        }
        return Path.Combine(directory, "AgentRadar.Worker.exe");
    }

    private static bool Has(string[] args, string flag)
    {
        foreach (var arg in args)
        {
            if (string.Equals(arg, flag, StringComparison.Ordinal)) return true;
        }
        return false;
    }
}

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Imaging;
using AgentRadar.Core;
namespace AgentRadar.Windows.Acceptance;
public static class SmokeHarness {
 public static async void Run(string output) {
  var checks=new List<object>();
  RadarRuntime? runtime=null;
  void Check(bool pass,string name,object? evidence=null){checks.Add(new{name,pass,evidence});if(!pass)throw new InvalidOperationException(name);}
  try {
   Directory.CreateDirectory(output);
   var repository=new SettingsRepository(Path.Combine(output,"isolated-data"));
   var worker=new FixtureWorker();
   runtime=new RadarRuntime(repository,worker,new FixturePlatform());
   var main=new MainWindow(runtime); Application.Current.MainWindow=main;main.Show();
   await runtime.StartAsync();await Task.Delay(700);
   main.SetExpanded(true);main.UpdateLayout();await Task.Delay(400);
   Check(Math.Abs(main.Width-238)<1,"expanded panel width",main.Width);
   Capture(main,Path.Combine(output,"01-expanded.png"));
   main.SetExpanded(false);await Task.Delay(300);main.UpdateLayout();
   Check(Math.Abs(main.Width-60)<1 && Math.Abs(main.Height-32)<1,"compact capsule size",new{main.Width,main.Height});
   Capture(main,Path.Combine(output,"02-compact.png"));
   main.SetExpanded(true); main.OpenSettings();await Task.Delay(500);
   var settings=main.CurrentSettingsWindow ?? throw new InvalidOperationException("settings missing");
   Check(settings.IsVisible,"settings actually shown");
   Capture(settings,Path.Combine(output,"03-settings.png"));
   var handle=new WindowInteropHelper(settings).Handle;
   await Task.Run(()=>InvokeName(handle,"提示词"));await Task.Delay(300);
   Check(await Task.Run(()=>FindName(handle,"保存")!=null),"save control accessible");
   Capture(settings,Path.Combine(output,"04-prompts.png"));
   await Task.Run(()=>SetText(handle,"意外中断提示词",new string('中',2001)));await Task.Delay(300);
   Check(await Task.Run(()=>ReadText(handle,"意外中断提示词").Length==2001),"oversize draft fully retained");
   Check(await Task.Run(()=>FindName(handle,"保存")?.Current.IsEnabled==false),"oversize draft cannot save");
   Capture(settings,Path.Combine(output,"05-over-limit.png"));
   await Task.Run(()=>SetText(handle,"意外中断提示词","CI isolated recovery text"));await Task.Delay(200);
   await Task.Run(()=>InvokeName(handle,"保存"));await Task.Delay(300);
   Check(repository.Load().InterruptText=="CI isolated recovery text","actual save reaches isolated file");
   await Task.Run(()=>InvokeName(handle,"完成"));await Task.Delay(300);
   main.OpenSettings();await Task.Delay(300);
   var reopened=main.CurrentSettingsWindow ?? throw new InvalidOperationException("reopen missing");
   var reopenedHandle=new WindowInteropHelper(reopened).Handle;
   await Task.Run(()=>InvokeName(reopenedHandle,"提示词"));await Task.Delay(200);
   Check(await Task.Run(()=>ReadText(reopenedHandle,"意外中断提示词"))=="CI isolated recovery text","saved text survives close/reopen");
   await Task.Run(()=>InvokeName(reopenedHandle,"监督"));await Task.Delay(200);
   Check(await Task.Run(()=>FindName(reopenedHandle,"搜索会话、软件或工作区")!=null),"supervision search accessible");
   Capture(reopened,Path.Combine(output,"06-supervision.png"));
   await Task.Run(()=>InvokeName(reopenedHandle,"完成"));await Task.Delay(200);
   var notice=new NoticeRecord("fixture-notice",worker.Rows[0].Id,"Codex · 完成待验收","Fixture conversation","这个项目已完结，请验收","completion",false);
   main.ShowNotice(notice);await Task.Delay(400);
   var reminder=Application.Current.Windows.Cast<Window>().FirstOrDefault(w=>w!=main && w.IsVisible);
   Check(reminder!=null,"product reminder is visible");
   if(reminder!=null)Capture(reminder,Path.Combine(output,"07-reminder.png"));
   Check(worker.RealSends==0,"acceptance performs no external sends");
   var result=new{all_passed=true,checks,external_sends=0,data_root=repository.DataDirectory,renderer="actual WPF product windows; captured with RenderTargetBitmap"};
   File.WriteAllText(Path.Combine(output,"result.json"),JsonSerializer.Serialize(result,new JsonSerializerOptions{WriteIndented=true}));
   runtime.Dispose();Application.Current.Shutdown(0);
  } catch(Exception ex) {
   Directory.CreateDirectory(output);File.WriteAllText(Path.Combine(output,"result.json"),JsonSerializer.Serialize(new{all_passed=false,checks,error=ex.GetType().Name+": "+ex.Message,external_sends=0},new JsonSerializerOptions{WriteIndented=true}));
   runtime?.Dispose();Application.Current.Shutdown(1);
  }
 }
 static AutomationElement? FindName(IntPtr hwnd,string name){var root=AutomationElement.FromHandle(hwnd);return root.FindFirst(TreeScope.Descendants,new PropertyCondition(AutomationElement.NameProperty,name));}
 static void InvokeName(IntPtr hwnd,string name){var e=FindName(hwnd,name)??throw new InvalidOperationException("control not found: "+name);if(e.TryGetCurrentPattern(InvokePattern.Pattern,out var p)){((InvokePattern)p).Invoke();return;}if(e.TryGetCurrentPattern(SelectionItemPattern.Pattern,out var s)){((SelectionItemPattern)s).Select();return;}throw new InvalidOperationException("no invoke pattern: "+name);}
 static void SetText(IntPtr hwnd,string name,string value){var e=FindName(hwnd,name)??throw new InvalidOperationException("input missing: "+name);((ValuePattern)e.GetCurrentPattern(ValuePattern.Pattern)).SetValue(value);}
 static string ReadText(IntPtr hwnd,string name){var e=FindName(hwnd,name)??throw new InvalidOperationException("input missing: "+name);return ((ValuePattern)e.GetCurrentPattern(ValuePattern.Pattern)).Current.Value;}
 static void Capture(Window window,string filename){window.UpdateLayout();var w=(int)Math.Ceiling(window.ActualWidth);var h=(int)Math.Ceiling(window.ActualHeight);if(w<=0||h<=0)throw new InvalidOperationException("window not rendered");var bitmap=new RenderTargetBitmap(w,h,96,96,PixelFormats.Pbgra32);bitmap.Render(window);var png=new PngBitmapEncoder();png.Frames.Add(BitmapFrame.Create(bitmap));using var stream=File.Create(filename);png.Save(stream);}
 sealed class FixturePlatform:IPlatformServices {
  public double IdleSeconds=>1000;public bool DesktopUsable=>true;
  public Task<NavigationResult> OpenAsync(SessionRecord s,CancellationToken token=default)=>Task.FromResult(new NavigationResult(false,"Isolated preview does not navigate external conversations"));
  public Task<string> CheckAccessibilityAsync(CancellationToken token=default)=>Task.FromResult("Isolated preview: native controls only");
  public void SetKeepAwake(bool enabled){}public void SetStartup(bool enabled){}
 }
 sealed class FixtureWorker:IWorkerClient {
  public event Action<CollectorSnapshot>? Snapshot;public event Action<string>? Fault;
  public int RealSends{get;private set;}=0;
  public List<SessionRecord> Rows{get;}=[];
  public Task StartAsync(CancellationToken token=default){var now=DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()/1000d;Rows.Clear();Rows.Add(new SessionRecord{Id="codex:11111111-1111-4111-8111-111111111111",AppId="codex",AppName="Codex",Title="Fixture conversation",Project="C:\\Fixture\\VideoOS",Status="running",Evidence="isolated lifecycle fixture",Source="local-session",Target="fixture://disabled",StartedAt=now-20,UpdatedAt=now,TimingBasis="turn"});Rows.Add(new SessionRecord{Id="openclaw:main:22222222-2222-4222-8222-222222222222",AppId="openclaw",AppName="OpenClaw",Title="Fixture OpenClaw",Project="C:\\Fixture\\Research",Status="unknown",Source="local-db",NavigationKey="agent:main:dashboard:fixture",UpdatedAt=now,ManualOnly=true});Snapshot?.Invoke(new CollectorSnapshot{Sessions=Rows,CollectedAt=now});return Task.CompletedTask;}
  public void Stop(){}public void Dispose(){}
  public Task<BridgeResult> RunBridgeAsync(Dictionary<string,object?> request,CancellationToken token=default)=>Task.FromResult(new BridgeResult{Code="preview_sending_disabled",Attempted=false,DiagnosticSummary="isolated preview; no native helper or gateway invoked"});
 }
}

using System;
using System.Diagnostics;
using System.Linq;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Automation;
using AgentRadar.Core;
using Microsoft.Win32;
namespace AgentRadar.Windows.Services;
public sealed class WindowsPlatform:IPlatformServices,IDisposable {
 [StructLayout(LayoutKind.Sequential)]struct LastInput{public uint Size;public uint Tick;}
 [DllImport("user32.dll")]static extern bool GetLastInputInfo(ref LastInput info);
 [DllImport("user32.dll",SetLastError=true)]static extern IntPtr OpenInputDesktop(uint flags,bool inherit,uint access);
 [DllImport("user32.dll")]static extern bool CloseDesktop(IntPtr handle);
 [DllImport("kernel32.dll")]static extern uint SetThreadExecutionState(uint flags);
 public static uint InputTick {get{var info=new LastInput{Size=(uint)Marshal.SizeOf<LastInput>()};if(!GetLastInputInfo(ref info))throw new InvalidOperationException("input clock unavailable");return info.Tick;}}
 public double IdleSeconds{get{try{return unchecked((uint)Environment.TickCount-InputTick)/1000d;}catch{return 0;}}}
 public bool DesktopUsable{get{var desktop=OpenInputDesktop(0,false,1);if(desktop==IntPtr.Zero)return false;CloseDesktop(desktop);return true;}}
 public async Task<NavigationResult> OpenAsync(SessionRecord row,CancellationToken token=default){var link=StagePolicy.SafeLink(row.Target,row.AppId);if(link==null)return new(false,"没有可核验的原会话跳转入口；可使用手动恢复或在应用中打开");try{Process.Start(new ProcessStartInfo(link){UseShellExecute=true});await Task.Delay(800,token);if(row.AppId=="zcode")return new(false,"已打开工作区，请选择原会话");var probe=await Task.Run(()=>NativeAutomation.Run(System.Text.Json.JsonSerializer.Serialize(new{stage="snapshot",id=row.Id,app_id=row.AppId,title=row.Title,project=row.Project,target=row.Target,pid=row.Pid,source=row.Source})),token);using var doc=System.Text.Json.JsonDocument.Parse(probe);return doc.RootElement.TryGetProperty("identity",out var identity)&&identity.GetBoolean()?new(true,"已核验原会话"):new(false,"已请求跳转，但当前会话身份仍待确认");}catch{return new(false,"未确认原会话已经打开，提醒已保留");}}
 public Task<string> CheckAccessibilityAsync(CancellationToken token=default)=>Task.Run(()=>{try{var windows=AutomationElement.RootElement.FindAll(TreeScope.Children,Condition.TrueCondition);var apps=Process.GetProcesses().Count(p=>{try{return AppCatalog.TryMatchProcess(p.ProcessName,out _,out _)&&p.MainWindowHandle!=IntPtr.Zero;}catch{return false;}});return $"Windows 可访问性组件可读取桌面；发现 {apps} 个 AI 应用窗口。\n同一权限级的具体会话仍须只读核验；不会申请管理员权限或修改系统保护。\n桌面窗口数：{windows.Count}";}catch{return "窗口暂不可读，请确认桌面已解锁及目标应用以相同权限级运行。";}},token);
 public void SetKeepAwake(bool enabled){if(SetThreadExecutionState(enabled?0x80000003u:0x80000000u)==0)throw new InvalidOperationException("keep awake unavailable");}
 public void SetStartup(bool enabled){using var key=Registry.CurrentUser.CreateSubKey(@"Software\Microsoft\Windows\CurrentVersion\Run");if(enabled)key.SetValue("AgentRadar","\""+Environment.ProcessPath+"\"");else{var old=key.GetValue("AgentRadar") as string;if(old?.Contains("AgentRadar.exe",StringComparison.OrdinalIgnoreCase)==true)key.DeleteValue("AgentRadar",false);}}
 public void Dispose(){SetThreadExecutionState(0x80000000u);}
}

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Linq;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Windows.Automation;
using AgentRadar.Core;
namespace AgentRadar.Windows.Services;

/// Fixed metadata only; all source identity checks run again in the Python guardian.
public static class NativeAutomation {
 static readonly string[] Hints=["与 Codex 协作","随心输入","询问 ChatGPT","Work with Codex","消息输入框","发送任务消息","提出后续修改要求","Ask for follow-up changes","发送给 AutoClaw","填写你的目标","Message","聊天输入"];
 static readonly string[] Sends=["发送","发送消息","发送提示","提交","Send","Send message","Send prompt","Submit"];
 static readonly string[] Stops=["停止","停止生成","停止回复","Stop","Stop generating","Stop response"];
 [DllImport("user32.dll")]static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")]static extern bool SetForegroundWindow(IntPtr hwnd);
 [DllImport("user32.dll")]static extern uint SendInput(uint n,INPUT[] inputs,int size);
 [StructLayout(LayoutKind.Sequential)]struct INPUT{public uint type;public INPUTDATA data;}
 [StructLayout(LayoutKind.Explicit)]struct INPUTDATA{[FieldOffset(0)]public KEYBDINPUT keyboard;[FieldOffset(0)]public MOUSEINPUT mouse;}
 [StructLayout(LayoutKind.Sequential)]struct KEYBDINPUT{public ushort vk;public ushort scan;public uint flags;public uint time;public UIntPtr extra;}
 [StructLayout(LayoutKind.Sequential)]struct MOUSEINPUT{public int x,y;public uint data,flags,time;public UIntPtr extra;}
 static void Paste(){var sequence=new[]{Key(0x11,false),Key(0x56,false),Key(0x56,true),Key(0x11,true)};if(SendInput((uint)sequence.Length,sequence,Marshal.SizeOf<INPUT>())!=sequence.Length)throw new InvalidOperationException("input_failed");}
 static INPUT Key(ushort key,bool up)=>new(){type=1,data=new INPUTDATA{keyboard=new KEYBDINPUT{vk=key,flags=up?2u:0u,extra=new UIntPtr(0x41524144)}}};
 public static string Run(string json){bool wrote=false;try{using var document=JsonDocument.Parse(json);var q=document.RootElement;string S(string name)=>q.TryGetProperty(name,out var p)&&p.ValueKind==JsonValueKind.String?p.GetString()??"":"";bool B(string name)=>q.TryGetProperty(name,out var p)&&p.ValueKind==JsonValueKind.True;var stage=S("stage");if(stage=="discover")return Discover();if(stage=="health"){var root=AutomationElement.RootElement;return Result(new{code=root!=null?"ready":"permission_required",attempted=false});}
 var id=S("id");var app=S("app_id");var wanted=S("title");var project=S("project");var nav=S("navigation_key");var target=S("target");int? pid=q.TryGetProperty("pid",out var pidValue)&&pidValue.TryGetInt32(out var n)&&n>0?n:null;var mutating=stage is "route" or "write" or "send";
 if(mutating&&!StagePolicy.MutationAuthorized(true,Environment.GetEnvironmentVariable(StagePolicy.SupervisorEnvironment),B("supervisor_verified"),id,app,wanted,project,nav,target,pid))return Result(new{code="target_unverified",attempted=false});
 using var platform=new WindowsPlatform();if(mutating&&(!platform.DesktopUsable||platform.IdleSeconds<2))return Result(new{code="user_active",attempted=false});
 if(stage=="route"){var link=StagePolicy.SafeLink(target,app);if(link!=null){Process.Start(new ProcessStartInfo(link){UseShellExecute=true});Thread.Sleep(700);} }
 var candidates=Process.GetProcesses().Where(p=>{try{return AppCatalog.TryMatchProcess(p.ProcessName,out var matched,out _)&&matched==app&&p.MainWindowHandle!=IntPtr.Zero&&(!pid.HasValue||p.Id==pid.Value);}catch{return false;}}).ToList();
 if(candidates.Count!=1)return Result(new{code=candidates.Count>1?"window_ambiguous":"app_unavailable",identity=false,attempted=false});var process=candidates[0];var hwnd=process.MainWindowHandle;var window=AutomationElement.FromHandle(hwnd);var elements=Walk(window);var tail=id.Split(':').LastOrDefault()??"";var keys=new[]{tail,nav.Split(':').LastOrDefault()??""}.Where(x=>x.Length>=16).ToArray();
 bool HasKey(AutomationElement e){try{return StagePolicy.MetadataCarriesThread(e==window?e.Current.Name:"",e.Current.AutomationId,e.Current.HelpText,keys,e==window);}catch{return false;}}
 bool CurrentCarrier(AutomationElement e){try{if(e==window||e.Current.ControlType==ControlType.Document)return true;if(e.TryGetCurrentPattern(SelectionItemPattern.Pattern,out var p)){var rect=e.Current.BoundingRectangle;var area=window.Current.BoundingRectangle;return ((SelectionItemPattern)p).Current.IsSelected && rect.Left<area.Left+200 && (e.Current.ControlType==ControlType.ListItem||e.Current.ControlType==ControlType.TreeItem);}return false;}catch{return false;}}
 var identity=elements.Any(e=>CurrentCarrier(e)&&HasKey(e));
 // A global unique local title may identify only an explicit current header,
 // not an arbitrary historical message or an unselected sidebar row.
 if(!identity&&B("title_unique")){identity=elements.Any(e=>IsHeader(e,window,wanted,project));}
 var editors=elements.Where(e=>IsComposer(e,window)).ToList();var sends=elements.Where(e=>IsAction(e,Sends)).ToList();var busy=elements.Any(e=>IsAction(e,Stops));var input=editors.Count==1?editors[0]:null;var value=input==null?null:Read(input);var focused=GetForegroundWindow()==hwnd;
 var snapshot=new{code=!identity?"target_unverified":busy?"already_running":editors.Count!=1?"composer_unreadable":"ready",identity,stable_id=identity?id:null,title_only=false,ambiguous=false,input_count=editors.Count,busy,send_available=sends.Count==1,send_enabled=sends.Count==1&&sends[0].Current.IsEnabled,send_deferred=value==""&&sends.Count==0,empty=value=="",digest=value==null?null:Digest(value),focused,attempted=false};
 if(stage=="route"&&identity&&!focused){SetForegroundWindow(hwnd);Thread.Sleep(150);return Result(new{code="ready",identity,stable_id=id,title_only=false,ambiguous=false,input_count=editors.Count,busy,send_available=sends.Count==1,send_enabled=sends.Count==1&&sends[0].Current.IsEnabled,send_deferred=value==""&&sends.Count==0,empty=value=="",digest=value==null?null:Digest(value),focused=GetForegroundWindow()==hwnd,attempted=false});}
 if(stage=="snapshot"||stage=="route")return Result(snapshot);
 if(!identity||busy||input==null)return Result(snapshot);
 if(!focused){SetForegroundWindow(hwnd);Thread.Sleep(120);if(GetForegroundWindow()!=hwnd)return Result(new{code="focus_changed",attempted=false});}
 if(stage=="write"){var text=S("text");if(StagePolicy.ValidateSendText(text)!=null||string.IsNullOrWhiteSpace(text))return Result(new{code="text_invalid",attempted=false});if(value==null||value!=""&&!B("overwrite"))return Result(new{code=value==null?"composer_unreadable":"draft_present",attempted=false});input.SetFocus();wrote=true;
 if(input.TryGetCurrentPattern(ValuePattern.Pattern,out var pattern)&&!((ValuePattern)pattern).Current.IsReadOnly)((ValuePattern)pattern).SetValue(text);
 else {var original=System.Windows.Clipboard.GetDataObject();try{System.Windows.Clipboard.SetText(text);input.SetFocus();var selectAll=new[]{Key(0x11,false),Key(0x41,false),Key(0x41,true),Key(0x11,true)};SendInput(4,selectAll,Marshal.SizeOf<INPUT>());Paste();var own=WindowsPlatform.InputTick;var until=Environment.TickCount64+2100;while(Environment.TickCount64<until){if(WindowsPlatform.InputTick!=own)return Result(new{code="user_active",attempted=true});Thread.Sleep(25);}}finally{if(System.Windows.Clipboard.ContainsText()&&System.Windows.Clipboard.GetText()==text&&original!=null)System.Windows.Clipboard.SetDataObject(original,true);}}
 Thread.Sleep(100);var readback=Read(input);return Result(new{code=readback!=null&&StagePolicy.CanonicalText(readback)==StagePolicy.CanonicalText(text)?"written":"input_changed",attempted=true,focused=GetForegroundWindow()==hwnd,digest=readback==null?null:Digest(readback),used_enter=false});}
 if(stage=="send"){if(sends.Count!=1||!sends[0].Current.IsEnabled||!sends[0].TryGetCurrentPattern(InvokePattern.Pattern,out var pattern))return Result(new{code="send_unavailable",attempted=false});wrote=true;((InvokePattern)pattern).Invoke();return Result(new{code="sent",attempted=true,focused=GetForegroundWindow()==hwnd,used_enter=false});}
 return Result(new{code="unsupported",attempted=false});
 }catch{return Result(new{code=wrote?"input_failed":"target_unverified",attempted=wrote});}}
 static string Discover(){var rows=new List<SessionRecord>();var clock=Stopwatch.StartNew();var now=DateTimeOffset.UtcNow.ToUnixTimeMilliseconds()/1000d;foreach(var p in Process.GetProcesses()){if(clock.ElapsedMilliseconds>1500||rows.Count>=20)break;try{if(!AppCatalog.TryMatchProcess(p.ProcessName,out var app,out var name)||p.MainWindowHandle==IntPtr.Zero)continue;var e=AutomationElement.FromHandle(p.MainWindowHandle);var title=e.Current.Name.Trim();if(title.Length==0||title.Length>180||AppCatalog.IsBareAppName(title))continue;var hash=Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(title))).ToLowerInvariant()[..12];rows.Add(new SessionRecord{Id="window:"+app+":"+p.Id+":"+p.MainWindowHandle.ToInt64()+":"+hash,AppId=app,AppName=name,Title=title,Status="unknown",StatusReason="仅确认应用窗口标题，具体轮次待核验",Evidence="只读 Windows UI Automation 窗口元数据",Source="window",UpdatedAt=now,Pid=p.Id,Target=""});}catch{}}return Result(new{sessions=rows,errors=Array.Empty<string>(),collected_at=now});}
 static string Result(object value)=>JsonSerializer.Serialize(value);
 static string Digest(string value)=>Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(StagePolicy.CanonicalText(value)))).ToLowerInvariant();
 static List<AutomationElement> Walk(AutomationElement root){var list=new List<AutomationElement>();var pending=new Stack<AutomationElement>();pending.Push(root);var clock=Stopwatch.StartNew();while(pending.Count>0&&list.Count<4096&&clock.ElapsedMilliseconds<2000){var e=pending.Pop();list.Add(e);try{var child=TreeWalker.ControlViewWalker.GetFirstChild(e);while(child!=null&&pending.Count<4096){pending.Push(child);child=TreeWalker.ControlViewWalker.GetNextSibling(child);}}catch{}}if(pending.Count>0)throw new InvalidOperationException("tree_incomplete");return list;}
 static bool IsAction(AutomationElement e,string[] names){try{return !e.Current.IsOffscreen&&e.Current.ControlType==ControlType.Button&&names.Any(x=>e.Current.Name.Trim().Equals(x,StringComparison.OrdinalIgnoreCase));}catch{return false;}}
 static bool IsComposer(AutomationElement e,AutomationElement root){try{if(e.Current.IsOffscreen||!e.Current.IsEnabled||(e.Current.ControlType!=ControlType.Edit&&e.Current.ControlType!=ControlType.Document))return false;var name=e.Current.Name+" "+e.Current.HelpText+" "+e.Current.AutomationId;if(name.Contains("search",StringComparison.OrdinalIgnoreCase)||name.Contains("搜索",StringComparison.Ordinal))return false;var hint=Hints.Any(h=>name.Contains(h,StringComparison.OrdinalIgnoreCase));var bound=e.Current.BoundingRectangle;var area=root.Current.BoundingRectangle;return hint&&bound.Height>10&&bound.Width>100&&bound.Top>area.Top+area.Height*.4;}catch{return false;}}
 static bool IsHeader(AutomationElement e,AutomationElement root,string title,string project){try{if(e.Current.IsOffscreen||e.Current.Name.Trim()!=title)return false;var rect=e.Current.BoundingRectangle;var area=root.Current.BoundingRectangle;if(rect.Top>area.Top+180||rect.Left<area.Left+160)return false;var parent=TreeWalker.ControlViewWalker.GetParent(e);if(parent!=null&&parent.Current.ControlType is var p&&(p==ControlType.ListItem||p==ControlType.TreeItem||p==ControlType.TabItem))return false;var folder=SessionFacts.FolderName(project);return folder.Length==0||root.Current.Name.Contains(folder,StringComparison.Ordinal)||root.Current.HelpText.Contains(project,StringComparison.Ordinal);}catch{return false;}}
 static string? Read(AutomationElement e){try{if(e.TryGetCurrentPattern(ValuePattern.Pattern,out var value))return ((ValuePattern)value).Current.Value;if(e.TryGetCurrentPattern(TextPattern.Pattern,out var pattern)){var text=((TextPattern)pattern).DocumentRange.GetText(2001);return Hints.Any(x=>text.Trim()==x)?"":text;}return null;}catch{return null;}}
}

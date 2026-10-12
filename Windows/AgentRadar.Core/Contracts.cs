using System.Text.Json.Serialization;
namespace AgentRadar.Core;
public sealed class SessionRecord {
 [JsonPropertyName("id")] public string Id {get;set;}="";
 [JsonPropertyName("app_id")] public string AppId {get;set;}="";
 [JsonPropertyName("app_name")] public string AppName {get;set;}="";
 [JsonPropertyName("title")] public string Title {get;set;}="";
 [JsonPropertyName("project")] public string Project {get;set;}="";
 [JsonPropertyName("status")] public string Status {get;set;}="unknown";
 [JsonPropertyName("evidence")] public string Evidence {get;set;}="";
 [JsonPropertyName("updated_at")] public double UpdatedAt {get;set;}
 [JsonPropertyName("source")] public string Source {get;set;}="";
 [JsonPropertyName("target")] public string Target {get;set;}="";
 [JsonPropertyName("started_at")] public double? StartedAt {get;set;}
 [JsonPropertyName("ended_at")] public double? EndedAt {get;set;}
 [JsonPropertyName("last_activity_at")] public double? LastActivityAt {get;set;}
 [JsonPropertyName("timing_basis")] public string? TimingBasis {get;set;}
 [JsonPropertyName("status_reason")] public string? StatusReason {get;set;}
 [JsonPropertyName("navigation_title")] public string? NavigationTitle {get;set;}
 [JsonPropertyName("navigation_key")] public string? NavigationKey {get;set;}
 [JsonPropertyName("lifecycle_revision")] public string? LifecycleRevision {get;set;}
 [JsonPropertyName("run_id")] public string? RunId {get;set;}
 [JsonPropertyName("manual_only")] public bool? ManualOnly {get;set;}
 [JsonPropertyName("continuous_authorized")] public bool? ContinuousAuthorized {get;set;}
 [JsonPropertyName("care_retry_at")] public double? CareRetryAt {get;set;}
 [JsonPropertyName("user_stopped")] public bool? UserStopped {get;set;}
 [JsonPropertyName("pid")] public int? Pid {get;set;}
 public string DisplayName => string.IsNullOrEmpty(Project)?AppName:Project.TrimEnd('/','\\').Split('/','\\').Last();
 public string StatusLabel => UserStopped==true?"已手动停止":Status switch {"running"=>"进行中","completed"=>"本轮完成","interrupted"=>"已中断","waiting"=>"等待确认",_=>"待确认"};
 public string RoundKey => Id+":"+(StartedAt is double start && double.IsFinite(start) ? ((long)(start*1000)).ToString(System.Globalization.CultureInfo.InvariantCulture):"0");
 public bool IsReadable => !string.IsNullOrWhiteSpace(Id)&&!string.IsNullOrWhiteSpace(Title)&&new[]{"running","completed","interrupted","waiting"}.Contains(Status);
}
public sealed class CollectorSnapshot {
 [JsonPropertyName("sessions")] public List<SessionRecord> Sessions {get;set;}=[];
 [JsonPropertyName("errors")] public List<string> Errors {get;set;}=[];
 [JsonPropertyName("collected_at")] public double CollectedAt {get;set;}
}
public sealed class BridgeResult {
 [JsonPropertyName("code")] public string Code {get;set;}="bridge_timeout";
 [JsonPropertyName("attempted")] public bool Attempted {get;set;}
 [JsonPropertyName("sent_at")] public double? SentAt {get;set;}
 [JsonPropertyName("run_id")] public string? RunId {get;set;}
 [JsonPropertyName("retry_after")] public double? RetryAfter {get;set;}
 [JsonPropertyName("diagnostic_summary")] public string? DiagnosticSummary {get;set;}
 [JsonPropertyName("identity")] public bool? Identity {get;set;}
}
public sealed class RadarSettings {
 public bool AutoSend {get;set;}=false; public bool Paused {get;set;}=false;
 public string Mode {get;set;}="collaboration";
 public bool Popup {get;set;}=true; public bool Sound {get;set;}=true;
 public bool KeepAwake {get;set;}=false; public bool StartWithWindows {get;set;}=false;
 public bool Expanded {get;set;}=false; public double X {get;set;}=double.NaN; public double Y {get;set;}=double.NaN;
 public double Transparency {get;set;}=.75; public string Frost {get;set;}="适中";
 public string ResumeText {get;set;}="";
 public string InterruptText {get;set;}="刚才中断了，请继续";
 public string ContinueText {get;set;}="请继续当前项目，先完成尚未完成的工作，再检查已有改动，找出下一项有价值的优化并实施；完成后实际测试，说明改动、验证结果和剩余问题。";
 public string AlternateText {get;set;}="请从真实使用者的角度走查当前项目，指出具体体验问题，提出可验证的改进建议并实施优化；完成后实际测试，说明改动、验证结果和剩余问题。";
 public int AlternateEvery {get;set;}=3; public bool RateLimitWake {get;set;}=false;
 public int RateLimitSeconds {get;set;}=10; public string RateLimitText {get;set;}="刚才被限流了，现在请继续";
 public Dictionary<string,string> AppContinueTexts {get;set;}=[]; public Dictionary<string,string> AppResumeTexts {get;set;}=[];
 public HashSet<string> SupervisedIds {get;set;}=[]; public HashSet<string> SupervisedApps {get;set;}=[];
 public HashSet<string> CareIds {get;set;}=[]; public HashSet<string> RemovedIds {get;set;}=[];
 public Dictionary<string,int> SendCounts {get;set;}=[];
 public Dictionary<string,string> Labels {get;set;}=[]; public HashSet<string> Pins {get;set;}=[];
 public string OpenClawGateway {get;set;}="ws://127.0.0.1:18789";
 public string OpenClawHome {get;set;}=""; public string WslDistribution {get;set;}="";
 public string WslExecutable {get;set;}="/usr/local/bin/openclaw";
}
public sealed record NoticeRecord(string Id,string SessionId,string Title,string Conversation,string Message,string Kind,bool CanRecover,bool Recovering=false,int? RemainingSeconds=null);
public sealed record NavigationResult(bool OpenedConversation,string Message);
public interface IWorkerClient : IDisposable {
 event Action<CollectorSnapshot>? Snapshot; event Action<string>? Fault;
 Task StartAsync(CancellationToken cancellationToken=default); void Stop();
 Task<BridgeResult> RunBridgeAsync(Dictionary<string,object?> request,CancellationToken cancellationToken=default);
}
public interface IPlatformServices {
 double IdleSeconds {get;} bool DesktopUsable {get;}
 Task<NavigationResult> OpenAsync(SessionRecord session,CancellationToken cancellationToken=default);
 Task<string> CheckAccessibilityAsync(CancellationToken cancellationToken=default);
 void SetKeepAwake(bool enabled); void SetStartup(bool enabled);
}

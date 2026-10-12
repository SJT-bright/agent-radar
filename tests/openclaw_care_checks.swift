import Foundation
@main struct OpenClawCareChecks {
 static func main() throws {
  let root = FileManager.default.temporaryDirectory.appendingPathComponent("radar-care-"+UUID().uuidString)
  try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
  defer { try? FileManager.default.removeItem(at: root) }
  let file = root.appendingPathComponent("selected.json")
  let id = "openclaw:main:11111111-1111-4111-8111-111111111111"
  let other = "openclaw:main:22222222-2222-4222-8222-222222222222"
  var checks = 0
  func check(_ ok: Bool, _ message: String) { precondition(ok, message); checks += 1 }
  check(try OpenClawCareRegistry.read(file).care.isEmpty, "missing registry does not authorize")
  let first = try OpenClawCareRegistry.update(file,id: id,enabled: true)
  check(first.ids==[id] && first.care==[id], "only exact chosen session")
  let second = try OpenClawCareRegistry.update(file,id: other,enabled: false)
  check(second.ids==[id,other] && second.care==[id], "visibility does not authorize other session")
  let restored = try OpenClawCareRegistry.read(file)
  check(restored.care==[id], "restart retains explicit care")
  check(try FileManager.default.attributesOfItem(atPath: file.path)[.posixPermissions] as? Int == 0o600, "private app-owned registry")
  _ = try OpenClawCareRegistry.update(file,id: id,enabled:false)
  check(try OpenClawCareRegistry.read(file).care.isEmpty, "disable persists")
  let before = try Data(contentsOf:file)
  do { _ = try OpenClawCareRegistry.update(file,id:"openclaw:main:not-a-uuid",enabled:true); preconditionFailure("invalid identity accepted") } catch { checks += 1 }
  check(try Data(contentsOf:file)==before,"invalid action preserves old registry")
  let link = root.appendingPathComponent("link.json")
  try FileManager.default.createSymbolicLink(at:link,withDestinationURL:file)
  do { _ = try OpenClawCareRegistry.read(link); preconditionFailure("redirect accepted") } catch { checks += 1 }
  var row = SessionRecord(id:id,app_id:"openclaw",app_name:"OpenClaw",title:"Isolated care target",project:"/fixture",status:"running",evidence:"explicit lifecycle",updated_at:100,source:"local-db",target:"",started_at:100,timing_basis:"turn",navigation_key:"agent:main:dashboard:test",manual_only:true,continuous_authorized:true,care_retry_at:0)
  var policy = ContinuationPolicy()
  _ = policy.observe([row],now:100,supervisedIDs:[id])
  row.status="interrupted";row.ended_at=110;row.status_reason="OpenClaw execution failed"
  let recovery = policy.observe([row],now:110,supervisedIDs:[id])
  check(recovery.count==1 && recovery[0].canContinue,"explicitly selected child auto recovery allowed")
  policy.recordAttempt(row,now:110)
  row.status="running";row.started_at=120;row.ended_at=nil
  _ = policy.observe([row],now:120,supervisedIDs:[id])
  row.status="interrupted";row.ended_at=130
  check(policy.observe([row],now:130,supervisedIDs:[id]).first?.canContinue==true,"care uses persisted hourly budget instead of blanket five-minute throttle")
  row.status="running";row.started_at=140;row.ended_at=nil
  _ = policy.observe([row],now:140,supervisedIDs:[id])
  row.status="interrupted";row.ended_at=150;row.care_retry_at=3700
  let cooled = policy.observe([row],now:150,supervisedIDs:[id])
  check(cooled.first?.canContinue==false,"budget exhausted remains observation only")
  check(cooled.first?.explanation.contains("3") == true,"cooldown reason visible")
  var completedPolicy = ContinuationPolicy();row.status="running";row.started_at=200;row.ended_at=nil;row.care_retry_at=0
  _ = completedPolicy.observe([row],now:200,completionMode:"rage",supervisedIDs:[id]);row.status="completed";row.ended_at=210
  let done=completedPolicy.observe([row],now:210,completionMode:"rage",supervisedIDs:[id])
  check(done.first?.kind=="completion" && done.first?.canContinue==false,"care completion never endlessly restarts finished background work")
  var manualPolicy = ContinuationPolicy();row.status="running";row.started_at=300;row.ended_at=nil;row.continuous_authorized=false
  _ = manualPolicy.observe([row],now:300,supervisedIDs:[id]);row.status="interrupted";row.ended_at=310
  check(manualPolicy.observe([row],now:310,supervisedIDs:[id]).first?.canContinue==false,"unselected background session retains manual-only boundary")
  print("OpenClaw care: \(checks) checks passed; no external app operations")
 }
}

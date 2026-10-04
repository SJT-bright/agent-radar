import Foundation

@main
struct NativeMonitorChecks {
    static func main() {
        let monitor = NativeMonitor.self
        assert(monitor.hasAIMetadata(bundleID: "ai.acme.desktop", name: "Acme"))
        assert(monitor.hasAIMetadata(bundleID: "com.acme.app", name: "Acme AI"))
        assert(monitor.hasAIMetadata(bundleID: "com.acme.app", name: "新智能体"))
        assert(!monitor.hasAIMetadata(bundleID: "com.acme.mail", name: "Mail"))
        assert(!monitor.hasAIMetadata(bundleID: "com.acme.paint", name: "Paint"))
        assert(!monitor.hasAIMetadata(bundleID: "com.apple.wallpaper.agent", name: "墙纸"))
        assert(!monitor.hasAIMetadata(bundleID: "com.openai.chat.computer-use", name: "ChatGPT Computer Use"))
        assert(!monitor.hasAIMetadata(bundleID: "ai.sample.helper", name: "Sample Helper"))
        assert(monitor.discoveryBatch(["a", "b", "c", "d", "e"], cursor: 0) == ["a", "b"])
        assert(monitor.discoveryBatch(["a", "b", "c", "d", "e"], cursor: 4) == ["e", "a"])
        assert(monitor.discoveryBatch([], cursor: 5).isEmpty)
        assert(monitor.buttonStatus(label: "停止生成", enabled: true) == "running")
        assert(monitor.buttonStatus(label: "Stop generating", enabled: true) == "running")
        assert(monitor.buttonStatus(label: "Allow once", enabled: true) == "waiting")
        assert(monitor.buttonStatus(label: "Stop generating", enabled: false) == nil)
        assert(monitor.buttonStatus(label: "Click Stop generating to stop", enabled: true) == nil)
        assert(monitor.buttonStatus(label: "Stop", enabled: true) == nil)
        assert(monitor.buttonStatus(label: "Allow", enabled: true) == nil)
        assert(monitor.buttonStatus(label: "中断运行", enabled: true, appID: "coze") == "running")
        assert(monitor.buttonStatus(label: "中断运行", enabled: false, appID: "coze") == nil)
        assert(monitor.buttonStatus(label: "中断运行", enabled: true, appID: "browser-x") == nil)
        assert(monitor.buttonStatus(label: "已取消 19秒", enabled: true, appID: "coze") == nil)
        assert(monitor.conversationHeading(appID: "coze", role: "AXGroup", label: "编程专家 在线") == "编程专家")
        assert(monitor.conversationHeading(appID: "coze", role: "AXStaticText", label: "编程专家 在线") == nil)
        assert(monitor.conversationHeading(appID: "coze", role: "AXStaticText", label: "编程专家 在线", depth: 7) == "编程专家")
        assert(monitor.conversationHeading(appID: "coze", role: "AXStaticText", label: "编程专家 在线", depth: 15) == nil)
        assert(monitor.conversationHeading(appID: "qoder-cn", role: "AXGroup", label: "编程专家 在线") == nil)
        assert(monitor.browserService("项目一 - ChatGPT - Google Chrome")?.id == "chatgpt")
        assert(monitor.browserService("DeepSeek - 探索未至之境")?.id == "deepseek")
        assert(monitor.browserService("Learn ChatGPT development - Google Chrome") == nil)
        assert(monitor.browserService("普通项目 - Google Chrome") == nil)
        let thread = "codex://threads/01a0c8b7-579f-7630-bec2-13c4afbdbf4a"
        assert(monitor.safeDeepLink(thread, appID: "codex") != nil)
        assert(monitor.safeDeepLink(thread, appID: "claude") == nil)
        assert(monitor.safeDeepLink("codex://threads/not-an-id", appID: "codex") == nil)
        assert(monitor.safeDeepLink("codex://threads//01a0c8b7-579f-7630-bec2-13c4afbdbf4a", appID: "codex") == nil)
        assert(monitor.safeDeepLink(thread + "?command=delete", appID: "codex") == nil)
        assert(monitor.safeDeepLink("file:///tmp/sample", appID: "codex") == nil)
        assert(monitor.safeDeepLink("workbuddy://chat/session-123", appID: "workbuddy") != nil)
        assert(monitor.safeDeepLink("workbuddy://chat/session-123", appID: "workbuddy-ai") == nil)
        assert(monitor.safeDeepLink("workbuddy://chat/foo%2Fbar", appID: "workbuddy") == nil)
        assert(monitor.safeDeepLink("workbuddy://chat/..", appID: "workbuddy") == nil)
        assert(monitor.safeDeepLink("zcode://workspace/open?path=%2FUsers%2Fexample%2FDownloads", appID: "zcode") != nil)
        let zcodeWorkspace = "zcode://workspace/open?path=%2FUsers%2Fexample%2F%E4%B8%AD%E6%96%87%E9%A1%B9%E7%9B%AE"
        assert(monitor.safeDeepLink(zcodeWorkspace, appID: "zcode") != nil)
        assert(monitor.safeDeepLink(zcodeWorkspace, appID: "zcode").flatMap { URLComponents(string: $0.absoluteString)?.queryItems?.first?.value } == "/Users/example/中文项目")
        assert(monitor.safeDeepLink("zcode://workspace/open?path=Users%2Fx", appID: "zcode") == nil)
        assert(monitor.safeDeepLink("zcode://workspace/open?session=%2FUsers%2Fx", appID: "zcode") == nil)
        assert(monitor.safeDeepLink("zcode://workspace/open?path=%2Fa%2F..%2Fb", appID: "zcode") == nil)
        assert(monitor.safeDeepLink("zcode://workspace/open?path=%2F.%2Fetc", appID: "zcode") == nil)
        assert(monitor.safeDeepLink("zcode://workspace/open?path=%2Fa&force=1", appID: "zcode") == nil)
        assert(monitor.safeDeepLink("zcode://workspace/open", appID: "zcode") == nil)
        assert(monitor.safeDeepLink("zcode://other/open?path=%2Fa", appID: "zcode") == nil)
        assert(monitor.safeDeepLink("zcode://workspace/open?path=%2FUsers%2Fexample%2FDownloads", appID: "workbuddy") == nil)
        assert(monitor.safeDeepLink("zcode://workspace/open?path=%2Fa%0Ab", appID: "zcode") == nil)
        let overlongWorkspace = "%2F" + String(repeating: "a", count: 512)
        assert(monitor.safeDeepLink("zcode://workspace/open?path=" + overlongWorkspace, appID: "zcode") == nil)
        // 真实生效探针：字段自洽（未授权必然没有读取数），且有界及时返回。
        let probeStart = Date()
        let probe = NativeMonitor.accessibilityProbe()
        assert(!probe.granted || probe.appsProbed <= 6)
        assert(probe.granted || (probe.appsProbed == 0 && probe.windowsRead == 0 && probe.sampleName == nil))
        assert(Date().timeIntervalSince(probeStart) < 4.0)
        let parents = monitor.processParentMap(" 100 90\n90\t80\n80 1\n1 0\ninvalid row\n50 -1\n2 0 extra\n4294967296 1\n")
        assert(parents == [100: 90, 90: 80, 80: 1])
        assert(monitor.nearestGUIAncestor(pid: 100, parents: parents, guiPIDs: [90, 80]) == 90)
        assert(monitor.nearestGUIAncestor(pid: 100, parents: parents, guiPIDs: [80]) == 80)
        assert(monitor.nearestGUIAncestor(pid: 100, parents: parents, guiPIDs: [777]) == nil)
        assert(monitor.nearestGUIAncestor(pid: 101, parents: parents, guiPIDs: [80]) == nil)
        assert(monitor.nearestGUIAncestor(pid: 100, parents: [100: 90, 90: 100], guiPIDs: [100]) == nil)
        assert(monitor.nearestGUIAncestor(pid: 1, parents: [1: 80], guiPIDs: [80]) == nil)
        let longChain = Dictionary(uniqueKeysWithValues: (100...140).map { (Int32($0), Int32($0 + 1)) })
        assert(monitor.nearestGUIAncestor(pid: 100, parents: longChain, guiPIDs: [132]) == 132)
        assert(monitor.nearestGUIAncestor(pid: 100, parents: longChain, guiPIDs: [133]) == nil)
        let start = Date()
        let realParents = monitor.readProcessParents()
        assert(realParents != nil && !(realParents?.isEmpty ?? true))
        assert(Date().timeIntervalSince(start) < 1.5)
        let missingCLI = SessionRecord(id: "test", app_id: "claude-code", app_name: "Claude Code",
            title: "test", project: "", status: "unknown", evidence: "", updated_at: 0,
            source: "test", target: "", pid: nil, window_id: nil)
        var completions = 0
        NativeMonitor().activate(missingCLI) { success in
            assert(Thread.isMainThread && success == .unavailable)
            completions += 1
        }
        assert(completions == 1)
        // 预算耗尽应用复用上一轮真实窗口行：ID 稳定、说明只追加在副本上、
        // 无历史时退回占位。这防止占位 ID 翻转摇动以会话 ID 为键的监督状态。
        func check(_ ok: Bool, _ name: String) { precondition(ok, name) }
        do {
            let app = AppRecord(id: "workbuddy", name: "WorkBuddy", bundleID: "com.workbuddy", pid: 42, path: "/Applications/WorkBuddy.app")
            let previous = [SessionRecord(id: "window:workbuddy:42:3", app_id: "workbuddy", app_name: "WorkBuddy",
                title: "进行中的对话", project: "", status: "running", evidence: "窗口状态可读",
                updated_at: 1, source: "window", target: "", pid: 42, window_id: 3)]
            let carried = NativeMonitor.carriedRows(previous: previous, app: app,
                                                    note: "本轮读取时间已用尽；显示上一轮窗口读取结果")
            check(carried.count == 1 && carried[0].id == previous[0].id, "carried rows keep stable session id")
            check(carried[0].status == "running" && carried[0].evidence.contains("上一轮"), "carried rows reuse real status with note")
            check(previous[0].evidence == "窗口状态可读", "carried note does not pollute stored rows")
            let fallback = NativeMonitor.carriedRows(previous: nil, app: app,
                                                     note: "窗口读取达到时间限制；显示上一轮窗口读取结果")
            check(fallback.count == 1 && fallback[0].id == "app:workbuddy:42" && fallback[0].title.contains("会话待识别"),
                  "no history falls back to placeholder")
            let empty = NativeMonitor.carriedRows(previous: [], app: app, note: "x")
            check(empty.count == 1 && empty[0].id == "app:workbuddy:42", "empty history falls back to placeholder")
        }
        print("NativeMonitor: 75 discovery, evidence, deep-link, carry-over, and CLI-container checks passed")
    }
}

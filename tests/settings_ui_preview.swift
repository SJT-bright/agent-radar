import AppKit
import Foundation

/// Compile with Sources/*.swift except main.swift. This process never calls
/// MonitorStore.start(); its continuation sender cannot touch another app.
private final class SettingsPreviewSender: ContinuationSending {
    var isRunning: Bool { false }
    func cancel() {}
    func run(_ request: [String: Any], completion: @escaping (String, Bool, Double?) -> Void) {
        // Report no attempt even when an isolated preview toggle is enabled.
        completion("preview_sending_disabled", false, nil)
    }
}

private final class SettingsPreviewDelegate: NSObject, NSApplicationDelegate {
    private let suiteName = "local.agentradar.settings-preview." + UUID().uuidString
    private var previewDefaults: UserDefaults?
    private var store: MonitorStore?
    private var terminationSignals: [DispatchSourceSignal] = []
    private var didCleanUp = false
    private var diagnosticsTimer: Timer?
    private var diagnosticsObservers: [NSObjectProtocol] = []

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Exercise the accessory app's missing-menu path without production data.
        if !ProcessInfo.processInfo.arguments.contains("--no-app-menu") { installMenu() }
        installTerminationHandlers()
        guard let defaults = UserDefaults(suiteName: suiteName) else {
            log("Unable to create isolated preferences")
            NSApp.terminate(nil)
            return
        }
        previewDefaults = defaults
        defaults.set(false, forKey: "autoContinueOptIn.v2")
        defaults.set(false, forKey: "autoQueueInsertion.v1")
        defaults.set(false, forKey: "keepAwakeEnabled.v1")
        defaults.set([
            "preview:codex:00", "preview:codex:01", "preview:codex:15", "preview:codex:missing"
        ], forKey: MonitorStore.supervisionDefaultsKey)
        defaults.set(["zcode"], forKey: MonitorStore.appSupervisionDefaultsKey)
        var rules = PromptRules()
        rules.completionMode = "rage"
        rules.save(to: defaults)

        let sender = SettingsPreviewSender()
        let controller = ContinuationController(bridge: sender, judgeBridge: sender, countDefaults: defaults)
        let store = MonitorStore(defaults: defaults, writeHealthDiagnostics: false, continuation: controller)
        self.store = store
        store.sessions = fixtureSessions(now: Date().timeIntervalSince1970)
        store.apps = previewApps.map {
            AppRecord(id: $0.id, name: $0.name, bundleID: "local.agentradar.preview." + $0.id,
                      pid: 0, path: "/AgentRadarSettingsPreview/" + $0.name + ".app")
        }
        store.accessibilityCheckDetail = "独立设置预览：使用隔离偏好与模拟会话，未启动监控。"
        store.recoverySummary = "独立预览 · 发送已禁用"

        // An optional collector snapshot is display-only. Identity targets are
        // replaced so the preview never retains a usable external destination.
        let arguments = ProcessInfo.processInfo.arguments
        if let option = arguments.firstIndex(of: "--snapshot") {
            guard arguments.indices.contains(option + 1) else {
                log("--snapshot requires a JSON file path")
                NSApp.terminate(nil)
                return
            }
            do {
                let data = try Data(contentsOf: URL(fileURLWithPath: arguments[option + 1]))
                let snapshot = try JSONDecoder().decode(CollectorSnapshot.self, from: data)
                store.sessions = snapshot.sessions.map { source in
                    var row = source
                    row.target = "agentradar-preview://session"
                    row.pid = nil
                    row.window_id = nil
                    return row
                }
                store.apps = []
            } catch {
                log("Snapshot could not be loaded: \(error.localizedDescription)")
                NSApp.terminate(nil)
                return
            }
        }
        if arguments.contains("--window-diagnostics") { installWindowDiagnostics() }
        openSettings()
        log("ready pid=\(ProcessInfo.processInfo.processIdentifier) suite=\(suiteName) sessions=\(store.sessions.count)")
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationWillTerminate(_ notification: Notification) {
        cleanUp()
    }

    @objc private func openSettings() {
        guard let store = store else { return }
        PromptSettingsController.shared.showGlobal(store: store)
        // The real settings controller remains unchanged; only this process's
        // window title and identifier identify the isolated review surface.
        for window in NSApp.windows where window.isVisible && window.sheetParent == nil {
            window.title = "AgentRadarSettingsPreview · 任务雷达设置（隔离预览）"
            window.identifier = NSUserInterfaceItemIdentifier("AgentRadarSettingsPreview")
        }
    }

    @objc private func quitPreview() { NSApp.terminate(nil) }

    private func installMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu(title: "AgentRadarSettingsPreview")
        let reopen = NSMenuItem(title: "再次打开设置", action: #selector(openSettings), keyEquivalent: ",")
        reopen.target = self
        appMenu.addItem(reopen)
        appMenu.addItem(.separator())
        let quit = NSMenuItem(title: "退出预览", action: #selector(quitPreview), keyEquivalent: "q")
        quit.target = self
        appMenu.addItem(quit)
        appItem.submenu = appMenu
        main.addItem(appItem)
        NSApp.mainMenu = main
    }

    private func installTerminationHandlers() {
        for number in [SIGTERM, SIGINT] {
            signal(number, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
            source.setEventHandler { NSApp.terminate(nil) }
            source.resume()
            terminationSignals.append(source)
        }
    }

    /// Read-only diagnostics for separating window lifecycle failures from a
    /// stalled external AX client. No prompt text or accessibility traversal is read.
    private func installWindowDiagnostics() {
        let windowEvents: [Notification.Name] = [
            NSWindow.willCloseNotification,
            NSWindow.didBecomeKeyNotification,
            NSWindow.didResignKeyNotification,
            NSWindow.didBecomeMainNotification,
            NSWindow.didResignMainNotification,
            NSWindow.didMiniaturizeNotification,
            NSWindow.didDeminiaturizeNotification
        ]
        let appEvents: [Notification.Name] = [
            NSApplication.didBecomeActiveNotification,
            NSApplication.didResignActiveNotification,
            NSApplication.didHideNotification,
            NSApplication.didUnhideNotification
        ]
        for name in windowEvents + appEvents {
            let observer = NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { [weak self] note in
                let number = (note.object as? NSWindow)?.windowNumber ?? -1
                self?.logWindowDiagnostics(event: "\(note.name.rawValue):\(number)")
            }
            diagnosticsObservers.append(observer)
        }
        let timer = Timer(timeInterval: 2, repeats: true) { [weak self] _ in
            self?.logWindowDiagnostics(event: "heartbeat")
        }
        diagnosticsTimer = timer
        RunLoop.main.add(timer, forMode: .common)
        RunLoop.main.add(timer, forMode: .modalPanel)
        logWindowDiagnostics(event: "installed")
    }

    private func logWindowDiagnostics(event: String) {
        let windows: [[String: Any]] = NSApp.windows.map { window in
            [
                "number": window.windowNumber,
                "identity": String(describing: ObjectIdentifier(window)),
                "class": String(describing: type(of: window)),
                "identifier": window.identifier?.rawValue ?? "",
                "visible": window.isVisible,
                "key": window.isKeyWindow,
                "main": window.isMainWindow,
                "miniaturized": window.isMiniaturized,
                "releasedWhenClosed": window.isReleasedWhenClosed,
                "occlusion": window.occlusionState.rawValue,
                "level": window.level.rawValue,
                "frame": NSStringFromRect(window.frame),
                "contentView": window.contentView.map { String(describing: type(of: $0)) } ?? "nil",
                "delegate": window.delegate.map { String(describing: type(of: $0)) } ?? "nil",
                "sheetParent": window.sheetParent?.windowNumber ?? -1
            ]
        }
        let state: [String: Any] = [
            "event": event,
            "uptime": ProcessInfo.processInfo.systemUptime,
            "active": NSApp.isActive,
            "hidden": NSApp.isHidden,
            "keyWindow": NSApp.keyWindow?.windowNumber ?? -1,
            "mainWindow": NSApp.mainWindow?.windowNumber ?? -1,
            "modalWindow": NSApp.modalWindow?.windowNumber ?? -1,
            "windows": windows
        ]
        guard let data = try? JSONSerialization.data(withJSONObject: state, options: [.sortedKeys]),
              let message = String(data: data, encoding: .utf8) else { return }
        log("window-diagnostics " + message)
    }

    private func cleanUp() {
        guard !didCleanUp else { return }
        didCleanUp = true
        diagnosticsTimer?.invalidate()
        diagnosticsTimer = nil
        diagnosticsObservers.forEach { NotificationCenter.default.removeObserver($0) }
        diagnosticsObservers.removeAll()
        store?.stop()
        previewDefaults?.removePersistentDomain(forName: suiteName)
        previewDefaults?.synchronize()
        terminationSignals.forEach { $0.cancel() }
        log("closed; isolated preferences removed")
    }

    private func log(_ message: String) {
        FileHandle.standardOutput.write(Data(("AgentRadarSettingsPreview: " + message + "\n").utf8))
    }

    private let previewApps = [(id: "codex", name: "Codex"), (id: "grok", name: "Grok"), (id: "zcode", name: "ZCode")]

    private func fixtureSessions(now: Double) -> [SessionRecord] {
        let statuses = ["running", "completed", "waiting", "interrupted", "idle", "stalled",
                        "running", "completed", "running", "waiting", "completed", "interrupted",
                        "idle", "completed", "running", "unknown"]
        return previewApps.enumerated().flatMap { appIndex, app in
            statuses.enumerated().map { index, status in
                let suffix = String(format: "%02d", index)
                let title: String
                if index == 0 || index == 2 {
                    title = "优化设置页面与监督列表"
                } else if index == 5 || index == 14 {
                    title = "检查跨项目长标题显示、监督范围筛选、未保存提示词恢复与窗口缩放后的固定操作栏布局 \(suffix)"
                } else if status == "unknown" {
                    title = "已选择但当前状态待确认的会话"
                } else {
                    title = ["整理任务入口", "验证提示词保存", "改进使用反馈", "检查边界情况"][index % 4] + " · " + suffix
                }
                let updated = now - Double(appIndex * 30 + index * 60)
                let projectName = index % 2 == 0 ? "项目甲" : "项目乙"
                let project = "/AgentRadarSettingsPreview/\(app.name)/\(projectName)-\(suffix)"
                return SessionRecord(
                    id: "preview:\(app.id):\(suffix)", app_id: app.id, app_name: app.name,
                    title: title, project: project, status: status,
                    evidence: "隔离设置预览的模拟会话", updated_at: updated,
                    source: "local-session", target: "agentradar-preview://session/\(app.id)/\(suffix)",
                    started_at: updated - 600,
                    ended_at: ["completed", "idle", "interrupted"].contains(status) ? updated : nil,
                    last_activity_at: updated, timing_basis: "turn",
                    status_reason: status == "unknown" ? "模拟证据过期，选择仍保留" : "独立预览数据")
            }
        }
    }
}

@main struct AgentRadarSettingsPreview {
    static func main() {
        ProcessInfo.processInfo.processName = "AgentRadarSettingsPreview"
        let application = NSApplication.shared
        application.setActivationPolicy(.regular)
        let delegate = SettingsPreviewDelegate()
        application.delegate = delegate
        withExtendedLifetime(delegate) { application.run() }
    }
}

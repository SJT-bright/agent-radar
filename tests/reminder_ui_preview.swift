import AppKit
import SwiftUI

private final class PreviewReminderSender: ContinuationSending {
    var isRunning: Bool { false }
    func cancel() {}
    func run(_ request: [String: Any], completion: @escaping (String, Bool, Double?) -> Void) {
        completion("preview_sending_disabled", false, nil)
    }
}

private final class PreviewRadarPanel: NSPanel {
    override var canBecomeKey: Bool { true }
}

/// Production settings, radar, coordinator and toast with isolated preferences.
/// MonitorStore.start is never called; no collector or external sender runs.
private final class ReminderPreviewDelegate: NSObject, NSApplicationDelegate {
    private let suiteName = "local.agentradar.reminder-preview." + UUID().uuidString
    private var defaults: UserDefaults!
    private var store: MonitorStore!
    private let toast = ContinuationToast()
    private let sound = ReminderSoundPlayer()
    private var coordinator: ReminderCoordinator!
    private var controls: NSWindow!
    private var radar: NSPanel!
    private var status: NSTextField!
    private var playbackCount = 0
    private var round = 0
    private var currentNotice: ContinuationNotice?
    private var hoverCaptureTimer: Timer?
    private var capturedHover = false
    private let receipt = FileManager.default.temporaryDirectory.appendingPathComponent("agentradar-reminder-preview-events.jsonl")

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        defaults = UserDefaults(suiteName: suiteName)!
        defaults.set(false, forKey: "autoContinueOptIn.v2")
        defaults.set(false, forKey: "autoQueueInsertion.v1")
        let sender = PreviewReminderSender()
        let continuation = ContinuationController(bridge: sender, judgeBridge: sender,
                                                 inputIdle: { 1000 }, countDefaults: defaults)
        store = MonitorStore(defaults: defaults, writeHealthDiagnostics: false, continuation: continuation)
        store.expanded = true
        let now = Date().timeIntervalSince1970
        store.sessions = [fixture(start: now - 8, status: "running")]
        radar = PreviewRadarPanel(contentRect: NSRect(x: 1160, y: 290, width: 238, height: 440),
                        styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        radar.title = "任务雷达 · 隔离验收"
        radar.level = .floating
        radar.isFloatingPanel = true
        radar.hidesOnDeactivate = false
        radar.isMovableByWindowBackground = false
        radar.becomesKeyOnlyIfNeeded = false
        radar.acceptsMouseMovedEvents = true
        radar.isOpaque = false; radar.backgroundColor = .clear
        radar.appearance = NSAppearance(named: .darkAqua)
        radar.contentView = NSHostingView(rootView: RadarView(store: store))
        radar.orderFrontRegardless()
        coordinator = ReminderCoordinator(store: store, toast: toast, anchor: { [weak self] in self?.radar.frame ?? .zero }, soundPlayer: sound)
        sound.onPlaybackStarted = { [weak self] in
            guard let self = self else { return }
            self.playbackCount += 1
            self.log("audio_started")
            self.updateStatus()
        }
        let savedPreferencesCallback = store.onReminderPreferencesChanged
        store.onReminderPreferencesChanged = { [weak self] in
            savedPreferencesCallback?()
            self?.log("preferences_saved")
            self?.updateStatus()
        }
        makeControls()
        installMenu()
        let hoverTimer = Timer(timeInterval: 0.02, repeats: true) { [weak self] _ in
            guard let self = self, self.store.settingsMenuTracking, !self.capturedHover,
                  let view = self.radar.contentView,
                  let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
            view.cacheDisplay(in: view.bounds, to: bitmap)
            guard let png = bitmap.representation(using: .png, properties: [:]) else { return }
            let file = FileManager.default.temporaryDirectory.appendingPathComponent("agentradar-hover-native.png")
            try? png.write(to: file)
            self.capturedHover = true
            self.log("native_menu_hover_render")
        }
        hoverCaptureTimer = hoverTimer
        RunLoop.main.add(hoverTimer, forMode: .common)
        updateStatus()
        log("ready")
    }

    private func fixture(start: Double, status: String) -> SessionRecord {
        SessionRecord(id: "preview:completion", app_id: "codex", app_name: "Codex", title: "增加开机自启动开关",
                      project: "/tmp/个人资料库", status: status, evidence: "isolated lifecycle fixture",
                      updated_at: Date().timeIntervalSince1970, source: "local-session", target: "preview://task",
                      started_at: start, ended_at: status == "completed" ? Date().timeIntervalSince1970 : nil,
                      timing_basis: "turn")
    }

    private func makeControls() {
        controls = NSWindow(contentRect: NSRect(x: 410, y: 360, width: 540, height: 250),
                            styleMask: [.titled, .closable], backing: .buffered, defer: false)
        controls.title = "提醒功能 · 隔离验收"
        controls.isReleasedWhenClosed = false
        let stack = NSStackView(); stack.orientation = .vertical; stack.spacing = 12
        stack.alignment = .leading; stack.translatesAutoresizingMaskIntoConstraints = false
        status = NSTextField(wrappingLabelWithString: "")
        stack.addArrangedSubview(status)
        let note = NSTextField(wrappingLabelWithString: "使用产品组件和隔离偏好；未启动监控，不会操作任何真实 AI 对话。")
        stack.addArrangedSubview(note)
        for buttons in [[("模拟任务完成", #selector(completion)), ("打开设置", #selector(settings))],
                        [("刷新同一提醒", #selector(refreshNotice)), ("下一条提醒", #selector(nextNotice)), ("清除全部提醒", #selector(clearNotices))],
                        [("提醒位置回执", #selector(recordFrame)), ("关闭预览", #selector(quit))]] {
            let row = NSStackView(); row.orientation = .horizontal; row.spacing = 10
            for (title, action) in buttons {
                let button = NSButton(title: title, target: self, action: action); button.bezelStyle = .rounded
                row.addArrangedSubview(button)
            }
            stack.addArrangedSubview(row)
        }
        controls.contentView!.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: controls.contentView!.leadingAnchor, constant: 20),
            stack.trailingAnchor.constraint(equalTo: controls.contentView!.trailingAnchor, constant: -20),
            stack.topAnchor.constraint(equalTo: controls.contentView!.topAnchor, constant: 20)
        ])
        controls.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func completion() {
        round += 1
        let started = Date().timeIntervalSince1970 - 0.05
        store.onSessionsObserved?([fixture(start: started, status: "running")], true)
        let completed = fixture(start: started, status: "completed")
        store.onSessionsObserved?([completed], true)
        store.sessions = [completed]
        let notice = ContinuationNotice(title: "Codex · 完成待验收", conversation: completed.title,
                                        message: "这个项目已完结，请验收", isCompletion: true,
                                        sessionID: completed.id, recoveryKey: ContinuationPolicy.key(completed),
                                        project: completed.project)
        currentNotice = notice
        store.onRecoveryNotice?(notice)
        log("completion_\(round)")
        updateStatus()
    }
    @objc private func refreshNotice() {
        if let notice = currentNotice { store.onRecoveryNotice?(notice) }
        log("same_notice_refresh")
    }
    @objc private func settings() { store.showPromptSettings() }
    @objc private func nextNotice() { toast.close(); log("next_notice") }
    @objc private func clearNotices() { toast.closeAll(); log("clear_notices") }
    @objc private func recordFrame() { log("frame") }
    @objc private func showControls() { controls.makeKeyAndOrderFront(nil) }
    @objc private func showToast() {
        guard let panel = NSApp.windows.first(where: { $0.title == "AI 监督提醒" }), panel.isVisible else { return }
        controls.orderOut(nil)
        radar.orderOut(nil)
        panel.makeKeyAndOrderFront(nil)
    }
    @objc private func showRadar() {
        controls.orderFront(nil)
        for panel in NSApp.windows where panel.title == "AI 监督提醒" { panel.orderOut(nil) }
        radar.makeKeyAndOrderFront(nil)
    }
    @objc private func quit() { NSApp.terminate(nil) }
    private func updateStatus() {
        status?.stringValue = "音效播放成功：\(playbackCount) 次；弹窗：\(store.reminderPopupEnabled ? "开" : "关")；音效：\(store.reminderSoundEnabled ? "开" : "关")"
    }
    private func log(_ event: String) {
        let panel = NSApp.windows.first { $0.title == "AI 监督提醒" }
        var value: [String: Any] = ["event": event, "time": Date().timeIntervalSince1970,
                                  "audio_started": playbackCount, "popup_enabled": store.reminderPopupEnabled,
                                  "sound_enabled": store.reminderSoundEnabled, "popup_visible": panel?.isVisible ?? false]
        if let frame = panel?.frame { value["frame"] = [frame.minX, frame.minY, frame.width, frame.height] }
        if let data = try? JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]) {
            if !FileManager.default.fileExists(atPath: receipt.path) { FileManager.default.createFile(atPath: receipt.path, contents: nil) }
            if let handle = try? FileHandle(forWritingTo: receipt) {
                _ = try? handle.seekToEnd(); try? handle.write(contentsOf: data + Data("\n".utf8)); try? handle.close()
            }
        }
    }
    private func installMenu() {
        let menu = NSMenu(); let item = NSMenuItem(); let appMenu = NSMenu()
        for (title, action, key) in [("查看控制面板", #selector(showControls), "1"),
                                    ("查看提醒框", #selector(showToast), "2"),
                                    ("查看三点按钮", #selector(showRadar), "3"),
                                    ("打开设置", #selector(settings), "4")] {
            let entry = NSMenuItem(title: title, action: action, keyEquivalent: key); entry.target = self
            appMenu.addItem(entry)
        }
        appMenu.addItem(.separator())
        let quit = NSMenuItem(title: "关闭预览", action: #selector(quit), keyEquivalent: "q"); quit.target = self
        appMenu.addItem(quit); item.submenu = appMenu; menu.addItem(item); NSApp.mainMenu = menu
    }
    func applicationWillTerminate(_ notification: Notification) {
        hoverCaptureTimer?.invalidate()
        toast.closeAll()
        if let defaults = defaults { defaults.removePersistentDomain(forName: suiteName) }
    }
}

@main struct ReminderPreview {
    static func main() {
        let app = NSApplication.shared; let delegate = ReminderPreviewDelegate()
        app.delegate = delegate; app.run()
    }
}

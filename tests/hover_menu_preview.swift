import AppKit

/// The production trigger and popover, with fixture-only actions. No store,
/// collector, defaults or external sender is created by this preview process.
private final class HoverPreviewAction: NSMenuItem {
    let perform: () -> Void
    init(_ title: String, perform: @escaping () -> Void) {
        self.perform = perform
        super.init(title: title, action: #selector(invoke), keyEquivalent: "")
        target = self
    }
    required init(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    @objc private func invoke() { perform() }
}

private final class HoverPreviewDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    private var status: NSTextField!
    private var button: HoverSettingsButton!
    private var timer: Timer?
    private var ticks = 0
    private var menuOpen = false
    private var actions = 0
    private let receipt = URL(fileURLWithPath: "/tmp/agentradar-hover-preview-events.jsonl")

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        let menu = NSMenu()
        let application = NSMenuItem(); application.submenu = NSMenu()
        application.submenu?.addItem(withTitle: "退出悬停预览", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        menu.addItem(application); NSApp.mainMenu = menu
        window = NSWindow(contentRect: NSRect(x: 500, y: 380, width: 560, height: 230), styleMask: [.titled, .closable, .miniaturizable], backing: .buffered, defer: false)
        window.title = "任务雷达 · 三点悬停隔离预览"
        window.appearance = NSAppearance(named: .darkAqua)
        let background = NSView(frame: NSRect(x: 0, y: 0, width: 560, height: 230))
        background.wantsLayer = true
        background.layer?.backgroundColor = NSColor(calibratedWhite: 0.12, alpha: 1).cgColor
        let instructions = NSTextField(wrappingLabelWithString: "将鼠标移到右上角三点：自动浮起并打开菜单。\n离开按钮和菜单：自动落下关闭。\n可核验子菜单、Esc、外部点击、方向键和 Enter。\n计数继续增长用于观察主线程没有被菜单阻塞。")
        instructions.frame = NSRect(x: 24, y: 80, width: 470, height: 115)
        background.addSubview(instructions)
        status = NSTextField(labelWithString: "")
        status.frame = NSRect(x: 24, y: 30, width: 500, height: 30)
        background.addSubview(status)
        button = HoverSettingsButton()
        button.frame = NSRect(x: 512, y: 192, width: 30, height: 26)
        button.image = NSImage(systemSymbolName: "ellipsis", accessibilityDescription: "设置")?
            .withSymbolConfiguration(.init(pointSize: 12, weight: .regular))
        button.makeMenu = { [weak self] in self?.fixtureMenu() ?? NSMenu() }
        button.trackingChanged = { [weak self] opened in
            guard let self else { return }
            self.menuOpen = opened
            self.log(opened ? "menu_opened" : "menu_closed")
            self.refreshStatus()
        }
        background.addSubview(button)
        window.contentView = background
        window.center()
        if let screen = window.screen ?? NSScreen.main {
            let visible = screen.visibleFrame.insetBy(dx: 12, dy: 12)
            var frame = window.frame
            frame.origin.x = min(max(frame.minX, visible.minX), max(visible.minX, visible.maxX - frame.width))
            frame.origin.y = min(max(frame.minY, visible.minY), max(visible.minY, visible.maxY - frame.height))
            window.setFrame(frame, display: false)
        }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        let timer = Timer(timeInterval: 0.02, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.ticks += 1
            if self.ticks % 10 == 0 { self.refreshStatus() }
        }
        self.timer = timer
        RunLoop.main.add(timer, forMode: .common)
        refreshStatus(); log("ready")
        let arguments = ProcessInfo.processInfo.arguments
        if arguments.contains("--accessibility-self-check") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { self.runAccessibilityChecks() }
        }
        if let position = arguments.firstIndex(of: "--duration"), arguments.indices.contains(position + 1),
           let duration = Double(arguments[position + 1]) {
            DispatchQueue.main.asyncAfter(deadline: .now() + duration) { NSApp.terminate(nil) }
        }
    }

    private func fixtureMenu() -> NSMenu {
        let menu = NSMenu(title: "任务雷达设置 · 隔离预览")
        menu.autoenablesItems = false
        func action(_ title: String) -> NSMenuItem {
            HoverPreviewAction(title) { [weak self] in
                self?.actions += 1; self?.log("action: " + title); self?.refreshStatus()
            }
        }
        menu.addItem(action("暂停监控"))
        menu.addItem(action("自动继续意外中断 ✓"))
        let disabled = NSMenuItem(title: "监督已暂停 · 禁用状态", action: nil, keyEquivalent: "")
        disabled.isEnabled = false; menu.addItem(disabled)
        menu.addItem(.separator())
        let parent = NSMenuItem(title: "背景透明度", action: nil, keyEquivalent: "")
        let child = NSMenu(title: "背景透明度"); child.autoenablesItems = false
        for value in [70, 75, 80] {
            let item = action("\(value)%")
            item.state = value == 75 ? .on : .off; child.addItem(item)
        }
        parent.submenu = child; menu.addItem(parent)
        let nestedParent = NSMenuItem(title: "已移除会话（2）", action: nil, keyEquivalent: "")
        let nested = NSMenu(title: "已移除会话"); nested.autoenablesItems = false
        nested.addItem(action("恢复 · Codex · 准确定位和长标题"))
        nested.addItem(action("恢复 · Gemini · 新会话"))
        nested.addItem(.separator()); nested.addItem(action("恢复全部会话"))
        nestedParent.submenu = nested; menu.addItem(nestedParent)
        menu.addItem(action("设置…"))
        menu.addItem(.separator())
        menu.addItem(action("退出任务雷达（仅记录预览操作）"))
        return menu
    }

    private func refreshStatus() {
        status.stringValue = "主线程计数：\(ticks)    菜单：\(menuOpen ? "已打开" : "已关闭")    操作回执：\(actions)"
    }

    /// Real AppKit dispatch with stationary hardware pointer: AX opens the
    /// production trigger, a menu-local mouse event opens a submenu, AX presses
    /// its action, then anchor-window keyboard events select and run an action.
    private func runAccessibilityChecks() {
        let initialPointer = NSEvent.mouseLocation
        let initialActions = actions
        func verify(_ condition: Bool, _ step: String) -> Bool {
            if !condition { log("selfcheck_failed: " + step); NSApp.terminate(nil) }
            return condition
        }
        _ = button.accessibilityPerformPress()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) {
            guard verify(self.menuOpen, "AX opening must survive departure grace"),
                  let row = self.menuRow(named: "背景透明度"), let menuWindow = row.window else { return }
            let point = row.convert(NSPoint(x: row.bounds.midX, y: row.bounds.midY), to: nil)
            let event = NSEvent.mouseEvent(with: .leftMouseDown, location: point, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                          windowNumber: menuWindow.windowNumber, context: nil, eventNumber: 1, clickCount: 1, pressure: 1)!
            NSApp.postEvent(event, atStart: false)
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
                guard verify(self.menuOpen, "menu-local mouse event must not dismiss using hardware coordinates"),
                      let child = self.menuRow(named: "75%") else {
                    _ = verify(false, "mouse event must open native submenu"); return
                }
                guard verify(child.accessibilityPerformPress(), "submenu AXPress must be available"),
                      verify(self.actions == initialActions + 1 && !self.menuOpen, "submenu action executes exactly once and dismisses") else { return }
                _ = self.button.accessibilityPerformPress()
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
                    self.postKey(125, to: self.window)
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
                        self.postKey(36, to: self.window)
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
                            guard verify(self.actions == initialActions + 2 && !self.menuOpen, "anchor-window Down/Enter runs one action") else { return }
                            _ = self.button.accessibilityPerformPress()
                            DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
                                self.postKey(53, to: self.window)
                                DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) {
                                    guard verify(!self.menuOpen && self.actions == initialActions + 2, "anchor-window Esc closes without action"),
                                          verify(NSEvent.mouseLocation == initialPointer, "AppKit/AX interaction does not need to move hardware pointer") else { return }
                                    self.log("accessibility_selfcheck_passed")
                                    NSApp.terminate(nil)
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    private func postKey(_ keyCode: UInt16, to window: NSWindow) {
        let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: ProcessInfo.processInfo.systemUptime,
                                    windowNumber: window.windowNumber, context: nil, characters: "", charactersIgnoringModifiers: "", isARepeat: false, keyCode: keyCode)!
        NSApp.postEvent(event, atStart: false)
    }

    private func menuRow(named label: String) -> NSView? {
        func find(_ view: NSView) -> NSView? {
            if view.accessibilityLabel() == label { return view }
            return view.subviews.lazy.compactMap(find).first
        }
        return NSApp.windows.filter { $0 !== window && $0.isVisible }.compactMap { $0.contentView }.lazy.compactMap(find).first
    }

    private func log(_ event: String) {
        let data = try! JSONSerialization.data(withJSONObject: ["event": event, "ticks": ticks, "actions": actions, "time": Date().timeIntervalSince1970], options: [.sortedKeys])
        if !FileManager.default.fileExists(atPath: receipt.path) { FileManager.default.createFile(atPath: receipt.path, contents: nil) }
        if let handle = try? FileHandle(forWritingTo: receipt) {
            defer { try? handle.close() }
            _ = try? handle.seekToEnd(); try? handle.write(contentsOf: data + Data([10]))
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ notification: Notification) { button?.closeMenu(); timer?.invalidate(); log("terminated") }
}

@main
struct HoverMenuPreview {
    static func main() {
        let application = NSApplication.shared
        let delegate = HoverPreviewDelegate()
        application.delegate = delegate
        application.run()
    }
}

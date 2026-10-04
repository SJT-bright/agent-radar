import AppKit
import SwiftUI

final class RadarPanel: NSPanel {
    var compactPresentation = false
    var leftControl = false
    private(set) var dragActive = false
    private var dragPointer: NSPoint?
    private var dragOrigin: NSPoint?
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
    /// mouseUp 可能因休眠/快速用户切换而丢失：悬停 tick 校验真实按键，
    /// 按键已释放即复位拖拽状态，避免悬停状态机永久卡死。
    func clearDragIfReleased() {
        if dragPointer != nil, NSEvent.pressedMouseButtons & 1 == 0 {
            dragPointer = nil
            dragOrigin = nil
            dragActive = false
        }
    }

    override func sendEvent(_ event: NSEvent) {
        if event.type == .leftMouseDown {
            let point = event.locationInWindow
            let withinHandle = compactPresentation ?
                (leftControl ? point.x >= RadarView.compactControlWidth : point.x < frame.width - RadarView.compactControlWidth) :
                (point.y >= frame.height - 40 && point.x >= (leftControl ? 50 : 0) && point.x < frame.width - (leftControl ? 40 : 76))
            if withinHandle {
                dragPointer = convertPoint(toScreen: event.locationInWindow)
                dragOrigin = frame.origin
                dragActive = true
                return
            }
        }
        if event.type == .leftMouseDragged, let start = dragPointer, let origin = dragOrigin {
            let pointer = convertPoint(toScreen: event.locationInWindow)
            setFrameOrigin(NSPoint(x: origin.x + pointer.x - start.x, y: origin.y + pointer.y - start.y))
            return
        }
        if event.type == .leftMouseUp, dragPointer != nil {
            dragPointer = nil
            dragOrigin = nil
            dragActive = false
            return
        }
        super.sendEvent(event)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private var panel: RadarPanel!
    private var statusItem: NSStatusItem!
    private let store = MonitorStore()
    private let recoveryToast = ContinuationToast()
    private var panelMotion: PanelMotion?
    private var motionTimer: Timer?
    private var motionAnchor = NSPoint.zero // control center X and panel top Y
    private var motionTimestamp = 0.0
    private var applyingMotion = false
    private var hoverTimer: Timer?
    private var hoverState = PanelHoverState()

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Reopening the .app should focus the existing instance, never create
        // competing samplers or multiple pets.
        if NSRunningApplication.runningApplications(withBundleIdentifier: "local.agentradar.desktop").count > 1 {
            for app in NSRunningApplication.runningApplications(withBundleIdentifier: "local.agentradar.desktop") where app.processIdentifier != getpid() {
                app.activate(options: [.activateIgnoringOtherApps])
            }
            NSApp.terminate(nil)
            return
        }
        NSApp.setActivationPolicy(.accessory)
        panel = RadarPanel(contentRect: NSRect(origin: .zero, size: store.panelSize),
                           styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        panel.title = "任务雷达"
        panel.identifier = NSUserInterfaceItemIdentifier("AgentRadarPanel")
        panel.level = .floating
        panel.isFloatingPanel = true
        panel.hidesOnDeactivate = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.appearance = NSAppearance(named: .darkAqua)
        panel.acceptsMouseMovedEvents = true
        panel.hasShadow = true
        panel.isMovableByWindowBackground = false
        panel.becomesKeyOnlyIfNeeded = false
        panel.delegate = self
        panel.compactPresentation = !store.expanded
        panel.leftControl = store.handleOnLeft
        panel.contentView = NSHostingView(rootView: RadarView(store: store))
        let screen = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let savedY = UserDefaults.standard.object(forKey: "panelY") as? Double
        // 胶囊与展开面板都贴屏幕最右缘：每次启动把右缘对齐可见区右缘。
        let origin = NSPoint(x: screen.maxX - store.panelSize.width,
                             y: savedY ?? screen.maxY - store.panelSize.height - 30)
        panel.setFrameOrigin(clamped(origin, size: panel.frame.size))
        store.onResize = { [weak self] expanded in self?.resize(expanded) }
        store.onRecoveryNotice = { [weak self] notice in
            guard let self = self else { return }
            self.recoveryToast.show(notice, anchor: self.panel.frame,
                                    cancel: { [weak self] in self?.store.cancelContinuation() },
                                    permission: { [weak self] in self?.store.requestPermission() },
                                    retry: { [weak self] sessionID, key in
                                        self?.store.retryContinuation(sessionID: sessionID, key: key)
                                    }, open: { [weak self] sessionID in
                                        self?.store.openNoticeSession(sessionID: sessionID) ?? false
                                    }, cancelRecovery: { [weak self] sessionID, key in
                                        self?.store.cancelContinuation(sessionID: sessionID, key: key)
                                    })
        }
        NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification,
                                               object: nil, queue: .main) { [weak self] _ in
            self?.reclampPanelForScreenChange()
        }
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = NSImage(systemSymbolName: "sparkle.viewfinder", accessibilityDescription: "任务雷达")
        statusItem.button?.toolTip = "任务雷达 · 显示悬浮窗"
        statusItem.button?.target = self
        statusItem.button?.action = #selector(showPanel)
        panel.orderFrontRegardless()
        hoverState.presentationChanged(expanded: store.expanded,
                                       pointerInside: panel.frame.contains(NSEvent.mouseLocation))
        let timer = Timer(timeInterval: 0.1, repeats: true) { [weak self] _ in self?.updatePanelHover() }
        hoverTimer = timer
        RunLoop.main.add(timer, forMode: .common)
        if ProcessInfo.processInfo.arguments.contains(PermissionRestartPlan.recheckArgument) {
            store.beginPermissionRepair()
        }
        store.start()
        if ProcessInfo.processInfo.arguments.contains(PermissionRestartPlan.recheckArgument) {
            DispatchQueue.main.async { [weak self] in self?.store.repairAccessibility() }
        }
        if ProcessInfo.processInfo.arguments.contains("--smoke-test") {
            DispatchQueue.main.asyncAfter(deadline: .now() + 12) { NSApp.terminate(nil) }
        }
    }

    func applicationDidBecomeActive(_ notification: Notification) {
        store.permissionWindowBecameActive()
    }

    @objc private func showPanel() {
        if !store.expanded { store.setExpanded(true) }
        panel.orderFrontRegardless()
    }
    /// 显示器拔插/改排列后面板可能滞留屏幕外：钳回可见区并取消进行中动画。
    private func reclampPanelForScreenChange() {
        panelMotion = nil
        motionTimer?.invalidate()
        motionTimer = nil
        panel.setFrameOrigin(clamped(panel.frame.origin, size: panel.frame.size))
    }

    private func updatePanelHover() {
        guard panel.isVisible else { return }
        panel.clearDragIfReleased()
        let pointerInside = panel.frame.contains(NSEvent.mouseLocation) || panel.dragActive || store.settingsMenuTracking
        if let shouldExpand = hoverState.update(expanded: store.expanded,
                                                pointerInside: pointerInside,
                                                now: ProcessInfo.processInfo.systemUptime) {
            store.setExpanded(shouldExpand)
        }
    }
    private func resize(_ expanded: Bool) {
        let old = panel.frame
        let wasCompact = panel.compactPresentation
        if wasCompact == expanded {
            hoverState.presentationChanged(expanded: expanded,
                                           pointerInside: old.contains(NSEvent.mouseLocation))
        }
        let anchorX = old.maxX   // 右缘锚定：展开面板与收起胶囊的右缘始终贴齐
        let visible = NSScreen.screens.map(\.visibleFrame).first(where: { $0.contains(NSPoint(x: anchorX, y: old.maxY - 20)) }) ?? NSScreen.main?.visibleFrame ?? old
        if expanded && wasCompact {
            // Grow into the space available around the same button. Near the
            // lower edge, shorten the scroll area instead of moving the button.
            store.expansionHeightLimit = min(548, max(112, old.maxY - visible.minY))
        }
        let size = store.panelSize
        panel.compactPresentation = !expanded
        panel.leftControl = store.handleOnLeft
        guard old.size != size || panelMotion != nil else { return }
        if panelMotion?.target == size { return }
        let x = anchorX - size.width
        let wanted = NSPoint(x: x, y: old.maxY - size.height)
        let origin = wasCompact != !expanded ? wanted : clamped(wanted, size: size)
        motionAnchor = NSPoint(x: origin.x + size.width, y: origin.y + size.height)
        if NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            motionTimer?.invalidate()
            motionTimer = nil
            panelMotion = nil
            applyMotion(size)
            savePosition()
            return
        }
        if var motion = panelMotion {
            motion.target = size
            panelMotion = motion
        } else {
            panelMotion = PanelMotion(size: old.size, target: size)
        }
        if motionTimer == nil {
            motionTimestamp = ProcessInfo.processInfo.systemUptime
            let timer = Timer(timeInterval: 1.0 / 60, repeats: true) { [weak self] _ in self?.advanceMotion() }
            motionTimer = timer
            RunLoop.main.add(timer, forMode: .common)
        }
    }

    private func advanceMotion() {
        guard var motion = panelMotion else { return }
        let now = ProcessInfo.processInfo.systemUptime
        let size = motion.advance(by: now - motionTimestamp)
        motionTimestamp = now
        panelMotion = motion
        if motion.settled {
            applyMotion(motion.target)
            motionTimer?.invalidate()
            motionTimer = nil
            panelMotion = nil
            savePosition()
        } else { applyMotion(size) }
    }

    private func applyMotion(_ size: NSSize) {
        let x = motionAnchor.x - size.width
        applyingMotion = true
        panel.setFrame(NSRect(x: x, y: motionAnchor.y - size.height, width: size.width, height: size.height),
                       display: false, animate: false)
        applyingMotion = false
    }
    private func clamped(_ origin: NSPoint, size: NSSize) -> NSPoint {
        let screens = NSScreen.screens.map(\.visibleFrame)
        let visible = screens.first(where: { $0.contains(origin) }) ?? NSScreen.main?.visibleFrame ?? .zero
        return NSPoint(x: max(visible.minX, min(origin.x, visible.maxX - size.width)),
                       y: max(visible.minY, min(origin.y, visible.maxY - size.height)))
    }
    func windowDidMove(_ notification: Notification) {
        guard panel != nil, !applyingMotion else { return }
        if panelMotion != nil {
            motionAnchor = NSPoint(x: panel.frame.maxX, y: panel.frame.maxY)
        }
        savePosition()
    }
    private func savePosition() {
        UserDefaults.standard.set(panel.frame.origin.x, forKey: "panelX")
        UserDefaults.standard.set(panel.frame.origin.y, forKey: "panelY")
    }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showPanel()
        return true
    }
    func applicationWillTerminate(_ notification: Notification) {
        hoverTimer?.invalidate()
        motionTimer?.invalidate()
        store.stop()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()

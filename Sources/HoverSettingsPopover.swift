import AppKit
import QuartzCore

/// NSMenu remains the fresh action model; no popUp/tracking loop is entered.
/// Each menu is an ordinary nonactivating panel, so collection and animations
/// continue on the normal run loop while it is visible.
final class HoverSettingsPopover: NSObject {
    var didClose: (() -> Void)?
    private weak var anchor: NSView?
    private var menus: [HoverMenuPanel] = []
    private var departure = SettingsMenuDepartureState()
    private var pointerTimer: Timer?
    private var localEvents: Any?
    private var globalEvents: Any?
    private var pendingSubmenu: DispatchWorkItem?
    private var reducedMotion = false
    private var activationObserver: NSObjectProtocol?

    var isOpen: Bool { !menus.isEmpty }

    func open(_ menu: NSMenu, from anchor: NSView, reduceMotion: Bool, keyboardInitiated: Bool = false) {
        guard !isOpen, anchor.window != nil, !menu.items.isEmpty else { return }
        self.anchor = anchor
        reducedMotion = reduceMotion || NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        departure = SettingsMenuDepartureState()
        if keyboardInitiated { departure.holdForKeyboard(pointer: NSEvent.mouseLocation) }
        let panel = makePanel(menu, depth: 0)
        let anchorRect = screenRect(of: anchor)
        let screen = screenFrame(at: anchorRect.center)
        let finalFrame = Self.rootFrame(size: panel.frame.size, anchor: anchorRect, screen: screen)
        menus.append(panel)
        show(panel, frame: finalFrame)
        // A nonactivating key panel handles arrows/Esc without activating Radar
        // or sending keystrokes to the AI application's composer.
        panel.makeKey()
        installObservers()
    }

    func close() {
        guard isOpen else { return }
        pendingSubmenu?.cancel(); pendingSubmenu = nil
        pointerTimer?.invalidate(); pointerTimer = nil
        if let localEvents { NSEvent.removeMonitor(localEvents) }
        if let globalEvents { NSEvent.removeMonitor(globalEvents) }
        localEvents = nil; globalEvents = nil
        if let activationObserver { NSWorkspace.shared.notificationCenter.removeObserver(activationObserver) }
        activationObserver = nil
        let closing = menus
        menus.removeAll()
        for panel in closing {
            panel.parent?.removeChildWindow(panel)
            if panel.isKeyWindow { panel.resignKey() }
            if reducedMotion {
                panel.orderOut(nil)
            } else {
                // Windows stop receiving input immediately; their fade cannot
                // keep settingsMenuTracking alive or block the next hover.
                panel.ignoresMouseEvents = true
                NSAnimationContext.runAnimationGroup({ context in
                    context.duration = 0.10
                    context.timingFunction = CAMediaTimingFunction(name: .easeOut)
                    panel.animator().alphaValue = 0
                }, completionHandler: { panel.orderOut(nil) })
            }
        }
        anchor = nil
        didClose?()
    }

    private func installObservers() {
        localEvents = NSEvent.addLocalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown, .otherMouseDown, .keyDown]) { [weak self] event in
            guard let self, self.isOpen else { return event }
            if event.type == .keyDown {
                let destination = event.window ?? NSApp.keyWindow
                if let destination, self.menus.contains(where: { $0 === destination }) || self.anchor?.window === destination {
                    return self.handleKey(event) ? nil : event
                }
                return event
            }
            // The dispatched event owns its coordinates. Accessibility/remote
            // input can target a menu without moving the hardware pointer.
            let point = event.window?.convertPoint(toScreen: event.locationInWindow) ?? NSEvent.mouseLocation
            if !self.containsPointer(point) { self.close() }
            return event
        }
        globalEvents = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown, .otherMouseDown]) { [weak self] _ in
            // A click delivered to another application dismisses immediately.
            self?.close()
        }
        activationObserver = NSWorkspace.shared.notificationCenter.addObserver(forName: NSWorkspace.didActivateApplicationNotification,
                                                                               object: nil, queue: .main) { [weak self] notification in
            guard let application = notification.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,
                  application.processIdentifier != ProcessInfo.processInfo.processIdentifier else { return }
            self?.close()
        }
        let timer = Timer(timeInterval: 0.04, repeats: true) { [weak self] _ in
            guard let self, self.isOpen else { return }
            guard let anchor = self.anchor, anchor.window?.isVisible == true, !anchor.isHiddenOrHasHiddenAncestor else {
                self.close(); return
            }
            let point = NSEvent.mouseLocation
            if self.departure.shouldClose(pointerInside: self.containsPointer(point), pointer: point,
                                          now: ProcessInfo.processInfo.systemUptime) { self.close() }
        }
        pointerTimer = timer
        RunLoop.main.add(timer, forMode: .common)
    }

    private func containsPointer(_ point: NSPoint) -> Bool {
        if let anchor, screenRect(of: anchor).insetBy(dx: -5, dy: -5).contains(point) { return true }
        return menus.contains { $0.frame.insetBy(dx: -5, dy: -5).contains(point) }
    }

    private func makePanel(_ menu: NSMenu, depth: Int) -> HoverMenuPanel {
        let screen = anchor.map { screenFrame(at: screenRect(of: $0).center) } ?? NSScreen.main?.visibleFrame ?? .zero
        let panel = HoverMenuPanel(menu: menu, maximumSize: NSSize(width: max(180, screen.width - 16), height: max(80, screen.height - 16)))
        panel.level = anchor?.window?.level ?? .floating
        panel.menuView.hovered = { [weak self, weak panel] index in
            guard let self, let panel, self.isOpen else { return }
            self.departure.resumePointer()
            self.pendingSubmenu?.cancel()
            let work = DispatchWorkItem { [weak self, weak panel] in
                guard let self, let panel, self.menus.indices.contains(depth), self.menus[depth] === panel,
                      panel.menuView.selectedIndex == index else { return }
                self.openSubmenu(at: index, depth: depth)
            }
            self.pendingSubmenu = work
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.10, execute: work)
        }
        panel.menuView.pressed = { [weak self] index in self?.activate(index: index, depth: depth) }
        panel.onKeyDown = { [weak self] event in _ = self?.handleKey(event) }
        return panel
    }

    private func show(_ panel: HoverMenuPanel, frame: NSRect) {
        let initial = reducedMotion ? frame : frame.offsetBy(dx: 0, dy: -5)
        panel.setFrame(initial, display: true)
        panel.alphaValue = reducedMotion ? 1 : 0
        anchor?.window?.addChildWindow(panel, ordered: .above)
        panel.orderFrontRegardless()
        guard !reducedMotion else { return }
        NSAnimationContext.runAnimationGroup { context in
            context.duration = 0.14
            context.timingFunction = CAMediaTimingFunction(name: .easeOut)
            panel.animator().alphaValue = 1
            panel.animator().setFrame(frame, display: true)
        }
    }

    private func removeMenus(after depth: Int) {
        guard menus.count > depth + 1 else { return }
        let closing = Array(menus.dropFirst(depth + 1))
        menus.removeSubrange((depth + 1)..<menus.count)
        for panel in closing { panel.parent?.removeChildWindow(panel); panel.orderOut(nil) }
    }

    private func openSubmenu(at index: Int, depth: Int) {
        guard menus.indices.contains(depth), menus[depth].menuView.menuModel.items.indices.contains(index) else { return }
        let parent = menus[depth]
        let item = parent.menuView.menuModel.items[index]
        if menus.count > depth + 1, menus[depth + 1].menuView.menuModel === item.submenu { return }
        removeMenus(after: depth)
        guard item.isEnabled, let submenu = item.submenu, !submenu.items.isEmpty else { return }
        let panel = makePanel(submenu, depth: depth + 1)
        let rowRect = parent.menuView.screenRectForRow(index)
        let screen = screenFrame(at: rowRect.center)
        let frame = Self.submenuFrame(size: panel.frame.size, row: rowRect, parent: parent.frame, screen: screen)
        menus.append(panel)
        show(panel, frame: frame)
    }

    private func activate(index: Int, depth: Int) {
        guard menus.indices.contains(depth), menus[depth].menuView.menuModel.items.indices.contains(index) else { return }
        let item = menus[depth].menuView.menuModel.items[index]
        guard item.isEnabled, !item.isSeparatorItem else { return }
        if item.submenu != nil { openSubmenu(at: index, depth: depth); return }
        let action = item.action, target = item.target
        // Keep the model alive until invocation, even after all panels close.
        close()
        if let action { NSApp.sendAction(action, to: target, from: item) }
    }

    @discardableResult private func handleKey(_ event: NSEvent) -> Bool {
        guard isOpen else { return false }
        let depth = menus.count - 1
        let view = menus[depth].menuView
        if [125, 126, 124, 123, 36, 49, 76].contains(event.keyCode) {
            departure.holdForKeyboard(pointer: NSEvent.mouseLocation)
        }
        switch event.keyCode {
        case 53: close()
        case 125, 126:
            pendingSubmenu?.cancel()
            view.selectNext(direction: event.keyCode == 125 ? 1 : -1)
        case 124:
            if let index = view.selectedIndex {
                openSubmenu(at: index, depth: depth)
                if menus.count > depth + 1 { menus.last?.menuView.selectNext(direction: 1) }
            }
        case 123:
            if depth > 0 { removeMenus(after: depth - 1) }
        case 36, 49, 76:
            if let index = view.selectedIndex { activate(index: index, depth: depth) }
        default: return false
        }
        return true
    }

    static func rootFrame(size: NSSize, anchor: NSRect, screen: NSRect) -> NSRect {
        let bounds = screen.insetBy(dx: 8, dy: 8)
        var x = anchor.maxX - size.width
        var y = anchor.minY - size.height - 4
        if y < bounds.minY {
            if anchor.maxY + size.height + 4 <= bounds.maxY {
                y = anchor.maxY + 4
            } else {
                // Tall menus go beside the button instead of covering it.
                x = anchor.minX - size.width - 4
                if x < bounds.minX { x = anchor.maxX + 4 }
                y = anchor.maxY - size.height
            }
        }
        return clamp(NSRect(origin: NSPoint(x: x, y: y), size: size), to: bounds)
    }

    static func submenuFrame(size: NSSize, row: NSRect, parent: NSRect, screen: NSRect) -> NSRect {
        let bounds = screen.insetBy(dx: 8, dy: 8)
        var x = parent.maxX + 4
        if x + size.width > bounds.maxX { x = parent.minX - size.width - 4 }
        return clamp(NSRect(x: x, y: row.maxY + 5 - size.height, width: size.width, height: size.height), to: bounds)
    }

    private static func clamp(_ frame: NSRect, to bounds: NSRect) -> NSRect {
        NSRect(x: min(max(frame.minX, bounds.minX), max(bounds.minX, bounds.maxX - frame.width)),
               y: min(max(frame.minY, bounds.minY), max(bounds.minY, bounds.maxY - frame.height)),
               width: frame.width, height: frame.height)
    }

    private func screenFrame(at point: NSPoint) -> NSRect {
        (NSScreen.screens.first { $0.frame.contains(point) } ?? anchor?.window?.screen ?? NSScreen.main)?.visibleFrame ?? .zero
    }

    private func screenRect(of view: NSView) -> NSRect {
        view.window?.convertToScreen(view.convert(view.bounds, to: nil)) ?? .zero
    }

    deinit {
        pointerTimer?.invalidate()
        if let localEvents { NSEvent.removeMonitor(localEvents) }
        if let globalEvents { NSEvent.removeMonitor(globalEvents) }
        if let activationObserver { NSWorkspace.shared.notificationCenter.removeObserver(activationObserver) }
        for panel in menus { panel.parent?.removeChildWindow(panel); panel.orderOut(nil) }
    }
}

private extension NSRect {
    var center: NSPoint { NSPoint(x: midX, y: midY) }
}

private final class HoverMenuPanel: NSPanel {
    let menuView: HoverMenuView
    var onKeyDown: ((NSEvent) -> Void)?
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    init(menu: NSMenu, maximumSize: NSSize) {
        menuView = HoverMenuView(menu: menu, maximumWidth: maximumSize.width)
        let size = NSSize(width: menuView.frame.width, height: min(menuView.frame.height + 10, maximumSize.height))
        super.init(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        isFloatingPanel = true; hidesOnDeactivate = false; becomesKeyOnlyIfNeeded = false
        isOpaque = false; backgroundColor = .clear; hasShadow = true
        animationBehavior = .none
        appearance = NSAppearance(named: .darkAqua)
        let backdrop = NSVisualEffectView(frame: NSRect(origin: .zero, size: size))
        backdrop.material = .popover; backdrop.blendingMode = .behindWindow; backdrop.state = .active
        backdrop.wantsLayer = true
        backdrop.layer?.cornerRadius = 12
        backdrop.layer?.masksToBounds = true
        backdrop.layer?.borderWidth = 0.7
        backdrop.layer?.borderColor = NSColor.white.withAlphaComponent(0.3).cgColor
        let scroll = NSScrollView(frame: backdrop.bounds.insetBy(dx: 0, dy: 5))
        scroll.drawsBackground = false; scroll.borderType = .noBorder
        scroll.hasVerticalScroller = menuView.frame.height > size.height - 10
        scroll.autohidesScrollers = true
        scroll.documentView = menuView
        backdrop.addSubview(scroll)
        contentView = backdrop
        setAccessibilityLabel(menu.title)
    }

    override func keyDown(with event: NSEvent) { onKeyDown?(event) }
}

private final class HoverMenuView: NSView {
    let menuModel: NSMenu
    var hovered: ((Int) -> Void)?
    var pressed: ((Int) -> Void)?
    private(set) var selectedIndex: Int?
    private var rows: [HoverMenuRow] = []
    override var isFlipped: Bool { true }

    init(menu: NSMenu, maximumWidth: CGFloat) {
        self.menuModel = menu
        let font = NSFont.systemFont(ofSize: 13)
        let titleWidth = menu.items.filter { !$0.isSeparatorItem }.map {
            ($0.title as NSString).size(withAttributes: [.font: font]).width
        }.max() ?? 160
        let width = min(maximumWidth, min(380, max(220, ceil(titleWidth) + 56)))
        let height = menu.items.reduce(CGFloat(0)) { $0 + ($1.isSeparatorItem ? 11 : 28) }
        super.init(frame: NSRect(x: 0, y: 0, width: width, height: height))
        setAccessibilityElement(true)
        setAccessibilityRole(.menu)
        setAccessibilityLabel(menu.title)
        var y: CGFloat = 0
        for (index, item) in menu.items.enumerated() {
            let row = HoverMenuRow(item: item, frame: NSRect(x: 5, y: y, width: width - 10, height: item.isSeparatorItem ? 11 : 28))
            row.entered = { [weak self] in
                guard let self else { return }
                self.select(index)
                self.hovered?(index)
            }
            row.pressed = { [weak self] in self?.pressed?(index) }
            rows.append(row); addSubview(row)
            y += row.frame.height
        }
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    private func select(_ index: Int) {
        if let previous = selectedIndex, rows.indices.contains(previous) { rows[previous].highlighted = false }
        selectedIndex = index
        rows[index].highlighted = menuModel.items[index].isEnabled && !menuModel.items[index].isSeparatorItem
    }

    func selectNext(direction: Int) {
        let selectable = menuModel.items.indices.filter { menuModel.items[$0].isEnabled && !menuModel.items[$0].isSeparatorItem }
        guard !selectable.isEmpty else { return }
        let position = selectedIndex.flatMap { selectable.firstIndex(of: $0) }
        let next = position.map { ($0 + direction + selectable.count) % selectable.count } ?? (direction > 0 ? 0 : selectable.count - 1)
        select(selectable[next])
        scrollToVisible(rows[selectable[next]].frame)
    }

    func screenRectForRow(_ index: Int) -> NSRect {
        window?.convertToScreen(convert(rows[index].frame, to: nil)) ?? .zero
    }
}

private final class HoverMenuRow: NSView {
    let item: NSMenuItem
    var entered: (() -> Void)?
    var pressed: (() -> Void)?
    var highlighted = false { didSet { needsDisplay = true } }
    private var hoverArea: NSTrackingArea?
    override var isFlipped: Bool { true }

    init(item: NSMenuItem, frame: NSRect) {
        self.item = item
        super.init(frame: frame)
        if !item.isSeparatorItem {
            setAccessibilityElement(true)
            setAccessibilityRole(.menuItem)
            setAccessibilityLabel(item.title)
            setAccessibilityEnabled(item.isEnabled)
            if item.state == .on { setAccessibilityValue("已选择") }
        }
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func updateTrackingAreas() {
        if let hoverArea { removeTrackingArea(hoverArea) }
        let area = NSTrackingArea(rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self, userInfo: nil)
        addTrackingArea(area); hoverArea = area
        super.updateTrackingAreas()
    }

    override func mouseEntered(with event: NSEvent) { entered?() }
    override func mouseDown(with event: NSEvent) { if item.isEnabled { pressed?() } }
    override func accessibilityPerformPress() -> Bool {
        guard item.isEnabled else { return false }
        pressed?(); return true
    }

    override func draw(_ dirtyRect: NSRect) {
        if item.isSeparatorItem {
            NSColor.white.withAlphaComponent(0.15).setFill()
            NSRect(x: 8, y: 5, width: bounds.width - 16, height: 0.6).fill()
            return
        }
        if highlighted {
            NSColor.controlAccentColor.withAlphaComponent(0.9).setFill()
            NSBezierPath(roundedRect: bounds.insetBy(dx: 0, dy: 1), xRadius: 5, yRadius: 5).fill()
        }
        let paragraph = NSMutableParagraphStyle(); paragraph.lineBreakMode = .byTruncatingTail
        var color = item.isEnabled ? NSColor.white : NSColor.white.withAlphaComponent(0.42)
        if !highlighted, let attributed = item.attributedTitle, attributed.length > 0,
           let titleColor = attributed.attribute(.foregroundColor, at: 0, effectiveRange: nil) as? NSColor { color = titleColor }
        let attributes: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: 13), .foregroundColor: color, .paragraphStyle: paragraph]
        (item.title as NSString).draw(in: NSRect(x: 23, y: 5, width: bounds.width - 46, height: 19), withAttributes: attributes)
        if item.state != .off {
            let mark = item.state == .on ? "✓" : "−"
            (mark as NSString).draw(at: NSPoint(x: 6, y: 5), withAttributes: attributes)
        }
        if item.submenu != nil {
            ("›" as NSString).draw(at: NSPoint(x: bounds.maxX - 17, y: 2),
                                  withAttributes: [.font: NSFont.systemFont(ofSize: 21), .foregroundColor: color])
        }
    }
}

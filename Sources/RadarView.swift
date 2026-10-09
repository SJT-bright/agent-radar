import SwiftUI
import AppKit

/// Adjustable translucent frost; text and app icons remain crisp.
struct RadarView: View {
    @ObservedObject var store: MonitorStore
    @State private var queryHovered = false
    @State private var linkHovered = false
    @State private var undoHovered = false
    @State private var repairHovered = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var hoverSpring: Animation? { reduceMotion ? nil : .spring(response: 0.22, dampingFraction: 1) }
    private var glassOpacity: Double { 1 - store.transparency }

    var body: some View {
        GeometryReader { geometry in
            let alignment: Alignment = store.handleOnLeft ? .topLeading : .topTrailing
            let revealProgress = max(0, min(1, (geometry.size.width - 60) / 178))
            ZStack(alignment: alignment) {
                conversationList
                    .frame(width: 238, height: geometry.size.height)
                    .opacity(max(0, (revealProgress - 0.15) / 0.85))
                    .allowsHitTesting(store.expanded && revealProgress > 0.95)
                    .accessibilityHidden(revealProgress < 0.95)
                rail.frame(width: 60, height: 32)
                    .opacity(max(0, 1 - revealProgress * 3))
                    .allowsHitTesting(!store.expanded && revealProgress < 0.05)
                    .accessibilityHidden(revealProgress > 0.05)
            }
            .frame(width: geometry.size.width, height: geometry.size.height, alignment: alignment)
        }
        .background {
            // 苹果式深色玻璃：磨砂以高基线混入、不被透明度设置稀释；
            // 恒定深色底 + 顶部微光/底部渐暗提供玻璃厚度与层次。
            ZStack {
                FrostedBackdrop(strength: glassOpacity + store.frostLevel.materialShare * (1 - glassOpacity))
                Color.black.opacity(glassOpacity * (1 - store.frostLevel.materialShare) + 0.12)
                LinearGradient(colors: [.white.opacity(0.055), .clear, .black.opacity(0.12)],
                               startPoint: .top, endPoint: .bottom)
            }.allowsHitTesting(false)
        }
        .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 18, style: .continuous)
            .strokeBorder(.white.opacity(0.42), lineWidth: 0.7).allowsHitTesting(false))
        .overlay(RoundedRectangle(cornerRadius: 17.2, style: .continuous)
            .strokeBorder(.white.opacity(0.08), lineWidth: 0.6).allowsHitTesting(false))
        // A single persistent control, outside either presentation, preserves
        // its hover state and screen anchor through expansion/collapse.
        // 收起胶囊的箭头必须避开拖拽把手：默认（把手在左）箭头贴右缘，
        // handleOnLeft 时镜像到左缘，否则点击多数变成拖动。
        .overlay(alignment: store.expanded ? .topTrailing : (store.handleOnLeft ? .topLeading : .topTrailing)) {
            HoverControl(symbol: store.expanded ? "chevron.right" : (store.handleOnLeft ? "chevron.left" : "chevron.right"),
                         label: store.expanded ? "折叠浮窗" : "展开全部应用") { store.toggleExpanded() }
                .padding(.trailing, store.expanded ? 7 : 0)
                .padding(.top, 2)
        }
        .foregroundStyle(.white)
        .preferredColorScheme(.dark)
    }

    private var rail: some View {
        // 数字和控制各占胶囊的一半；镜像时同时交换两者的位置。
        // 不再按箭头字形宽度留白，避免按钮换边后覆盖数字。
        Text(store.compactCountLabel)
            .font(.system(size: 13, weight: .semibold, design: .rounded))
            .monospacedDigit()
            .lineLimit(1).minimumScaleFactor(0.7)
            .frame(width: Self.compactControlWidth, height: 32)
            .frame(maxWidth: .infinity, maxHeight: .infinity,
                   alignment: store.handleOnLeft ? .trailing : .leading)
            .contentShape(Rectangle())
            .contextMenu { controls }
            .help("拖动浮条")
            .overlay(alignment: .topTrailing) {
                if !store.workspaceConflicts.isEmpty {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 7)).foregroundStyle(.orange)
                        .padding(.trailing, store.handleOnLeft ? 2 : 32).padding(.top, 2)
                        .help("\(store.workspaceConflicts.count) 个工作文件夹存在多软件同时工作；展开查看感叹号")
                        .allowsHitTesting(false)
                }
            }
            .shadow(color: .black.opacity(0.65), radius: 1.5, y: 1)
    }

    static let compactControlWidth: CGFloat = 30

    private var conversationList: some View {
        VStack(spacing: 0) {
            HStack(spacing: 7) {
                if store.handleOnLeft { Color.clear.frame(width: 30, height: 30).allowsHitTesting(false) }
                Image(systemName: "sparkles").font(.system(size: 12))
                Text("任务雷达").font(.system(size: 12, weight: .semibold))
                if !store.workspaceConflicts.isEmpty {
                    Button { store.showWorkspaceConflicts() } label: {
                        Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                    }.buttonStyle(RadarButtonStyle())
                        .help("\(store.workspaceConflicts.count) 个工作文件夹存在多软件同时工作；点击查看")
                        .accessibilityLabel("同目录工作提醒，\(store.workspaceConflicts.count) 个文件夹")
                }
                Spacer(minLength: 0)
                HoverSettingsMenu(store: store)
                    .frame(width: 30, height: 26)
                    .help("悬停或点击打开设置")
                if !store.handleOnLeft { Color.clear.frame(width: 30, height: 30).allowsHitTesting(false) }
            }.padding(.horizontal, 12).frame(height: 40)
                .shadow(color: .black.opacity(0.65), radius: 1.5, y: 1)

            ScrollView {
                LazyVStack(spacing: 4) {
                    if store.displaySessions.isEmpty {
                        Text("暂无可显示的对话")
                            .font(.system(size: 11)).frame(height: 72)
                    }
                    ForEach(store.conversationGroups) { group in
                        ConversationGroupView(group: group, store: store)
                    }
                }.padding(.horizontal, 8).padding(.vertical, 4)
            }.scrollIndicators(.hidden)

            HStack(spacing: 5) {
                Circle().fill(.white)
                    .frame(width: 4, height: 4)
                Text(store.paused ? "监控已暂停" : "\(store.conversationGroups.count) 个应用 · \(store.conversationGroups.reduce(0) { $0 + $1.sessions.count }) 条会话")
                Spacer(minLength: 2)
            }.font(.system(size: 9.5)).foregroundStyle(.white)
                .padding(.horizontal, 13).frame(height: 28)
                .shadow(color: .black.opacity(0.65), radius: 1.5, y: 1)
            accessibilityQuery
            if let message = store.actionMessage {
                HStack(spacing: 5) {
                    Text(message).lineLimit(1).help(message)
                    Spacer(minLength: 0)
                    if store.undoRemovalKey != nil {
                        Button("撤销") { store.undoRemoval() }
                            .buttonStyle(RadarButtonStyle()).fontWeight(.semibold)
                            .foregroundStyle(.white.opacity(undoHovered ? 1 : 0.85))
                            .padding(.horizontal, 7).frame(height: 20)
                            .background(Capsule().fill(.white.opacity(undoHovered ? 0.34 : 0.18)))
                            .overlay(Capsule().strokeBorder(.white.opacity(undoHovered ? 0.4 : 0), lineWidth: 0.6))
                            .scaleEffect(undoHovered ? 1.06 : 1)
                            .onHover { undoHovered = $0 }
                            .animation(hoverSpring, value: undoHovered)
                            .accessibilityLabel("撤销移除会话")
                    }
                }.font(.system(size: 9)).padding(.horizontal, 10).frame(height: 24)
            }
        }
    }

    private var queryColor: Color {
        switch store.accessibilityCheck {
        case .passed: return Color(red: 0.28, green: 0.94, blue: 0.57)
        case .failed: return Color(red: 1, green: 0.38, blue: 0.40)
        case .idle, .checking: return .white.opacity(0.75)
        }
    }

    // 运行模式状态色：狂暴红、协作蓝，与恢复提醒里的中断红区分开。
    static let rageModeColor = Color(red: 1, green: 0.36, blue: 0.34)
    static let collaborationModeColor = Color(red: 0.38, green: 0.68, blue: 1)

    private var accessibilityQuery: some View {
        HStack(spacing: 5) {
            Image(systemName: store.accessibilityCheck == .passed ? "checkmark.circle.fill" :
                    store.accessibilityCheck == .failed ? "xmark.circle.fill" : "circle.dotted")
                .font(.system(size: 10)).foregroundStyle(queryColor)
                .accessibilityHidden(true)
            Text("辅助功能").foregroundStyle(.white)
            Text(store.accessibilityCheckLabel).foregroundStyle(queryColor)
                .accessibilityLabel("辅助功能" + store.accessibilityCheckLabel)
            Spacer(minLength: 0)
            if store.accessibilityCheck == .failed {
                Button { store.repairAccessibility() } label: {
                    Text("修复").fontWeight(.medium)
                        .foregroundStyle(Color(red: 1, green: 0.38, blue: 0.40))
                        .padding(.horizontal, 7).frame(height: 22)
                        .background(Capsule().fill(Color(red: 1, green: 0.38, blue: 0.40)
                            .opacity(repairHovered ? 0.30 : 0.14)))
                        .overlay(Capsule().strokeBorder(Color(red: 1, green: 0.38, blue: 0.40)
                            .opacity(0.55), lineWidth: 0.6))
                        .scaleEffect(repairHovered ? 1.05 : 1)
                        .contentShape(Capsule())
                }.buttonStyle(RadarButtonStyle())
                    .onHover { repairHovered = $0 }
                    .animation(hoverSpring, value: repairHovered)
                    .help("打开自助修复窗口，分别查询窗口与输入组件，并按步骤重新绑定当前版本")
                    .accessibilityLabel("修复辅助功能授权")
            }
            Button { store.verifyAccessibility() } label: {
                Text(store.accessibilityCheck == .checking ? "查询中" : "查询")
                    .fontWeight(.medium).foregroundStyle(.white)
                    .padding(.horizontal, 7).frame(height: 22)
                    .background(Capsule().fill(queryColor.opacity(queryHovered ? 0.46 : 0.24)))
                    .overlay(Capsule().strokeBorder(queryColor.opacity(0.55), lineWidth: 0.6))
                    .shadow(color: queryColor.opacity(queryHovered ? 0.35 : 0), radius: queryHovered ? 4 : 0)
                    .scaleEffect(queryHovered ? 1.05 : 1)
                    .contentShape(Capsule())
            }.buttonStyle(RadarButtonStyle())
                .onHover { queryHovered = $0 }
                .animation(hoverSpring, value: queryHovered)
                .disabled(store.accessibilityCheck == .checking)
                .accessibilityLabel("查询辅助功能")
            Button { store.requestPermission() } label: {
                Image(systemName: "arrow.up.forward.square").font(.system(size: 12))
                    .foregroundStyle(.white.opacity(linkHovered ? 1 : 0.72))
                    .scaleEffect(linkHovered ? 1.15 : 1)
                    .frame(width: 22, height: 24).contentShape(Rectangle())
            }.buttonStyle(RadarButtonStyle())
                .onHover { linkHovered = $0 }
                .animation(hoverSpring, value: linkHovered)
                .help("前往系统设置 → 隐私与安全性 → 辅助功能")
                .accessibilityLabel("打开辅助功能系统设置")
        }.font(.system(size: 9)).padding(.horizontal, 12).frame(height: 30)
            .background(.white.opacity(0.04))
            .help(store.accessibilityCheckDetail)
            .animation(.easeInOut(duration: 0.18), value: store.accessibilityCheck)
    }

    @ViewBuilder private var controls: some View {
        Button(store.paused ? "继续监控" : "暂停监控") { store.togglePause() }
        Button(store.autoContinueEnabled ? "自动继续意外中断 ✓" : "开启自动继续意外中断") { store.toggleAutoContinue() }
        Button("新消息自动插队" + (store.autoQueueInsertionEnabled ? " ✓" : "")) { store.toggleAutoQueueInsertion() }
        Button("查询 AI 监督状态") { store.showContinuationDiagnostics() }
        Text(store.promptRules.completionMode == "rage" ? "当前：狂暴模式" : "当前：协作模式")
            .foregroundColor(store.promptRules.completionMode == "rage" ? Self.rageModeColor : Self.collaborationModeColor)
        Button("设置…") { store.showPromptSettings() }
        Button("预览中断提醒") { store.previewContinuationNotice() }
        Button("夜间常亮（防休眠）" + (store.keepAwakeEnabled ? " ✓" : "")) { store.toggleKeepAwake() }
            .help(store.keepAwakeStatus.detail)
        if store.keepAwakeEnabled && !store.keepAwakeStatus.fullyActive { Text(store.keepAwakeStatus.summary) }
        Text(store.recoverySummary)
        Divider()
        Menu("已移除会话（\(store.removedSessions.count)）") {
            if store.removedSessions.isEmpty {
                Text("暂无已移除会话")
            } else {
                ForEach(store.removedSessions) { removed in
                    Button("恢复 · \(removed.appName) · \(String(removed.title.prefix(48)))") {
                        store.restoreSession(removed.id)
                    }
                }
                Divider()
                Button("恢复全部会话") { store.restoreAllSessions() }
            }
        }
        Menu("背景透明度") {
            ForEach([0.70, 0.75, 0.80], id: \.self) { value in
                Button("\(Int(value * 100))%" + (store.transparency == value ? " ✓" : "")) {
                    store.transparency = value
                }
            }
        }
        Menu("磨砂质感") {
            ForEach(FrostLevel.allCases, id: \.rawValue) { level in
                Button(level.label + (store.frostLevel == level ? " ✓" : "")) {
                    store.frostLevel = level
                }
            }
        }
        Button("后台读取情况…") { store.showBackgroundDiagnostics() }
        Button("添加其他 AI 应用…") { store.addApplication() }
        Button("打开辅助功能系统设置") { store.requestPermission() }
        Button("查询辅助功能") { store.verifyAccessibility() }
        Button("辅助功能自助修复…") { store.repairAccessibility() }
        Button(store.loginEnabled ? "关闭登录时启动" : "登录时启动") { store.toggleLogin() }
        if !store.errors.isEmpty {
            Divider()
            ForEach(store.errors, id: \.self) { Text($0) }
        }
        Divider()
        Button("退出任务雷达") { NSApp.terminate(nil) }
    }
}

/// Rebuild the action model on opening; asynchronous panels render the menu
/// without NSMenu's modal pointer tracking or a blocked hover-close path.
private struct HoverSettingsMenu: NSViewRepresentable {
    let store: MonitorStore
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    func makeNSView(context: Context) -> HoverSettingsButton {
        let button = HoverSettingsButton()
        configure(button)
        return button
    }

    func updateNSView(_ button: HoverSettingsButton, context: Context) { configure(button) }

    static func dismantleNSView(_ button: HoverSettingsButton, coordinator: ()) { button.closeMenu() }

    private func configure(_ button: HoverSettingsButton) {
        button.isEnabled = store.expanded
        button.reduceMotion = reduceMotion
        button.image = NSImage(systemSymbolName: store.errors.isEmpty ? "ellipsis" : "exclamationmark.circle",
                               accessibilityDescription: "设置")?
            .withSymbolConfiguration(.init(pointSize: 12, weight: .regular))
        button.makeMenu = { [weak store] in
            guard let store else { return NSMenu() }
            return Self.menu(for: store)
        }
        button.trackingChanged = { [weak store] in store?.settingsMenuTracking = $0 }
    }

    private static func menu(for store: MonitorStore) -> NSMenu {
        let menu = NSMenu(title: "任务雷达设置")
        menu.appearance = NSAppearance(named: .darkAqua)
        menu.autoenablesItems = false
        func action(_ title: String, _ perform: @escaping () -> Void) {
            menu.addItem(SettingsActionItem(title, perform: perform))
        }
        func information(_ title: String, color: NSColor? = nil) {
            let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            item.isEnabled = false
            if let color {
                item.attributedTitle = NSAttributedString(string: title, attributes: [.foregroundColor: color])
            }
            menu.addItem(item)
        }
        func submenu(_ title: String) -> NSMenu {
            let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            let child = NSMenu(title: title)
            child.autoenablesItems = false
            item.submenu = child
            menu.addItem(item)
            return child
        }
        action(store.paused ? "继续监控" : "暂停监控") { store.togglePause() }
        action(store.autoContinueEnabled ? "自动继续意外中断 ✓" : "开启自动继续意外中断") { store.toggleAutoContinue() }
        action("新消息自动插队" + (store.autoQueueInsertionEnabled ? " ✓" : "")) { store.toggleAutoQueueInsertion() }
        action("查询 AI 监督状态") { store.showContinuationDiagnostics() }
        information(store.promptRules.completionMode == "rage" ? "当前：狂暴模式" : "当前：协作模式",
                    color: NSColor(store.promptRules.completionMode == "rage" ? RadarView.rageModeColor : RadarView.collaborationModeColor))
        action("设置…") { store.showPromptSettings() }
        action("预览中断提醒") { store.previewContinuationNotice() }
        action("夜间常亮（防休眠）" + (store.keepAwakeEnabled ? " ✓" : "")) { store.toggleKeepAwake() }
        if store.keepAwakeEnabled && !store.keepAwakeStatus.fullyActive { information(store.keepAwakeStatus.summary) }
        information(store.recoverySummary)
        menu.addItem(.separator())
        let removed = submenu("已移除会话（\(store.removedSessions.count)）")
        if store.removedSessions.isEmpty {
            let item = NSMenuItem(title: "暂无已移除会话", action: nil, keyEquivalent: "")
            item.isEnabled = false
            removed.addItem(item)
        } else {
            for row in store.removedSessions {
                removed.addItem(SettingsActionItem("恢复 · \(row.appName) · \(String(row.title.prefix(48)))") {
                    store.restoreSession(row.id)
                })
            }
            removed.addItem(.separator())
            removed.addItem(SettingsActionItem("恢复全部会话") { store.restoreAllSessions() })
        }
        let transparency = submenu("背景透明度")
        for value in [0.70, 0.75, 0.80] {
            let item = SettingsActionItem("\(Int(value * 100))%") { store.transparency = value }
            item.state = store.transparency == value ? .on : .off
            transparency.addItem(item)
        }
        let frost = submenu("磨砂质感")
        for level in FrostLevel.allCases {
            let item = SettingsActionItem(level.label) { store.frostLevel = level }
            item.state = store.frostLevel == level ? .on : .off
            frost.addItem(item)
        }
        action("后台读取情况…") { store.showBackgroundDiagnostics() }
        action("添加其他 AI 应用…") { store.addApplication() }
        action("打开辅助功能系统设置") { store.requestPermission() }
        action("查询辅助功能") { store.verifyAccessibility() }
        action("辅助功能自助修复…") { store.repairAccessibility() }
        action(store.loginEnabled ? "关闭登录时启动" : "登录时启动") { store.toggleLogin() }
        if !store.errors.isEmpty {
            menu.addItem(.separator())
            for error in store.errors { information(error) }
        }
        menu.addItem(.separator())
        action("退出任务雷达") { NSApp.terminate(nil) }
        return menu
    }
}

private final class SettingsActionItem: NSMenuItem {
    private let perform: () -> Void

    init(_ title: String, perform: @escaping () -> Void) {
        self.perform = perform
        super.init(title: title, action: #selector(invoke), keyEquivalent: "")
        target = self
    }

    required init(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    @objc private func invoke() { perform() }
}

final class HoverSettingsButton: NSButton {
    var makeMenu: (() -> NSMenu)?
    var trackingChanged: ((Bool) -> Void)?
    var reduceMotion = false {
        didSet {
            if oldValue != reduceMotion { updateAppearance() }
        }
    }
    override var isEnabled: Bool {
        didSet {
            if !isEnabled {
                openGeneration += 1
                pendingOpen?.cancel()
                pendingOpen = nil
                popover.close()
            }
            updateAppearance()
            window?.invalidateCursorRects(for: self)
        }
    }
    private var hoverArea: NSTrackingArea?
    private var pendingOpen: DispatchWorkItem?
    private var openGeneration = 0
    private var hoverState = SettingsMenuHoverState()
    private var pointerHovered = false
    private let popover = HoverSettingsPopover()
    private var hoverProgress: CGFloat = 0
    private var hoverTarget: CGFloat = 0
    private var appearanceTimer: Timer?
    private var previousAppearanceTick: TimeInterval = 0

    init() {
        super.init(frame: .zero)
        isBordered = false
        imagePosition = .imageOnly
        imageScaling = .scaleProportionallyDown
        bezelStyle = .regularSquare
        contentTintColor = .white.withAlphaComponent(0.78)
        wantsLayer = true
        target = self
        action = #selector(openFromClick)
        setAccessibilityLabel("设置")
        setAccessibilityHelp("悬停或点击打开任务雷达设置")
        setAccessibilityRole(.menuButton)
        popover.didClose = { [weak self] in self?.finishTracking() }
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    // AppKit controls can rebuild their backing layers. Draw the affordance in
    // the control itself so it cannot lose its size or sit behind that rebuild.
    override var wantsUpdateLayer: Bool { false }

    override func draw(_ dirtyRect: NSRect) {
        let reduced = reduceMotion || NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
        let lift: CGFloat = reduced ? 0 : (isFlipped ? -1 : 1) * hoverProgress * 1.5
        if hoverProgress > 0.001 {
            let surface = NSBezierPath(roundedRect: bounds.insetBy(dx: 2, dy: 3).offsetBy(dx: 0, dy: lift),
                                       xRadius: 6, yRadius: 6)
            NSGraphicsContext.saveGraphicsState()
            let shadow = NSShadow()
            shadow.shadowColor = NSColor.black.withAlphaComponent(0.28 * hoverProgress)
            shadow.shadowBlurRadius = 3
            shadow.shadowOffset = NSSize(width: 0, height: -2)
            shadow.set()
            NSColor.white.withAlphaComponent(0.20 * hoverProgress).setFill()
            surface.fill()
            NSGraphicsContext.restoreGraphicsState()
            NSColor.white.withAlphaComponent(0.58 * hoverProgress).setStroke()
            surface.lineWidth = 0.8
            surface.stroke()
        }
        NSGraphicsContext.saveGraphicsState()
        let transform = AffineTransform(translationByX: 0, byY: lift)
        (transform as NSAffineTransform).concat()
        super.draw(dirtyRect)
        NSGraphicsContext.restoreGraphicsState()
    }

    override func resetCursorRects() {
        super.resetCursorRects()
        if isEnabled { addCursorRect(bounds, cursor: .pointingHand) }
    }

    override func updateTrackingAreas() {
        if let hoverArea { removeTrackingArea(hoverArea) }
        let area = NSTrackingArea(rect: .zero, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect],
                                  owner: self, userInfo: nil)
        addTrackingArea(area)
        hoverArea = area
        super.updateTrackingAreas()
    }

    override func mouseEntered(with event: NSEvent) {
        setHovered(true)
        guard isEnabled, hoverState.entered() else { return }
        // A short dwell prevents a pass through the header from opening settings.
        pendingOpen?.cancel()
        openGeneration += 1
        let generation = openGeneration
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.openGeneration == generation, self.pointerInside else { return }
            self.openMenu()
        }
        pendingOpen = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.08, execute: work)
    }

    override func mouseExited(with event: NSEvent) {
        openGeneration += 1
        pendingOpen?.cancel()
        setHovered(false)
        hoverState.exited()
    }

    @objc private func openFromClick() {
        if popover.isOpen { closeMenu() } else { openMenu(explicitClick: true) }
    }

    func closeMenu() {
        openGeneration += 1
        pendingOpen?.cancel(); pendingOpen = nil
        popover.close()
    }

    private func openMenu(explicitClick: Bool = false) {
        guard isEnabled, window != nil, let menu = makeMenu?(),
              hoverState.open(explicitClick: explicitClick) else { return }
        pendingOpen?.cancel()
        trackingChanged?(true)
        setHovered(true)
        popover.open(menu, from: self, reduceMotion: reduceMotion,
                     keyboardInitiated: explicitClick && !pointerInside)
        if !popover.isOpen { finishTracking() }
    }

    private func finishTracking() {
        guard hoverState.closed(pointerInside: pointerInside) else { return }
        trackingChanged?(false)
        // Dismissal while still hovering must not immediately reopen the menu.
        setHovered(pointerInside)
    }

    private var pointerInside: Bool {
        guard let window else { return false }
        return visibleRect.contains(convert(window.convertPoint(fromScreen: NSEvent.mouseLocation), from: nil))
    }

    private func setHovered(_ hovered: Bool) {
        pointerHovered = hovered
        updateAppearance()
    }

    private func updateAppearance() {
        let active = isEnabled && (pointerHovered || hoverState.isOpen)
        hoverTarget = active ? 1 : 0
        if reduceMotion || NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            appearanceTimer?.invalidate(); appearanceTimer = nil
            hoverProgress = hoverTarget
            updateDrawing()
            return
        }
        guard abs(hoverProgress - hoverTarget) > 0.005 else { updateDrawing(); return }
        guard appearanceTimer == nil else { return }
        previousAppearanceTick = ProcessInfo.processInfo.systemUptime
        let timer = Timer(timeInterval: 1.0 / 60.0, repeats: true) { [weak self] _ in
            guard let self else { return }
            let now = ProcessInfo.processInfo.systemUptime
            let dt = min(0.05, max(0, now - self.previousAppearanceTick))
            self.previousAppearanceTick = now
            self.hoverProgress += (self.hoverTarget - self.hoverProgress) * CGFloat(1 - exp(-dt * 28))
            if abs(self.hoverProgress - self.hoverTarget) < 0.005 {
                self.hoverProgress = self.hoverTarget
                self.appearanceTimer?.invalidate(); self.appearanceTimer = nil
            }
            self.updateDrawing()
        }
        appearanceTimer = timer
        RunLoop.main.add(timer, forMode: .common)
    }

    private func updateDrawing() {
        contentTintColor = .white.withAlphaComponent(isEnabled ? 0.78 + 0.22 * hoverProgress : 0.4)
        needsDisplay = true
    }

    deinit {
        pendingOpen?.cancel()
        appearanceTimer?.invalidate()
        popover.close()
    }
}

private struct ConversationGroupView: View {
    let group: ConversationGroup
    @ObservedObject var store: MonitorStore
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var isExpanded: Bool { store.isAppGroupExpanded(group.id) }

    var body: some View {
        VStack(spacing: 2) {
            ConversationCard(session: group.primary, store: store,
                             groupCount: group.sessions.count,
                             groupExpanded: isExpanded,
                             hasHiddenInterruption: group.sessions.dropFirst().contains {
                                 $0.status == "interrupted" && Date().timeIntervalSince1970 - ($0.ended_at ?? $0.updated_at) < 600
                             },
                             toggleGroup: toggle)
            if isExpanded {
                ForEach(group.sessions.dropFirst()) { session in
                    CompactConversationRow(session: session, store: store)
                        .transition(.opacity.combined(with: .move(edge: .top)))
                }
            }
        }
    }

    private func toggle() {
        withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.28)) {
            store.toggleAppGroup(group.id)
        }
    }
}

private struct HoverControl: View {
    let symbol: String
    let label: String
    var action: () -> Void
    @State private var hovered = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol).font(.system(size: 12, weight: .semibold))
                .foregroundStyle(.white.opacity(hovered ? 1 : 0.72))
                .frame(width: RadarView.compactControlWidth, height: 28)
                .contentShape(Rectangle())
                .scaleEffect(hovered && !reduceMotion ? 1.12 : 1)
        }.buttonStyle(RadarButtonStyle()).onHover { hovered = $0 }
            .animation(reduceMotion ? nil : .spring(response: 0.22, dampingFraction: 1), value: hovered)
            .help(label).accessibilityLabel(label)
    }
}

private func conversationStatusColor(_ session: SessionRecord) -> Color {
    switch session.status {
    case "completed": return Color(red: 0.28, green: 0.94, blue: 0.57)
    case "interrupted": return session.userStoppedInterruption ? Color.white.opacity(0.85)
        : Color(red: 1, green: 0.38, blue: 0.40)
    default: return .white
    }
}

private struct ConversationCard: View {
    let session: SessionRecord
    @ObservedObject var store: MonitorStore
    let groupCount: Int
    let groupExpanded: Bool
    let hasHiddenInterruption: Bool
    let toggleGroup: () -> Void
    @State private var infoHovered = false
    @State private var hovered = false
    @State private var groupArrowHovered = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var statusColor: Color { conversationStatusColor(session) }

    var body: some View {
        Button { store.openSession(session) } label: {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 4) {
                    appIcon.frame(width: 20, height: 20)
                    Text(session.app_name).font(.system(size: 10.5, weight: .semibold))
                        .lineLimit(1).minimumScaleFactor(0.9)
                    Spacer(minLength: 2)
                    Circle().fill(statusColor).frame(width: 4, height: 4)
                    Text(session.statusLabel + (groupCount > 1 ? "" : " ⓘ"))
                        .font(.system(size: 9, weight: .medium))
                        .foregroundStyle(infoHovered ? statusColor.opacity(1) : statusColor)
                        .padding(.horizontal, 2)
                        .padding(.vertical, 1)
                        .background(Capsule().fill(.white.opacity(infoHovered ? 0.16 : 0)))
                        .overlay(Capsule().strokeBorder(.white.opacity(infoHovered ? 0.28 : 0), lineWidth: 0.6))
                        .scaleEffect(infoHovered ? 1.06 : 1)
                        .fixedSize()
                    if groupCount > 1 { Color.clear.frame(width: 29, height: 20) }
                }
                HStack(spacing: 4) {
                    if let conflict = store.workspaceConflict(for: session) {
                        WorkspaceConflictBadge(conflict: conflict)
                    }
                    Text(session.primaryDisplayName).font(.system(size: 11.5, weight: .semibold))
                    .lineLimit(1).truncationMode(.middle).frame(height: 16, alignment: .topLeading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                if let title = session.secondaryDisplayName {
                    Text(title).font(.system(size: 9.5)).foregroundStyle(.white.opacity(0.72))
                        .lineLimit(1).frame(height: 11, alignment: .topLeading)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    let now = (store.pausedAt ?? context.date).timeIntervalSince1970
                    HStack(spacing: 4) {
                        Image(systemName: session.status == "interrupted" ? "pause.circle" : "clock")
                            .font(.system(size: 8))
                        Text(session.elapsedLabel(at: now)).monospacedDigit()
                        Spacer(minLength: 0)
                        if session.status == "stalled", let activity = session.last_activity_at {
                            Text("静默 \(SessionRecord.duration(now - activity))").monospacedDigit()
                        } else if store.paused {
                            Text("已暂停")
                        }
                        if store.isSupervised(session) {
                            Image(systemName: "arrow.triangle.2.circlepath")
                                .font(.system(size: 8)).foregroundStyle(.white)
                                .help(store.supervisionDetail(for: session))
                                .accessibilityLabel("持续监督已标记；" + store.supervisionDetail(for: session))
                        }
                        if store.promptRules.followUp(for: session.id) != nil {
                            Image(systemName: "arrow.uturn.forward")
                                .font(.system(size: 8)).foregroundStyle(.white.opacity(0.85))
                                .help("已保留旧会话提示词；当前由全局运行模式决定完成行为")
                        }
                    }.font(.system(size: 9)).foregroundStyle(.white).lineLimit(1)
                        .padding(.trailing, 22)
                }
                if session.status == "interrupted" || session.status == "stalled" {
                    Text(session.statusReason).font(.system(size: 9)).lineLimit(1)
                        .foregroundStyle(statusColor)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }.padding(.horizontal, 9).padding(.vertical, 3)
                .frame(height: ((session.status == "interrupted" || session.status == "stalled") ? 76 : 62)
                    + (session.secondaryDisplayName == nil ? 0 : 14))
                .shadow(color: .black.opacity(0.65), radius: 1.5, y: 1)
                .background(RoundedRectangle(cornerRadius: 11)
                    .fill(.white.opacity(hovered ? 0.26 : 0.055)))
                .overlay(RoundedRectangle(cornerRadius: 11)
                    .strokeBorder(.white.opacity(hovered ? 0.35 : 0.10), lineWidth: 0.6))
                .contentShape(RoundedRectangle(cornerRadius: 11))
        }.buttonStyle(RadarButtonStyle(cornerRadius: 9))
            .scaleEffect(hovered && !reduceMotion ? 1.015 : 1)
            .offset(y: hovered && !reduceMotion ? -2 : 0)
            .shadow(color: .black.opacity(hovered ? 0.20 : 0), radius: hovered ? 5 : 0, y: hovered ? 4 : 0)
            .onHover { hovered = $0 }
            .animation(reduceMotion ? nil : .spring(response: 0.26, dampingFraction: 1), value: hovered)
            .accessibilityLabel("\(session.app_name)：\(session.primaryDisplayName)，对话：\(session.title)，\(session.statusLabel)" + (store.workspaceConflict(for: session) == nil ? "" : "，警告：多个软件正在同一文件夹工作"))
            .overlay(alignment: .topTrailing) {
                Button { store.showSessionDetails(session) } label: {
                    Color.clear.frame(width: 64, height: 26).contentShape(Rectangle())
                }.buttonStyle(RadarButtonStyle(cornerRadius: 9)).padding(.trailing, groupCount > 1 ? 32 : 0)
                    .onHover { infoHovered = $0 }
                    .animation(reduceMotion ? nil : .spring(response: 0.22, dampingFraction: 1), value: infoHovered)
                    .help("查看状态与计时原因")
                    .accessibilityLabel(session.app_name + " 状态与计时原因")
            }
            .overlay(alignment: .topTrailing) {
                if groupCount > 1 {
                    Button(action: toggleGroup) {
                        HStack(spacing: 2) {
                            Text("\(groupCount)").font(.system(size: 8, weight: .semibold))
                            Image(systemName: groupExpanded ? "chevron.up" : "chevron.down")
                                .font(.system(size: 8, weight: .semibold))
                        }
                        .foregroundStyle(hasHiddenInterruption && !groupExpanded
                            ? Color(red: 1, green: 0.38, blue: 0.40) : .white)
                        .frame(width: 29, height: 24)
                        .background(Capsule().fill(.white.opacity(groupArrowHovered ? 0.27 : 0.07)))
                        .contentShape(Rectangle())
                    }.buttonStyle(RadarButtonStyle())
                        .onHover { groupArrowHovered = $0 }
                        .padding(.trailing, 6).padding(.top, 2)
                        .help("点击\(groupExpanded ? "收起" : "展开") \(session.app_name) 的 \(groupCount) 条对话")
                        .accessibilityLabel("点击\(groupExpanded ? "收起" : "展开") \(session.app_name) 的 \(groupCount) 条对话")
                }
            }
            .overlay(alignment: .topTrailing) {
                Button { store.removeSession(session) } label: {
                    Image(systemName: "xmark").font(.system(size: 8, weight: .semibold))
                        .foregroundStyle(.white).frame(width: 22, height: 20)
                        .background(Capsule().fill(.white.opacity(hovered ? 0.24 : 0.07)))
                        .contentShape(Rectangle())
                }.buttonStyle(RadarButtonStyle())
                    .opacity(0.9)
                    .padding(.trailing, 6).padding(.top, session.secondaryDisplayName == nil ? 42 : 56)
                    .help("从雷达移除此会话；保留原聊天，可撤销")
                    .accessibilityLabel(session.app_name + "：从雷达移除会话")
            }
            .contextMenu {
                Button("查看状态与计时原因…") { store.showSessionDetails(session) }
                Button("打开对应应用") { store.openSession(session) }
                if session.status == "interrupted" {
                    Button("手动恢复此会话") {
                        store.retryContinuation(sessionID: session.id, key: ContinuationPolicy.key(session))
                    }
                    Divider()
                }
                Button(store.isSupervised(session) ? "取消持续监督" : "持续监督此会话") {
                    store.toggleSupervision(for: session)
                }
                Button(store.promptRules.followUp(for: session.id) == nil ? "保留会话提示词…" : "编辑已保留的会话提示词…") {
                    store.showFollowUpEditor(session)
                }
                if store.promptRules.followUp(for: session.id) != nil {
                    Button("移除已保留的会话提示词", role: .destructive) { store.setFollowUp(nil, for: session) }
                }
                Divider()
                Button("从雷达移除会话", role: .destructive) { store.removeSession(session) }
            }
            .help("\(session.displayIdentityDetail)\n\(session.statusLabel) · \(session.statusReason)\n\(session.timingExplanation)\n点击右上角状态可查看详情")
    }

    @ViewBuilder private var appIcon: some View {
        if let icon = store.applicationIcon(for: session) {
            Image(nsImage: icon).renderingMode(.original).resizable().scaledToFit()
                .accessibilityLabel(session.app_name + " 图标")
        } else {
            Image(systemName: session.app_id == "claude-code" ? "terminal" : "app.dashed")
                .font(.system(size: 17)).foregroundStyle(.white)
        }
    }
}

private struct CompactConversationRow: View {
    let session: SessionRecord
    @ObservedObject var store: MonitorStore
    @State private var hovered = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var statusColor: Color { conversationStatusColor(session) }
    private var showsReason: Bool { session.status == "interrupted" || session.status == "stalled" }

    var body: some View {
        Button { store.openSession(session) } label: {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 4) {
                    if let conflict = store.workspaceConflict(for: session) {
                        WorkspaceConflictBadge(conflict: conflict)
                    }
                    Text(session.primaryDisplayName).font(.system(size: 10, weight: .semibold))
                        .lineLimit(1).truncationMode(.middle)
                    Spacer(minLength: 2)
                    Circle().fill(statusColor).frame(width: 3, height: 3)
                    Text(session.statusLabel + " ⓘ").font(.system(size: 8, weight: .medium))
                        .foregroundStyle(statusColor).fixedSize()
                }
                if let title = session.secondaryDisplayName {
                    Text(title).font(.system(size: 9)).foregroundStyle(.white.opacity(0.72))
                        .lineLimit(1).frame(height: 12, alignment: .topLeading)
                }
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    let now = (store.pausedAt ?? context.date).timeIntervalSince1970
                    HStack(spacing: 3) {
                        Image(systemName: session.status == "interrupted" ? "pause.circle" : "clock")
                        Text(session.elapsedLabel(at: now)).monospacedDigit()
                        Spacer(minLength: 0)
                        if store.isSupervised(session) {
                            Image(systemName: "arrow.triangle.2.circlepath")
                                .font(.system(size: 8)).foregroundStyle(.white)
                                .help(store.supervisionDetail(for: session))
                                .accessibilityLabel("持续监督已标记；" + store.supervisionDetail(for: session))
                        }
                        if store.promptRules.followUp(for: session.id) != nil {
                            Image(systemName: "arrow.uturn.forward")
                                .help("已保留旧会话提示词；当前由全局运行模式决定完成行为")
                        }
                    }.font(.system(size: 8)).foregroundStyle(.white).lineLimit(1)
                        .padding(.trailing, 20)
                }
                if showsReason {
                    Text(session.statusReason).font(.system(size: 8)).foregroundStyle(statusColor)
                        .lineLimit(1).frame(maxWidth: .infinity, alignment: .leading)
                }
            }.padding(.leading, 26).padding(.trailing, 9).padding(.vertical, 3)
                .frame(height: (showsReason ? 54 : 40) + (session.secondaryDisplayName == nil ? 0 : 14))
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: 9).fill(.white.opacity(hovered ? 0.21 : 0.04)))
                .overlay(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 1).fill(.white.opacity(0.36))
                        .frame(width: 1).padding(.leading, 17).padding(.vertical, 6)
                }
                .contentShape(RoundedRectangle(cornerRadius: 9))
        }.buttonStyle(RadarButtonStyle(cornerRadius: 9))
            .scaleEffect(hovered && !reduceMotion ? 1.01 : 1)
            .offset(y: hovered && !reduceMotion ? -1 : 0)
            .onHover { hovered = $0 }
            .animation(reduceMotion ? nil : .spring(response: 0.22, dampingFraction: 1), value: hovered)
            .accessibilityLabel("\(session.app_name)：\(session.primaryDisplayName)，对话：\(session.title)，\(session.statusLabel)" + (store.workspaceConflict(for: session) == nil ? "" : "，警告：多个软件正在同一文件夹工作"))
            .overlay(alignment: .topTrailing) {
                Button { store.showSessionDetails(session) } label: {
                    Color.clear.frame(width: 58, height: 19).contentShape(Rectangle())
                }.buttonStyle(RadarButtonStyle(cornerRadius: 9)).help("查看状态与计时原因")
                    .accessibilityLabel(session.app_name + " 状态与计时原因")
            }
            .overlay(alignment: .bottomTrailing) {
                Button { store.removeSession(session) } label: {
                    Image(systemName: "xmark").font(.system(size: 7, weight: .semibold))
                        .foregroundStyle(.white).frame(width: 18, height: 16)
                        .background(Capsule().fill(.white.opacity(hovered ? 0.24 : 0.07)))
                        .contentShape(Rectangle())
                }.buttonStyle(RadarButtonStyle())
                    .opacity(0.9)
                    .padding(.trailing, 6).padding(.bottom, 3)
                    .help("从雷达移除此会话；保留原聊天，可撤销")
                    .accessibilityLabel(session.app_name + "：从雷达移除会话")
            }
            .contextMenu {
                Button("查看状态与计时原因…") { store.showSessionDetails(session) }
                Button("打开对应应用") { store.openSession(session) }
                if session.status == "interrupted" {
                    Button("手动恢复此会话") {
                        store.retryContinuation(sessionID: session.id, key: ContinuationPolicy.key(session))
                    }
                    Divider()
                }
                Button(store.isSupervised(session) ? "取消持续监督" : "持续监督此会话") {
                    store.toggleSupervision(for: session)
                }
                Button(store.promptRules.followUp(for: session.id) == nil ? "保留会话提示词…" : "编辑已保留的会话提示词…") {
                    store.showFollowUpEditor(session)
                }
                if store.promptRules.followUp(for: session.id) != nil {
                    Button("移除已保留的会话提示词", role: .destructive) { store.setFollowUp(nil, for: session) }
                }
                Divider()
                Button("从雷达移除会话", role: .destructive) { store.removeSession(session) }
            }
            .help("\(session.displayIdentityDetail)\n\(session.statusLabel) · \(session.statusReason)\n\(session.timingExplanation)\n点击右上角状态可查看详情")
    }
}

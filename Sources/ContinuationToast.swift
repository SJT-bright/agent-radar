import AppKit
import SwiftUI

private final class ContinuationPanel: NSPanel {
    var dragRegions: [NSRect] = []
    var dragStarted: () -> Void = {}
    var dragEnded: (NSRect, Bool) -> Void = { _, _ in }
    private var dragPointer: NSPoint?
    private var dragOrigin: NSPoint?
    private var releaseTimer: Timer?
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    override func sendEvent(_ event: NSEvent) {
        if event.type == .leftMouseDown, dragRegions.contains(where: { $0.contains(event.locationInWindow) }) {
            dragPointer = convertPoint(toScreen: event.locationInWindow)
            dragOrigin = frame.origin
            dragStarted()
            let timer = Timer(timeInterval: 0.1, repeats: true) { [weak self] _ in
                if NSEvent.pressedMouseButtons & 1 == 0 { self?.finishDrag() }
            }
            releaseTimer?.invalidate()
            releaseTimer = timer
            RunLoop.main.add(timer, forMode: .common)
            return
        }
        if event.type == .leftMouseDragged, let pointer = dragPointer, let origin = dragOrigin {
            let current = convertPoint(toScreen: event.locationInWindow)
            setFrameOrigin(NSPoint(x: origin.x + current.x - pointer.x, y: origin.y + current.y - pointer.y))
            return
        }
        if event.type == .leftMouseUp, dragPointer != nil {
            finishDrag()
            return
        }
        super.sendEvent(event)
    }

    private func finishDrag() {
        guard let origin = dragOrigin else { return }
        dragPointer = nil
        dragOrigin = nil
        releaseTimer?.invalidate()
        releaseTimer = nil
        dragEnded(frame, frame.origin != origin)
    }

    deinit { releaseTimer?.invalidate() }
}

/// Window coordinates stay in AppKit's global points, including screens left of the main screen.
enum ContinuationToastGeometry {
    static func dragRegions(size: NSSize, hasSecondaryName: Bool, workspaceConflict: Bool) -> [NSRect] {
        // Header is 28 points high. Its icon and open button remain outside the drag target.
        let headerLeft: CGFloat = 10 + 28 + 5
        let headerRight = size.width - 10 - (workspaceConflict ? 0 : 26 + 5)
        var regions = [NSRect(x: headerLeft, y: size.height - 10 - 28, width: headerRight - headerLeft, height: 28),
                       NSRect(x: 10, y: size.height - 10 - 28 - 5 - 16, width: size.width - 20, height: 16)]
        if hasSecondaryName {
            regions.append(NSRect(x: 10, y: size.height - 10 - 28 - 5 - 16 - 5 - 12, width: size.width - 20, height: 12))
        }
        return regions
    }

    static func anchoredFrame(size: NSSize, anchor: NSRect, visibleFrames: [NSRect]) -> NSRect {
        let visible = targetVisibleFrame(for: anchor, in: visibleFrames)
        let x = anchor.minX >= visible.minX + size.width + 18
            ? anchor.minX - size.width - 12 : anchor.maxX + 12
        return clamp(NSRect(x: x, y: anchor.maxY - size.height, width: size.width, height: size.height),
                     to: visibleFrames)
    }

    static func frame(size: NSSize, topLeft: NSPoint, visibleFrames: [NSRect]) -> NSRect {
        clamp(NSRect(x: topLeft.x, y: topLeft.y - size.height, width: size.width, height: size.height),
              to: visibleFrames)
    }

    static func clamp(_ frame: NSRect, to visibleFrames: [NSRect]) -> NSRect {
        guard !visibleFrames.isEmpty else { return frame }
        let visible = targetVisibleFrame(for: frame, in: visibleFrames)
        return NSRect(x: max(visible.minX, min(frame.minX, visible.maxX - frame.width)),
                      y: max(visible.minY, min(frame.minY, visible.maxY - frame.height)),
                      width: frame.width, height: frame.height)
    }

    private static func targetVisibleFrame(for frame: NSRect, in visibleFrames: [NSRect]) -> NSRect {
        guard let first = visibleFrames.first else { return frame }
        // Use the screen containing most of the card; if disconnected, choose the nearest screen.
        return visibleFrames.reduce(first) { best, next in
            let bestIntersection = best.intersection(frame), nextIntersection = next.intersection(frame)
            let bestArea = bestIntersection.isNull ? 0 : bestIntersection.width * bestIntersection.height
            let nextArea = nextIntersection.isNull ? 0 : nextIntersection.width * nextIntersection.height
            if bestArea != nextArea { return nextArea > bestArea ? next : best }
            let point = NSPoint(x: frame.midX, y: frame.midY)
            func distance(to rect: NSRect) -> CGFloat {
                let dx = max(rect.minX - point.x, max(0, point.x - rect.maxX))
                let dy = max(rect.minY - point.y, max(0, point.y - rect.maxY))
                return dx * dx + dy * dy
            }
            return distance(to: next) < distance(to: best) ? next : best
        }
    }
}

final class ContinuationToast {
    private struct Entry: SessionNoticeEntry {
        var notice: ContinuationNotice
        let anchor: NSRect
        let cancel: () -> Void
        let permission: () -> Void
        let retry: (String, String) -> Void
        let open: (String) -> Bool
        let cancelRecovery: ((String, String) -> Void)?
        var key: String { (notice.sessionID ?? notice.conversation) + ":" + (notice.recoveryKey ?? notice.title) }
        var sessionKey: String { notice.sessionID ?? (notice.title + ":" + notice.conversation) }
        var completionKey: String? { notice.isCompletion ? key : nil }
    }
    private var panel: ContinuationPanel?
    private var queue = SessionNoticeQueue<Entry>()
    private var current: Entry? { queue.current }
    private var retryInFlight = Set<String>()
    private var presentedKeys = Set<String>()
    private var draggedTopLeft: NSPoint?
    private var dragging = false
    private var screenObserver: NSObjectProtocol?

    /// Hide only the panel. Unread notices remain available when the user enables popups again.
    var popupEnabled = true {
        didSet {
            guard popupEnabled != oldValue else { return }
            if popupEnabled { render() } else { panel?.orderOut(nil) }
        }
    }
    /// New notice first displayed; progress updates and show/hide switches do not notify again.
    var onDidPresentNotice: ((ContinuationNotice) -> Void)?

    init() {
        screenObserver = NotificationCenter.default.addObserver(forName: NSApplication.didChangeScreenParametersNotification,
                                                                object: nil, queue: .main) { [weak self] _ in
            guard let self = self, let panel = self.panel, !self.dragging else { return }
            let frame = ContinuationToastGeometry.clamp(panel.frame, to: NSScreen.screens.map(\.visibleFrame))
            panel.setFrame(frame, display: panel.isVisible)
            if self.draggedTopLeft != nil { self.draggedTopLeft = NSPoint(x: frame.minX, y: frame.maxY) }
        }
    }

    deinit {
        if let observer = screenObserver { NotificationCenter.default.removeObserver(observer) }
    }

    func reconcileWorkspaceConflicts(_ paths: Set<String>) {
        let obsolete: (Entry) -> Bool = { $0.notice.isWorkspaceConflict && !paths.contains($0.notice.project) }
        guard current.map(obsolete) == true || queue.pending.contains(where: obsolete) else { return }
        queue.remove(where: obsolete)
        if current == nil { panel?.orderOut(nil) } else { render() }
    }

    /// Dismiss this session, then expose the next unread session.
    func close() {
        queue.close()
        if current == nil { panel?.orderOut(nil) } else { render() }
    }

    /// 一键清除：当前条与全部排队提醒一起关闭。完成轮次的验收标记同步记录，
    /// 之后同一轮完成不会再次弹出；其余排队提醒与逐条点「知道了」等效丢弃。
    func closeAll() {
        queue.closeAll()
        panel?.orderOut(nil)
    }

    func show(_ notice: ContinuationNotice, anchor: NSRect, cancel: @escaping () -> Void,
              permission: @escaping () -> Void, retry: @escaping (String, String) -> Void = { _, _ in },
              open: @escaping (String) -> Bool = { _ in false },
              cancelRecovery: ((String, String) -> Void)? = nil) {
        let entry = Entry(notice: notice, anchor: anchor, cancel: cancel, permission: permission, retry: retry,
                          open: open, cancelRecovery: cancelRecovery)
        guard queue.show(entry) else { return }
        if !notice.isRecovering && !notice.cancellable { retryInFlight.remove(entry.key) }
        render()
    }

    private func render() {
        guard popupEnabled else { panel?.orderOut(nil); return }
        // Keep the current hosting tree and position intact until the pointer is released.
        guard !dragging else { return }
        guard let entry = current else { return }
        if panel == nil {
            let panel = ContinuationPanel(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            panel.title = "AI 监督提醒"
            panel.level = .floating
            panel.hidesOnDeactivate = false
            panel.isOpaque = false
            panel.backgroundColor = .clear
            panel.hasShadow = true
            panel.isMovable = true
            panel.isMovableByWindowBackground = false
            panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            panel.appearance = NSAppearance(named: .darkAqua)
            panel.dragStarted = { [weak self] in self?.dragging = true }
            panel.dragEnded = { [weak self] frame, moved in
                guard let self = self else { return }
                self.dragging = false
                if moved { self.draggedTopLeft = NSPoint(x: frame.minX, y: frame.maxY) }
                self.render()
            }
            self.panel = panel
        }
        guard let panel = panel else { return }
        let view = ContinuationToastView(notice: entry.notice, pendingCount: queue.pendingCount,
                                         hasPending: queue.pendingCount > 0,
                                         requested: retryInFlight.contains(entry.key),
                                         cancel: { [weak self] in
                                             if let session = entry.notice.sessionID, let key = entry.notice.recoveryKey {
                                                 entry.cancelRecovery?(session, key)
                                             } else { entry.cancel() }
                                             if self?.current?.key == entry.key { self?.close() }
                                         }, close: { [weak self] in self?.close() },
                                         dismissAll: { [weak self] in self?.closeAll() }, permission: entry.permission,
                                         open: { [weak self] in
                                             guard let self = self, self.current?.key == entry.key,
                                                   let session = entry.notice.sessionID else { return }
                                             if entry.open(session) {
                                                 self.close()
                                             } else {
                                                 self.queue.updateCurrent { $0.notice.message = "原会话暂未读取到，请在对应应用中打开" }
                                                 self.render()
                                             }
                                         },
                                         retry: { [weak self] in
                                             guard let self = self, self.current?.key == entry.key,
                                                   entry.notice.canRetry, !entry.notice.isRecovering,
                                                   let session = entry.notice.sessionID, let key = entry.notice.recoveryKey,
                                                   self.retryInFlight.insert(entry.key).inserted else { return }
                                             self.render() // Disable synchronously, before invoking controller.
                                             entry.retry(session, key)
                                         })
        panel.contentView = NSHostingView(rootView: view)
        let visibleFrames = NSScreen.screens.map(\.visibleFrame)
        let size = ContinuationToastView.size(for: entry.notice, hasPending: queue.pendingCount > 0)
        let frame = draggedTopLeft.map { ContinuationToastGeometry.frame(size: size, topLeft: $0, visibleFrames: visibleFrames) }
            ?? ContinuationToastGeometry.anchoredFrame(size: size, anchor: entry.anchor, visibleFrames: visibleFrames)
        panel.setFrame(frame, display: true)
        panel.dragRegions = ContinuationToastGeometry.dragRegions(size: size,
            hasSecondaryName: entry.notice.secondaryDisplayName != nil, workspaceConflict: entry.notice.isWorkspaceConflict)
        if draggedTopLeft != nil { draggedTopLeft = NSPoint(x: frame.minX, y: frame.maxY) }
        panel.orderFrontRegardless()
        if presentedKeys.insert(entry.key).inserted { onDidPresentNotice?(entry.notice) }
        if entry.notice.title.contains("提醒预览") { panel.makeKey() }
    }
}

/// Native dragging is confined to labels and blank title space; buttons and scrolling remain intact.
private struct ToastDragHandle: NSViewRepresentable {
    let toolTip: String

    func makeNSView(context: Context) -> DragView { DragView() }
    func updateNSView(_ view: DragView, context: Context) {
        view.toolTip = toolTip
    }

    final class DragView: NSView {
        override func resetCursorRects() { addCursorRect(bounds, cursor: .openHand) }
    }
}

private struct RecoveryIcon: View {
    let enabled: Bool
    let recovering: Bool
    let reason: String
    let completion: Bool
    let retry: () -> Void
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var body: some View {
        Button(action: retry) {
            TimelineView(.animation(minimumInterval: 1.0 / 30, paused: !recovering || reduceMotion)) { context in
                Image(systemName: "arrow.triangle.2.circlepath")
                    .font(.system(size: 16, weight: .medium)).foregroundStyle(.white)
                    .rotationEffect(.degrees(recovering && !reduceMotion ? context.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 1) * 360 : 0))
                    .frame(width: 28, height: 28).contentShape(Circle())
            }
        }.buttonStyle(RadarButtonStyle(cornerRadius: 16))
            .disabled(!enabled || recovering)
            .help(recovering ? "正在继续当前会话" : (enabled ? (completion ? "切换到原对话并发送「请继续」" : "恢复此条中断会话") : reason))
            .accessibilityLabel(completion ? "继续此对话" : "重试当前会话")
            .accessibilityHint(recovering ? "正在定位与发送，请等待" : (enabled ? "只对这条会话发送一次，不更改自动发送开关" : reason))
    }
}

private struct ToastCapsuleButton: View {
    let title: String
    let action: () -> Void
    var prominent = false
    @State private var hovered = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        Button(action: action) {
            Text(title).font(.system(size: prominent ? 12 : 11, weight: .semibold))
                .foregroundStyle(.white)
                .padding(.horizontal, prominent ? 18 : 10)
                .frame(minWidth: prominent ? 86 : 0, minHeight: prominent ? 30 : 26)
                .background(Capsule().fill(.white.opacity(hovered ? 0.42 : (prominent ? 0.25 : 0.14))))
                .overlay(Capsule().strokeBorder(.white.opacity(hovered ? 0.42 : 0.16), lineWidth: 0.6))
                .shadow(color: .white.opacity(hovered ? 0.15 : 0), radius: 4)
                .scaleEffect(hovered && !reduceMotion ? 1.05 : 1)
                .contentShape(Capsule())
        }.buttonStyle(RadarButtonStyle(cornerRadius: 18))
            .onHover { hovered = $0 }
            .animation(reduceMotion ? nil : .spring(response: 0.22, dampingFraction: 1), value: hovered)
    }
}

private struct ContinuationToastView: View {
    static func size(for notice: ContinuationNotice, hasPending: Bool) -> NSSize {
        let compact = notice.isCompletion && !hasPending && !notice.needsPermission && !notice.cancellable
        return NSSize(width: compact ? 232 : 240,
                      height: (compact ? 140 : 154) + (notice.secondaryDisplayName == nil ? 0 : 18)
                        + (notice.timing == nil ? 0 : 38) + (hasPending ? 30 : 0)
                        + (notice.needsPermission ? 30 : 0) + (notice.isWorkspaceConflict ? 10 : 0))
    }
    let notice: ContinuationNotice
    let pendingCount: Int
    let hasPending: Bool
    let requested: Bool
    let cancel: () -> Void
    let close: () -> Void
    let dismissAll: () -> Void
    let permission: () -> Void
    let open: () -> Void
    let retry: () -> Void
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 5) {
                if notice.isWorkspaceConflict {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 18)).foregroundStyle(.orange).frame(width: 28, height: 28)
                        .accessibilityLabel("多个软件正在同一文件夹工作")
                } else {
                    RecoveryIcon(enabled: notice.canRetry && !requested,
                             recovering: requested || notice.timing?.phase == .operating || notice.timing?.phase == .confirming || (notice.timing == nil && notice.isRecovering),
                             reason: notice.message, completion: notice.isCompletion, retry: retry)
                }
                Text(notice.title).fontWeight(.semibold).lineLimit(1)
                    .frame(maxWidth: .infinity, minHeight: 28, alignment: .leading)
                    .overlay(ToastDragHandle(toolTip: notice.title + " · 拖动可移动提醒框"))
                if !notice.isWorkspaceConflict { Button(action: open) {
                    Image(systemName: "arrow.up.right.square")
                        .font(.system(size: 15, weight: .semibold))
                        .frame(width: 26, height: 26)
                }
                .buttonStyle(RadarButtonStyle(cornerRadius: 8))
                .disabled(notice.sessionID == nil)
                .help("打开这条提醒对应的原对话")
                .accessibilityLabel("打开原对话")
                }
            }.font(.system(size: 11))
            Text(notice.primaryDisplayName).font(.system(size: 13, weight: .semibold))
                .lineLimit(1).truncationMode(.middle)
                .frame(maxWidth: .infinity, minHeight: 16, maxHeight: 16, alignment: .leading)
                .overlay(ToastDragHandle(toolTip: notice.project.isEmpty ? notice.conversation : notice.project))
            if let conversation = notice.secondaryDisplayName {
                Text(conversation).font(.system(size: 10)).foregroundStyle(.white.opacity(0.72))
                    .lineLimit(1).frame(maxWidth: .infinity, minHeight: 12, maxHeight: 12, alignment: .leading)
                    .overlay(ToastDragHandle(toolTip: conversation))
            }
            ScrollView(.vertical) {
                Text(notice.message).font(.system(size: 11))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                    .help(notice.message)
            }.frame(height: notice.isWorkspaceConflict ? 52 : (notice.isCompletion && !hasPending && !notice.needsPermission && !notice.cancellable ? 26 : 42))
            if let timing = notice.timing {
                TimelineView(.periodic(from: .now, by: 0.25)) { context in
                    let idle = HardwareInputIdle.seconds
                    VStack(alignment: .leading, spacing: 3) {
                        Text(timing.label(at: context.date.timeIntervalSince1970, idleSeconds: idle))
                            .font(.system(size: 12, weight: .semibold)).monospacedDigit()
                            .foregroundStyle(.orange).lineLimit(1)
                            .help(timing.label(at: context.date.timeIntervalSince1970, idleSeconds: idle))
                        Text(timing.inputHint(idleSeconds: idle))
                            .font(.system(size: 9)).foregroundStyle(.white.opacity(0.8)).lineLimit(1)
                            .help(timing.inputHint(idleSeconds: idle))
                    }.accessibilityElement(children: .combine)
                }
            }
            Spacer(minLength: 0)
            VStack(alignment: .leading, spacing: 4) {
                if notice.needsPermission {
                    HStack { ToastCapsuleButton(title: "开启辅助功能", action: permission); Spacer(minLength: 0) }
                }
                if pendingCount > 0 {
                    HStack(spacing: 4) {
                        Text("另有 \(pendingCount) 项待验收").font(.system(size: 10)).lineLimit(1)
                            .minimumScaleFactor(0.8).foregroundStyle(.white.opacity(0.75))
                        Spacer(minLength: 0)
                        ToastCapsuleButton(title: "全部知道了", action: dismissAll)
                    }
                }
                HStack(spacing: 6) {
                    if notice.cancellable { ToastCapsuleButton(title: "取消本轮", action: cancel) }
                    Spacer(minLength: 4)
                    ToastCapsuleButton(title: "知道了", action: close,
                                       prominent: notice.isCompletion && !notice.cancellable && !hasPending && !notice.needsPermission)
                }
            }
        }.padding(10).frame(width: Self.size(for: notice, hasPending: hasPending).width,
                            height: Self.size(for: notice, hasPending: hasPending).height, alignment: .topLeading)
            .foregroundStyle(.white).preferredColorScheme(.dark)
            .background { ZStack { FrostedBackdrop(strength: 0.9); Color.black.opacity(0.44) } }
            .clipShape(RoundedRectangle(cornerRadius: 18))
            .overlay(RoundedRectangle(cornerRadius: 18).strokeBorder(.white.opacity(0.3), lineWidth: 0.7))
    }
}

import AppKit
import SwiftUI

private final class ContinuationPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

final class ContinuationToast {
    private struct Entry {
        var notice: ContinuationNotice
        let anchor: NSRect
        let cancel: () -> Void
        let permission: () -> Void
        let retry: (String, String) -> Void
        let open: (String) -> Bool
        let cancelRecovery: ((String, String) -> Void)?
        var key: String { (notice.sessionID ?? notice.conversation) + ":" + (notice.recoveryKey ?? notice.title) }
    }
    private var panel: NSPanel?
    private var current: Entry?
    private var pending: [Entry] = []
    private var acknowledgedCompletions = Set<String>()
    private var retryInFlight = Set<String>()

    /// Dismiss this notice, then expose the next unread project completion.
    func close() {
        if let entry = current, entry.notice.isCompletion { acknowledgedCompletions.insert(entry.key) }
        current = pending.isEmpty ? nil : pending.removeFirst()
        if current == nil { panel?.orderOut(nil) } else { render() }
    }

    /// 一键清除：当前条与全部排队提醒一起关闭。完成轮次的验收标记同步记录，
    /// 之后同一轮完成不会再次弹出；其余排队提醒与逐条点「知道了」等效丢弃。
    func closeAll() {
        for entry in pending where entry.notice.isCompletion { acknowledgedCompletions.insert(entry.key) }
        if let entry = current, entry.notice.isCompletion { acknowledgedCompletions.insert(entry.key) }
        pending.removeAll()
        close()
    }

    func show(_ notice: ContinuationNotice, anchor: NSRect, cancel: @escaping () -> Void,
              permission: @escaping () -> Void, retry: @escaping (String, String) -> Void = { _, _ in },
              open: @escaping (String) -> Bool = { _ in false },
              cancelRecovery: ((String, String) -> Void)? = nil) {
        let entry = Entry(notice: notice, anchor: anchor, cancel: cancel, permission: permission, retry: retry,
                          open: open, cancelRecovery: cancelRecovery)
        if notice.isCompletion && acknowledgedCompletions.contains(entry.key) { return }
        if !notice.isRecovering && !notice.cancellable { retryInFlight.remove(entry.key) }
        if current?.key == entry.key {
            // An automatic countdown may never replace an unread completion.
            if current?.notice.isCompletion == true && !notice.isCompletion {
                enqueue(entry)
            } else { current = entry }
        } else if current?.notice.isCompletion == true || notice.isCompletion && current != nil {
            enqueue(entry)
        } else { current = entry }
        render()
    }

    private func enqueue(_ entry: Entry) {
        if let index = pending.firstIndex(where: { $0.key == entry.key && $0.notice.isCompletion == entry.notice.isCompletion }) {
            pending[index] = entry
        } else { pending.append(entry) }
    }

    private func render() {
        guard let entry = current else { return }
        if panel == nil {
            let panel = ContinuationPanel(contentRect: .zero, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
            panel.title = "AI 监督提醒"
            panel.level = .floating
            panel.hidesOnDeactivate = false
            panel.isOpaque = false
            panel.backgroundColor = .clear
            panel.hasShadow = true
            panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            panel.appearance = NSAppearance(named: .darkAqua)
            self.panel = panel
        }
        guard let panel = panel else { return }
        let view = ContinuationToastView(notice: entry.notice, pendingCount: pending.filter { $0.notice.isCompletion }.count,
                                         hasPending: !pending.isEmpty,
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
                                                 self.current?.notice.message = "原会话暂未读取到，请在对应应用中打开"
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
        let anchor = entry.anchor
        let visible = NSScreen.screens.map(\.visibleFrame).first(where: { $0.intersects(anchor) }) ?? NSScreen.main!.visibleFrame
        let size = ContinuationToastView.size(for: entry.notice, hasPending: !pending.isEmpty)
        let x = anchor.minX >= visible.minX + size.width + 18 ? anchor.minX - size.width - 12 : min(visible.maxX - size.width, anchor.maxX + 12)
        panel.setFrame(NSRect(x: max(visible.minX, x), y: max(visible.minY, min(anchor.maxY - size.height, visible.maxY - size.height)), width: size.width, height: size.height), display: true)
        panel.orderFrontRegardless()
        if entry.notice.title.contains("提醒预览") { panel.makeKey() }
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
                    .frame(width: 32, height: 32).contentShape(Circle())
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
            Text(title).font(.system(size: prominent ? 13 : 11, weight: .semibold))
                .foregroundStyle(.white)
                .padding(.horizontal, prominent ? 20 : 12)
                .frame(minWidth: prominent ? 94 : 0, minHeight: prominent ? 34 : 26)
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
        notice.isCompletion && !hasPending && !notice.needsPermission && !notice.cancellable
            ? NSSize(width: 250, height: 142) : NSSize(width: 264, height: 174)
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
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 7) {
                RecoveryIcon(enabled: notice.canRetry && !requested, recovering: notice.isRecovering || requested,
                             reason: notice.message, completion: notice.isCompletion, retry: retry)
                Text(notice.title).fontWeight(.semibold).lineLimit(1)
                Spacer(minLength: 0)
                Button(action: open) {
                    Image(systemName: "arrow.up.right.square")
                        .font(.system(size: 15, weight: .semibold))
                        .frame(width: 28, height: 28)
                }
                .buttonStyle(RadarButtonStyle(cornerRadius: 8))
                .disabled(notice.sessionID == nil)
                .help("打开这条提醒对应的原对话")
                .accessibilityLabel("打开原对话")
            }.font(.system(size: 12))
            Text(notice.conversation).font(.system(size: 12, weight: .medium)).lineLimit(2)
            Text(notice.message).font(.system(size: 11)).lineLimit(3).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            HStack(spacing: 6) {
                if notice.needsPermission { ToastCapsuleButton(title: "开启辅助功能", action: permission) }
                if pendingCount > 0 {
                    Text("另有 \(pendingCount) 项待验收").font(.system(size: 10)).lineLimit(1)
                        .minimumScaleFactor(0.8).foregroundStyle(.white.opacity(0.75))
                }
                Spacer(minLength: 4)
                if notice.cancellable { ToastCapsuleButton(title: "取消本轮", action: cancel) }
                if hasPending {
                    // 排队多条时一键清除，免去逐条点「知道了」。
                    ToastCapsuleButton(title: "全部知道了", action: dismissAll)
                }
                if !notice.cancellable {
                    ToastCapsuleButton(title: "知道了", action: close, prominent: true)
                } else if !hasPending {
                    // 不想打断自动恢复的用户也需要一个不打扰的关闭出口。
                    ToastCapsuleButton(title: "知道了", action: close)
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

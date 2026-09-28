import AppKit
import SwiftUI

/// The WindowServer supplies a live blurred backdrop. No screen capture,
/// private filters or permissions are used. Only the material layer is faded.
struct FrostedBackdrop: NSViewRepresentable {
    var strength: Double

    func makeNSView(context: Context) -> PassiveEffectView {
        let view = PassiveEffectView()
        view.material = .hudWindow
        view.blendingMode = .behindWindow
        view.state = .active
        view.appearance = NSAppearance(named: .darkAqua)
        view.alphaValue = strength
        let mask = NSImage(size: NSSize(width: 37, height: 37), flipped: false) { rect in
            NSColor.white.setFill()
            NSBezierPath(roundedRect: rect, xRadius: 18, yRadius: 18).fill()
            return true
        }
        mask.capInsets = NSEdgeInsets(top: 18, left: 18, bottom: 18, right: 18)
        mask.resizingMode = .stretch
        view.maskImage = mask
        return view
    }

    func updateNSView(_ view: PassiveEffectView, context: Context) {
        guard abs(view.alphaValue - strength) > 0.001 else { return }
        NSAnimationContext.runAnimationGroup { animation in
            animation.duration = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion ? 0 : 0.18
            view.animator().alphaValue = strength
        }
    }
}

final class PassiveEffectView: NSVisualEffectView {
    override func hitTest(_ point: NSPoint) -> NSView? { nil }
}

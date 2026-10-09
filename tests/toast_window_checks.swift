import AppKit
import Foundation

/// Production toast only: no MonitorStore, collector, sender, settings or formal data roots.
@main
struct ToastWindowChecks {
    static var count = 0
    static func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
        guard condition() else { fatalError(message) }
        count += 1
    }

    static func main() {
        let mainScreen = NSRect(x: 0, y: 0, width: 1440, height: 900)
        let leftScreen = NSRect(x: -1920, y: 0, width: 1920, height: 1080)
        let size = NSSize(width: 232, height: 140)
        let leftAnchored = ContinuationToastGeometry.anchoredFrame(size: size,
            anchor: NSRect(x: -1870, y: 700, width: 100, height: 100), visibleFrames: [mainScreen, leftScreen])
        expect(leftScreen.contains(leftAnchored), "Anchoring must support a screen left of the primary screen")
        let beforeResize = ContinuationToastGeometry.frame(size: size, topLeft: NSPoint(x: 600, y: 700), visibleFrames: [mainScreen])
        let afterResize = ContinuationToastGeometry.frame(size: NSSize(width: 240, height: 250), topLeft: NSPoint(x: 600, y: 700), visibleFrames: [mainScreen])
        expect(beforeResize.minX == afterResize.minX && beforeResize.maxY == afterResize.maxY,
               "Countdown and queue layout changes must preserve the dragged top-left position")
        let disconnected = ContinuationToastGeometry.frame(size: size, topLeft: NSPoint(x: -900, y: 600), visibleFrames: [mainScreen])
        expect(mainScreen.contains(disconnected), "Removing the dragged-to screen must put the card back on a remaining screen")
        expect(disconnected.maxY == 600, "Screen recovery should preserve the still-valid vertical position")
        let edge = ContinuationToastGeometry.clamp(NSRect(x: 1380, y: 850, width: 240, height: 220), to: [mainScreen])
        expect(mainScreen.contains(edge), "The whole card must stay inside the visible frame at the top and right edges")
        let original = NSRect(x: 20, y: 40, width: 240, height: 220)
        expect(ContinuationToastGeometry.clamp(original, to: []) == original, "Transiently absent screen information must not crash or invent coordinates")
        let handles = ContinuationToastGeometry.dragRegions(size: size, hasSecondaryName: true, workspaceConflict: false)
        func draggable(_ point: NSPoint) -> Bool { handles.contains(where: { $0.contains(point) }) }
        expect(draggable(NSPoint(x: 100, y: size.height - 24)), "The header title must be a drag target")
        expect(!draggable(NSPoint(x: 24, y: size.height - 24)), "The retry icon must remain outside the drag target")
        expect(!draggable(NSPoint(x: size.width - 22, y: size.height - 24)), "The open button must remain outside the drag target")
        expect(!draggable(NSPoint(x: 100, y: 20)), "Dismiss buttons and blank footer space must not start a drag")

        _ = NSApplication.shared
        NSApp.setActivationPolicy(.accessory)
        let toast = ContinuationToast()
        defer { toast.closeAll() }
        var displayed: [String] = []
        toast.onDidPresentNotice = { displayed.append(($0.sessionID ?? "") + ":" + ($0.recoveryKey ?? "")) }
        let screen = NSScreen.main?.visibleFrame ?? mainScreen
        let anchor = NSRect(x: screen.midX, y: screen.midY, width: 100, height: 100)
        func present(_ notice: ContinuationNotice) {
            toast.show(notice, anchor: anchor, cancel: {}, permission: {})
        }
        func notice(_ session: String, _ round: String) -> ContinuationNotice {
            ContinuationNotice(title: "Codex · 完成待验收", conversation: "隔离验证", message: "这个项目已完结，请验收",
                isCompletion: true, sessionID: session, recoveryKey: round, project: "/tmp/隔离验证")
        }
        expect(toast.popupEnabled, "Popups must remain enabled by default")
        toast.popupEnabled = false
        present(notice("a", "1"))
        present(notice("b", "1"))
        present(notice("b", "2"))
        expect(displayed.isEmpty, "Hidden notices must queue without a presentation callback")
        toast.popupEnabled = true
        expect(displayed == ["a:1"], "Enabling popups must display the retained current notice")
        guard let panel = NSApp.windows.first(where: { $0.title == "AI 监督提醒" }) else { fatalError("Production toast did not create its panel") }
        expect(panel.isVisible, "The retained current notice must be visible")
        expect(panel.frame.width < 250, "A single completion card must be narrower than the former 250-point card")
        // Dispatch through the production NSPanel event entry, bypassing SwiftUI view hit testing.
        let beforeDrag = panel.frame
        let downPoint = NSPoint(x: 100, y: beforeDrag.height - 24)
        func mouse(_ type: NSEvent.EventType, _ point: NSPoint) -> NSEvent {
            NSEvent.mouseEvent(with: type, location: point, modifierFlags: [],
                timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: panel.windowNumber,
                context: nil, eventNumber: 0, clickCount: 1, pressure: 1)!
        }
        panel.sendEvent(mouse(.leftMouseDown, downPoint))
        panel.sendEvent(mouse(.leftMouseDragged, NSPoint(x: downPoint.x + 40, y: downPoint.y + 30)))
        expect(panel.frame.minX == beforeDrag.minX + 40 && panel.frame.minY == beforeDrag.minY + 30,
               "Native window event dispatch must move the panel by the pointer's screen delta")
        var progressDuringDrag = notice("a", "1")
        progressDuringDrag.message = "拖动期间更新内容"
        let whileDragging = panel.frame
        present(progressDuringDrag)
        expect(panel.frame == whileDragging, "An update while the mouse is held must not replace or reposition the dragging panel")
        panel.sendEvent(mouse(.leftMouseUp, downPoint))
        expect(panel.frame.minX == whileDragging.minX && panel.frame.maxY == whileDragging.maxY,
               "Releasing the pointer must preserve the dragged position while applying queued content")
        let draggedTopLeft = NSPoint(x: panel.frame.minX, y: panel.frame.maxY)
        toast.popupEnabled = false
        expect(!panel.isVisible, "Disabling popups must immediately hide the existing panel")
        toast.popupEnabled = true
        expect(displayed == ["a:1"], "A show/hide setting switch must not replay the presentation event")
        var updated = notice("a", "1")
        updated.message = "原会话暂未读取到，请在对应应用中打开"
        present(updated)
        expect(displayed == ["a:1"], "Same-key message updates must not repeat the event")
        toast.close()
        expect(displayed == ["a:1", "b:2"], "Closing the first notice must reveal only the latest unread round for the next session")
        expect(panel.frame.minX == draggedTopLeft.x && panel.frame.maxY == draggedTopLeft.y,
               "The next notice and its different height must retain the dragged top-left position")
        toast.close()
        expect(!panel.isVisible, "An empty queue must hide its panel")
        present(notice("b", "1"))
        expect(!panel.isVisible && displayed.count == 2, "Acknowledgment must continue suppressing earlier unread completion keys for that session")
        toast.popupEnabled = false
        present(notice("c", "1"))
        present(notice("d", "1"))
        toast.close()
        expect(displayed.count == 2 && !panel.isVisible, "Advancing the queue while hidden must remain silent and hidden")
        toast.popupEnabled = true
        expect(displayed.last == "d:1" && displayed.count == 3, "The remaining unread session must reappear when enabled")
        toast.closeAll()
        present(notice("d", "1"))
        expect(!panel.isVisible && displayed.count == 3, "Dismiss-all must still acknowledge retained completion rounds")
        print("Toast geometry and presentation checks passed: \(count)")
    }
}

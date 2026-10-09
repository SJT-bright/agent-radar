import AppKit

@main
struct HoverMenuChecks {
    static func main() {
        var checks = 0
        func check(_ condition: Bool, _ message: String) {
            precondition(condition, message); checks += 1
        }
        let screen = NSRect(x: 0, y: 0, width: 1440, height: 875)
        let size = NSSize(width: 285, height: 670)
        let anchors = [
            NSRect(x: 1240, y: 820, width: 30, height: 26),
            NSRect(x: 6, y: 6, width: 30, height: 26),
            NSRect(x: 1400, y: 400, width: 30, height: 26),
            NSRect(x: 6, y: 400, width: 30, height: 26)
        ]
        for anchor in anchors {
            let menu = HoverSettingsPopover.rootFrame(size: size, anchor: anchor, screen: screen)
            check(screen.insetBy(dx: 8, dy: 8).contains(menu), "root menu remains inside current visible screen")
            check(!menu.intersects(anchor), "a tall menu must not cover its hover trigger")
            let row = NSRect(x: menu.minX, y: menu.midY, width: menu.width, height: 28)
            let child = HoverSettingsPopover.submenuFrame(size: NSSize(width: 260, height: 150), row: row, parent: menu, screen: screen)
            check(screen.insetBy(dx: 8, dy: 8).contains(child), "submenu remains inside current screen")
            check(!child.intersects(menu), "submenu flips sides before covering parent")
        }
        let secondScreen = NSRect(x: -1280, y: 200, width: 1280, height: 720)
        let menu = HoverSettingsPopover.rootFrame(size: NSSize(width: 300, height: 600), anchor: NSRect(x: -100, y: 850, width: 30, height: 26), screen: secondScreen)
        check(secondScreen.insetBy(dx: 8, dy: 8).contains(menu), "negative origin secondary display placement is valid")
        print("hover menu placement checks: \(checks) passed")
    }
}

import AppKit
import Foundation

/// Runs the production toast with fixed mock notices. No Store, collector or sender is created.
final class PreviewDelegate: NSObject, NSApplicationDelegate {
    let toast = ContinuationToast()
    var window: NSWindow!
    var status: NSTextField!
    var retryCount = 0
    var round = 0
    let receipt = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("agentradar-preview-events.log")
    func log(_ message: String) {
        let data = Data((message + "\n").utf8)
        if let handle = try? FileHandle(forWritingTo: receipt) {
            _ = try? handle.seekToEnd(); try? handle.write(contentsOf: data); try? handle.close()
        } else { try? data.write(to: receipt) }
        print(message); fflush(stdout)
    }
    var anchor: NSRect { NSRect(x: 750, y: 500, width: 200, height: 200) }
    func applicationDidFinishLaunching(_ note: Notification) {
        try? FileManager.default.removeItem(at: receipt)
        log("preview started; no Store, collector or sender")
        NSApp.setActivationPolicy(.regular)
        window = NSWindow(contentRect: NSRect(x: 760, y: 250, width: 380, height: 220), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "任务雷达 · 隔离交互验收"
        let content = NSView(frame: NSRect(x: 0, y: 0, width: 380, height: 220))
        let actions: [(String, Selector)] = [("1. 显示可恢复提醒", #selector(recovery)), ("2. 连续加入三个完成提醒", #selector(completions)), ("3. 加入自动倒计时", #selector(countdown)), ("4. 一键清除全部提醒", #selector(dismissAllNow)), ("5. 退出隔离预览", #selector(quit))]
        for (i, item) in actions.enumerated() {
            let button = NSButton(title: item.0, target: self, action: item.1)
            button.frame = NSRect(x: 15, y: 170 - i * 36, width: 345, height: 30)
            content.addSubview(button)
        }
        status = NSTextField(labelWithString: "回调次数：0；不会连接真实 AI")
        status.frame = NSRect(x: 15, y: 10, width: 345, height: 24)
        content.addSubview(status); window.contentView = content
        let menuBar = NSMenu(), rootItem = NSMenuItem()
        let testMenu = NSMenu(title: "验收")
        let menuActions: [(String, Selector, String)] = [("显示恢复", #selector(recovery), "1"), ("加入三个完成提醒", #selector(completions), "2"), ("加入倒计时", #selector(countdown), "3"), ("一键清除全部", #selector(dismissAllNow), "4"), ("退出隔离预览", #selector(quit), "0")]
        for action in menuActions {
            let item = NSMenuItem(title: action.0, action: action.1, keyEquivalent: action.2)
            item.target = self; testMenu.addItem(item)
        }
        rootItem.title = "验收"; rootItem.submenu = testMenu; menuBar.addItem(rootItem); NSApp.mainMenu = menuBar
        window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps: true)
        recovery()
    }
    func present(_ notice: ContinuationNotice) {
        toast.show(notice, anchor: anchor, cancel: {}, permission: {}, retry: { [weak self] sid, key in
            guard let self = self else { return }
            self.retryCount += 1
            self.status.stringValue = "回调次数：\(self.retryCount)；\(sid) / \(key)"
            self.log("retry callback \(self.retryCount) \(sid) \(key)")
            var busy = notice; busy.canRetry = false; busy.isRecovering = true; busy.cancellable = true; busy.message = "隔离预览：模拟恢复中，按钮应旋转且不可再次点击"
            self.present(busy)
            DispatchQueue.main.asyncAfter(deadline: .now() + 8) {
                var done = notice; done.canRetry = false; done.message = "隔离预览已完成；真实 AI 未被操作"
                self.present(done)
            }
        }, cancelRecovery: { [weak self] sid, key in
            self?.status.stringValue = "精确取消：\(sid) / \(key)"
            self?.log("cancel scoped \(sid) \(key)")
        })
    }
    @objc func recovery() {
        round += 1
        present(ContinuationNotice(title: "Grok · 提醒预览 · 已中断", conversation: "隔离交互测试", message: "点击左上角旋转图标恢复此条会话。此预览只统计回调，不会发送。", sessionID: "preview:recovery", recoveryKey: "preview:\(round)", canRetry: true))
    }
    @objc func completions() {
        for i in 1...3 {
            present(ContinuationNotice(title: "提醒预览项目 \(i) · 完成待验收", conversation: "独立项目 \(i)", message: "这个项目已完结，请验收", isCompletion: true, sessionID: "preview:project\(i)", recoveryKey: "complete:\(round)", canRetry: true))
        }
    }
    @objc func dismissAllNow() {
        toast.closeAll()
        status.stringValue = "已一键清除当前与全部排队提醒；完成验收标记已记录"
        log("dismiss all acknowledged; toast queue emptied")
    }
    @objc func countdown() {
        present(ContinuationNotice(title: "自动恢复 · 隔离预览", conversation: "新倒计时", message: "本条不应覆盖未读完成提醒", cancellable: true, sessionID: "preview:countdown", recoveryKey: "countdown:\(round)"))
    }
    @objc func quit() { NSApp.terminate(nil) }
}
@main struct PreviewMain {
    static func main() {
        let app = NSApplication.shared, delegate = PreviewDelegate()
        app.delegate = delegate; app.run()
    }
}

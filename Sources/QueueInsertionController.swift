import AppKit
import ApplicationServices
import Darwin
import Foundation

/// Watches only the foreground AI window. A queue control is pressed once when
/// it appears after a prior observation without one; existing queued work is
/// treated as the baseline when the app or window is first seen.
final class QueueInsertionController {
    var onResult: ((String) -> Void)?
    private let worker = DispatchQueue(label: "radar.queue-insertion", qos: .userInitiated)
    private var timer: Timer?
    private var scanning = false
    private let stateLock = NSLock()
    private var observed: [String: Bool] = [:]
    private var enabled = true
    private var paused = false
    private static let bundles: Set<String> = [
        "com.openai.codex", "dev.zcode.app", "com.qoder.app", "com.qodercn.app",
        "com.tencent.workbuddy.mac", "com.workbuddy.workbuddy-ai",
        "com.grokapp.desktop", "com.zhipuai.autoclaw"
    ]

    func start(enabled: Bool) {
        stateLock.lock()
        self.enabled = enabled
        stateLock.unlock()
        timer?.invalidate()
        let timer = Timer(timeInterval: 0.65, repeats: true) { [weak self] _ in self?.tick() }
        self.timer = timer
        RunLoop.main.add(timer, forMode: .common)
        tick()
    }

    func configure(enabled: Bool, paused: Bool) {
        stateLock.lock()
        self.enabled = enabled
        self.paused = paused
        observed.removeAll()
        stateLock.unlock()
        if enabled && !paused { tick() }
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        stateLock.lock()
        enabled = false
        observed.removeAll()
        stateLock.unlock()
    }

    private func tick() {
        stateLock.lock()
        let active = enabled && !paused
        stateLock.unlock()
        guard active, !scanning, AXIsProcessTrusted(),
              let app = NSWorkspace.shared.frontmostApplication,
              let bundle = app.bundleIdentifier?.lowercased(), Self.bundles.contains(bundle) else { return }
        scanning = true
        let pid = app.processIdentifier
        worker.async { [weak self] in
            let result = self?.inspect(pid: pid, bundle: bundle)
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.scanning = false
                if let result { self.onResult?(result) }
            }
        }
    }

    private func inspect(pid: pid_t, bundle: String) -> String? {
        guard let current = NSWorkspace.shared.frontmostApplication,
              current.processIdentifier == pid, current.bundleIdentifier?.lowercased() == bundle else { return nil }
        let app = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(app, 0.12)
        guard let windows = attribute(app, kAXWindowsAttribute as String) as? [AXUIElement],
              !windows.isEmpty else { return nil }
        let focusedValue = attribute(app, kAXFocusedWindowAttribute as String)
        let focused: AXUIElement? = focusedValue.flatMap {
            CFGetTypeID($0) == AXUIElementGetTypeID() ? ($0 as! AXUIElement) : nil
        }
        guard let window = focused.flatMap({ candidate in windows.first { CFEqual($0, candidate) } })
                ?? (windows.count == 1 ? windows[0] : nil) else { return nil }
        AXUIElementSetMessagingTimeout(window, 0.12)
        let windowKey = "\(pid):\(CFHash(window))"
        var stack = [(window, 0)]
        var visited = Set<CFHashCode>()
        var buttons: [AXUIElement] = []
        var urls: [String] = []
        var headings: [String] = []
        var composers: [CFHashCode] = []
        let deadline = ProcessInfo.processInfo.systemUptime + 2.0
        while let (node, depth) = stack.popLast(), visited.count < 1800,
              ProcessInfo.processInfo.systemUptime < deadline {
            guard visited.insert(CFHash(node)).inserted else { continue }
            AXUIElementSetMessagingTimeout(node, 0.12)
            let role = attribute(node, kAXRoleAttribute as String) as? String ?? ""
            if role == kAXButtonRole as String && buttonIsInsert(node) { buttons.append(node) }
            if role == "AXWebArea", let rawURL = attribute(node, "AXURL") {
                let url = (rawURL as? URL)?.absoluteString ?? (rawURL as? String) ?? ""
                if !url.isEmpty { urls.append(url) }
            }
            if role == kAXHeadingRole as String, headings.count < 3 {
                let heading = (attribute(node, kAXTitleAttribute as String) as? String)
                    ?? (attribute(node, kAXDescriptionAttribute as String) as? String)
                    ?? (attribute(node, kAXChildrenAttribute as String) as? [AXUIElement])?
                        .compactMap { attribute($0, kAXValueAttribute as String) as? String }.first
                    ?? ""
                if !heading.isEmpty { headings.append(heading) }
            }
            if (role == kAXTextAreaRole as String || role == kAXTextFieldRole as String),
               (attribute(node, kAXEnabledAttribute as String) as? NSNumber)?.boolValue == true {
                composers.append(CFHash(node))
            }
            if depth < 30, role != kAXStaticTextRole as String,
               let children = attribute(node, kAXChildrenAttribute as String) as? [AXUIElement] {
                for child in children.prefix(200).reversed() { stack.append((child, depth + 1)) }
            }
        }
        // Partial trees and ambiguous controls never authorize a click.
        guard stack.isEmpty, ProcessInfo.processInfo.systemUptime < deadline else { return nil }
        guard let key = Self.contextKey(window: windowKey, urls: urls,
                                        headings: headings, composers: composers) else { return nil }
        // The send bridge holds this same lock while it checks its own newly
        // queued message. Do not race its press or consume the new baseline.
        let lockPath = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/AgentRadar/continuation.lock").path
        let fd = open(lockPath, O_CREAT | O_RDWR, 0o600)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        guard flock(fd, LOCK_EX | LOCK_NB) == 0 else { return nil }
        defer { flock(fd, LOCK_UN) }
        let present = !buttons.isEmpty
        stateLock.lock()
        let wasPresent = observed[key]
        observed = [key: present]
        let active = enabled && !paused
        stateLock.unlock()
        guard active, Self.shouldInsert(previouslyPresent: wasPresent, count: buttons.count) else { return nil }
        guard NSWorkspace.shared.frontmostApplication?.processIdentifier == pid,
              buttonIsInsert(buttons[0]) else { return nil }
        return AXUIElementPerformAction(buttons[0], kAXPressAction as CFString) == .success
            ? "已自动点击当前对话的新消息「插队」"
            : "发现新排队消息，但「插队」未能点击"
    }

    private func buttonIsInsert(_ node: AXUIElement) -> Bool {
        guard (attribute(node, kAXEnabledAttribute as String) as? NSNumber)?.boolValue == true else { return false }
        let names = [kAXTitleAttribute as String, kAXDescriptionAttribute as String,
                     kAXHelpAttribute as String].compactMap { attribute(node, $0) as? String }
        return names.contains { $0.trimmingCharacters(in: .whitespacesAndNewlines) == "插队" }
    }

    static func shouldInsert(previouslyPresent: Bool?, count: Int) -> Bool {
        previouslyPresent == false && count == 1
    }

    static func contextKey(window: String, urls: [String], headings: [String],
                           composers: [CFHashCode]) -> String? {
        let conversationURLs = urls.filter {
            $0.contains("#/chat/") || $0.contains("/threads/") || $0.contains("/conversation/")
        }
        if !conversationURLs.isEmpty {
            return window + ":url:" + conversationURLs.sorted().joined(separator: "|")
        }
        guard !composers.isEmpty else { return nil }
        let title = headings.first { !["命令面板", "工作区", "任务", "ChatGPT"].contains($0) } ?? ""
        return window + ":ui:" + title + ":" + composers.sorted().map(String.init).joined(separator: ",")
    }

    private func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
        var value: CFTypeRef?
        return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
    }
}

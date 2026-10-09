import AppKit
import ApplicationServices
import Darwin
import Foundation

/// Read-only discovery. Call scan from the collector queue, never from the main queue.
/// Accessibility reads are bounded; discovery never activates an application.
enum SessionActivationResult { case unavailable, application, workspaceLink, conversationLink }

final class NativeMonitor {
    private var discoveredBundles = Set(UserDefaults.standard.stringArray(forKey: "autoDiscoveredAIBundles.v1") ?? [])
    private var discoveryCursor = 0
    // 上一轮每个应用（按 pid）的真实窗口行：本轮预算耗尽时原样复用，
    // 避免每次都生成新占位 ID 使监督/置顶等以会话 ID 为键的状态翻转。
    private var lastWindowRows: [Int32: [SessionRecord]] = [:]
    private struct Definition {
        let id: String
        let name: String
    }

    private struct Budget {
        let end: TimeInterval
        var available: Bool { ProcessInfo.processInfo.systemUptime < end }
    }

    private static let definitions: [String: Definition] = [
        "com.openai.codex": .init(id: "codex", name: "Codex"),
        "com.openai.chat": .init(id: "chatgpt", name: "ChatGPT"),
        "dev.zcode.app": .init(id: "zcode", name: "ZCode"),
        "com.tencent.workbuddy.mac": .init(id: "workbuddy", name: "WorkBuddy"),
        "com.workbuddy.workbuddy-ai": .init(id: "workbuddy-ai", name: "WorkBuddy AI"),
        "com.anthropic.claudefordesktop": .init(id: "claude", name: "Claude"),
        "com.anthropic.claude": .init(id: "claude", name: "Claude"),
        "com.todesktop.230313mzl4w4u92": .init(id: "cursor", name: "Cursor"),
        "com.google.antigravity": .init(id: "antigravity", name: "Antigravity"),
        // Google's native macOS app uses GeminiMacOS, not a browser/PWA id.
        "com.google.geminimacos": .init(id: "gemini", name: "Gemini"),
        "cn.trae.solo.app": .init(id: "trae-solo-cn", name: "TRAE SOLO CN"),
        "com.trae.app": .init(id: "trae", name: "TRAE"),
        "cn.trae.app": .init(id: "trae-cn", name: "TRAE CN"),
        "com.qoder.app": .init(id: "qoder", name: "Qoder"),
        "com.qodercn.app": .init(id: "qoder-cn", name: "Qoder CN"),
        "bot.cline.app": .init(id: "cline", name: "Cline"),
        "com.bot.pc.doubao": .init(id: "doubao", name: "豆包"),
        "com.moonshot.kimichat": .init(id: "kimi", name: "Kimi"),
        "com.grokapp.desktop": .init(id: "grok", name: "Grok"),
        "com.anysphere.sand": .init(id: "grok-bot", name: "Grok Bot"),
        "com.alibaba.tongyi": .init(id: "qianwen", name: "千问"),
        "com.tencent.yuanbao": .init(id: "yuanbao", name: "元宝"),
        "ai.openclaw.mac": .init(id: "openclaw", name: "OpenClaw"),
        "com.electron.ollama": .init(id: "ollama", name: "Ollama"),
        "com.nousresearch.hermes.setup": .init(id: "hermes", name: "Hermes"),
        "com.nousresearch.hermes": .init(id: "hermes", name: "Hermes"),
        "com.zhipuai.autoclaw": .init(id: "autoclaw", name: "AutoClaw"),
        "com.minimax.hub.global": .init(id: "minimax", name: "MiniMax Design"),
        "cn.coze.desktop": .init(id: "coze", name: "扣子"),
        "com.exafunction.windsurf": .init(id: "windsurf", name: "Windsurf"),
        "com.codeium.windsurf": .init(id: "windsurf", name: "Windsurf"),
        "ai.lmstudio.app": .init(id: "lmstudio", name: "LM Studio")
    ]

    private static let knownNames: [String: Definition] = {
        var result: [String: Definition] = [:]
        for definition in definitions.values {
            result[definition.name.lowercased()] = definition
        }
        result["doubao"] = .init(id: "doubao", name: "豆包")
        result["qianwen"] = .init(id: "qianwen", name: "千问")
        result["trae solo"] = .init(id: "trae-solo", name: "TRAE SOLO")
        return result
    }()

    private static let browserIDs: Set<String> = [
        "com.google.chrome", "com.google.chrome.beta", "com.google.chrome.dev",
        "com.google.chrome.canary", "com.apple.safari", "com.apple.safaritechnologypreview",
        "com.microsoft.edgemac", "com.brave.browser", "company.thebrowser.browser",
        "org.mozilla.firefox", "org.chromium.chromium", "com.operasoftware.opera"
    ]

    private static let localAdapterIDs: Set<String> = ["codex", "zcode", "workbuddy", "workbuddy-ai", "autoclaw", "grok", "qoder", "qoder-cn", "cline"]

    /// Exact bundle definitions take precedence over a localized application
    /// name. Recognition discovers an app; it grants no conversation status.
    static func knownApplication(bundleID: String, name: String) -> (id: String, name: String)? {
        let definition = definitions[bundleID.lowercased()] ?? knownNames[name.lowercased()]
        return definition.map { ($0.id, $0.name) }
    }

    /// Metadata is a discovery hint only, never proof of an active conversation.
    static func hasAIMetadata(bundleID: String, name: String) -> Bool {
        guard !bundleID.lowercased().hasPrefix("com.apple.") else { return false }
        let tokens = Set((bundleID + " " + name).lowercased().components(separatedBy: CharacterSet.alphanumerics.inverted))
        guard !tokens.contains("helper"), !tokens.isSuperset(of: ["computer", "use"]),
              !tokens.isSuperset(of: ["dock", "extra"]) else { return false }
        if !tokens.isDisjoint(with: ["ai", "llm", "gpt", "chatgpt", "copilot", "agent", "assistant"]) { return true }
        return ["智能体", "AI助手", "AI 助手", "大模型"].contains(where: name.contains)
    }

    static func discoveryBatch(_ candidates: [String], cursor: Int, limit: Int = 2) -> [String] {
        guard !candidates.isEmpty, limit > 0 else { return [] }
        return (0..<min(limit, candidates.count)).map { candidates[(max(0, cursor) + $0) % candidates.count] }
    }

    /// Returns all recognized running applications, including ones without readable windows.
    /// A running application alone is never evidence that an AI turn is running.
    func scan(extraBundleIDs: [String] = []) -> NativeScan {
        let trusted = AXIsProcessTrusted()
        let extras = Set(extraBundleIDs.map { $0.lowercased() })
        let deadline = Budget(end: ProcessInfo.processInfo.systemUptime + 3.2)
        var sessions: [SessionRecord] = []
        var apps: [AppRecord] = []
        let running = NSWorkspace.shared.runningApplications
            .filter { !$0.isTerminated && $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }
            .sorted {
                // Window-only apps must not wait behind apps with local adapters.
                let left = Self.localAdapterIDs.contains(Self.definitions[($0.bundleIdentifier ?? "").lowercased()]?.id ?? "")
                let right = Self.localAdapterIDs.contains(Self.definitions[($1.bundleIdentifier ?? "").lowercased()]?.id ?? "")
                if left != right { return !left }
                return ($0.localizedName ?? "") < ($1.localizedName ?? "")
            }

        // Rotate through previously unknown GUI apps. Only button labels and
        // window titles are read; no input values, screenshots or chat bodies.
        // Reserve a small budget so existing integrations cannot starve discovery.
        let known = running.filter { application in
            let key = (application.bundleIdentifier ?? "").lowercased()
            return Self.knownApplication(bundleID: key, name: application.localizedName ?? "") != nil
                || Self.browserIDs.contains(key) || extras.contains(key) || discoveredBundles.contains(key)
                || (application.activationPolicy == .regular && Self.hasAIMetadata(bundleID: key, name: application.localizedName ?? ""))
        }
        let knownPIDs = Set(known.map(\.processIdentifier))
        let unknown = running.filter { application in
            guard let key = application.bundleIdentifier?.lowercased(), !key.isEmpty else { return false }
            return application.activationPolicy == .regular && !knownPIDs.contains(application.processIdentifier)
                && !key.hasPrefix("com.apple.")
        }
        let probeIDs = trusted ? Self.discoveryBatch(unknown.compactMap { $0.bundleIdentifier?.lowercased() }, cursor: discoveryCursor) : []
        discoveryCursor = unknown.isEmpty ? 0 : (discoveryCursor + probeIDs.count) % unknown.count
        let probes = unknown.filter { probeIDs.contains(($0.bundleIdentifier ?? "").lowercased()) }

        for application in known + probes {
            guard let bundleID = application.bundleIdentifier, !bundleID.isEmpty else { continue }
            let bundleKey = bundleID.lowercased()
            let nativeDefinition = Self.knownApplication(bundleID: bundleKey, name: application.localizedName ?? "")
            let browser = Self.browserIDs.contains(bundleKey)
            let manuallyAdded = extras.contains(bundleKey)
            let probing = probeIDs.contains(bundleKey)
            // Helpers are not registered as separate AI applications unless explicitly added.
            guard application.activationPolicy != .prohibited || manuallyAdded else { continue }

            let definition = nativeDefinition.map { Definition(id: $0.id, name: $0.name) } ?? Definition(
                id: browser ? "browser-\(bundleKey)" : "custom-\(bundleKey)",
                name: application.localizedName ?? bundleID
            )
            let app = AppRecord(id: definition.id, name: definition.name, bundleID: bundleID,
                                pid: application.processIdentifier, path: application.bundleURL?.path ?? "")
            if (!browser || manuallyAdded) && !probing { apps.append(app) }

            guard trusted else {
                if !browser || manuallyAdded {
                    sessions.append(placeholder(app, evidence: "已发现应用；授予辅助功能权限后可读取窗口，会话状态待确认"))
                }
                continue
            }
            let windowDeadline = probing ? deadline.end : deadline.end - (probes.isEmpty ? 0 : 0.6)
            guard ProcessInfo.processInfo.systemUptime < windowDeadline else {
                if (!browser || manuallyAdded) && !probing {
                    sessions.append(contentsOf: Self.carriedRows(previous: lastWindowRows[app.pid], app: app,
                                                                 note: "本轮读取时间已用尽；显示上一轮窗口读取结果"))
                }
                continue
            }

            let axApplication = AXUIElementCreateApplication(app.pid)
            AXUIElementSetMessagingTimeout(axApplication, 0.06)
            let appBudget = Budget(end: min(windowDeadline, ProcessInfo.processInfo.systemUptime + (probing ? 0.25 : 0.45)))
            let windows = elements(attribute(axApplication, kAXWindowsAttribute as String))
            if windows.isEmpty {
                if (!browser || manuallyAdded) && !probing {
                    sessions.append(placeholder(app, evidence: "已发现应用；没有可读取的窗口，不能确认会话状态"))
                }
                continue
            }

            var observed = 0
            for window in windows.prefix(10) {
                guard appBudget.available else { break }
                let windowTitle = string(attribute(window, kAXTitleAttribute as String))
                let webService = browser ? Self.browserService(windowTitle) : nil
                guard !browser || webService != nil || manuallyAdded else { continue }
                let sessionAppID = webService.map { "web-\($0.id)" } ?? app.id
                let sessionAppName = webService.map { "\($0.name) · \(app.name)" } ?? app.name
                let state = readStatus(window, appID: sessionAppID, budget: appBudget)
                // Generic send/stop buttons and arbitrary window text do not
                // qualify a previously unknown program as an AI application.
                guard !probing || state.status == "running" else { continue }
                if probing && discoveredBundles.insert(bundleKey).inserted {
                    UserDefaults.standard.set(discoveredBundles.sorted(), forKey: "autoDiscoveredAIBundles.v1")
                    apps.append(app)
                }
                let number = (attribute(window, "AXWindowNumber") as? NSNumber)?.intValue
                // AXUIElement's CF identity remains stable when a window title changes.
                // A window identity represents the exposed window, not unseen tabs or chats.
                let key = number.map(String.init) ?? "ax-\(CFHash(window))"
                let title = state.title ?? (windowTitle.isEmpty ? "\(sessionAppName) · 未命名窗口" : windowTitle)
                var evidence = state.evidence
                if browser {
                    evidence += "；仅覆盖标题已识别的浏览器窗口，不代表所有后台标签页"
                }
                sessions.append(SessionRecord(
                    id: "window:\(app.id):\(app.pid):\(key)", app_id: sessionAppID,
                    app_name: sessionAppName, title: title, project: "", status: state.status,
                    evidence: evidence, updated_at: Date().timeIntervalSince1970,
                    source: "window", target: "", pid: app.pid, window_id: number
                ))
                observed += 1
            }
            if browser && observed > 0 && !manuallyAdded { apps.append(app) }
            if !browser && !probing && observed == 0 {
                sessions.append(contentsOf: Self.carriedRows(previous: lastWindowRows[app.pid], app: app,
                                                             note: "窗口读取达到时间限制；显示上一轮窗口读取结果"))
            }
        }
        var grouped: [Int32: [SessionRecord]] = [:]
        for row in sessions {
            guard row.source == "window", row.id.hasPrefix("window:"), let pid = row.pid else { continue }
            grouped[pid, default: []].append(row)
        }
        lastWindowRows = grouped
        return NativeScan(sessions: sessions, apps: apps, accessibility: trusted)
    }

    /// Called only in response to the user clicking a session. It never types or sends messages.
    /// Completion runs on the main thread. Success means the containing application (or a
    /// verified deep link) opened; it does not promise a particular CLI tab was selected.
    func activate(_ session: SessionRecord, completion: ((SessionActivationResult) -> Void)? = nil) {
        guard Thread.isMainThread else {
            DispatchQueue.main.async { [self] in activate(session, completion: completion) }
            return
        }
        if session.app_id == "claude-code" {
            activateCLIContainer(pid: session.pid) { completion?($0 ? .application : .unavailable) }
            return
        }
        let verifiedHandlers = ["codex": "com.openai.codex", "workbuddy": "com.tencent.workbuddy.mac",
                                "zcode": "dev.zcode.app"]
        if let url = Self.safeDeepLink(session.target, appID: session.app_id),
           let handlerURL = NSWorkspace.shared.urlForApplication(toOpen: url),
           Bundle(url: handlerURL)?.bundleIdentifier == verifiedHandlers[session.app_id] {
            let opened = NSWorkspace.shared.open(url)
            completion?(opened ? (session.app_id == "zcode" ? .workspaceLink : .conversationLink) : .unavailable)
            return
        }
        let candidates = NSWorkspace.shared.runningApplications
        let matchesApp: (NSRunningApplication) -> Bool = { app in
                guard let bundle = app.bundleIdentifier else { return false }
                return Self.knownApplication(bundleID: bundle, name: app.localizedName ?? "")?.id == session.app_id
                    || "custom-\(bundle.lowercased())" == session.app_id
                    || "browser-\(bundle.lowercased())" == session.app_id
                    || (session.app_id.hasPrefix("web-") && Self.browserIDs.contains(bundle.lowercased()))
            }
        let pidApplication = session.pid.flatMap { NSRunningApplication(processIdentifier: $0) }
        let application = pidApplication.flatMap { matchesApp($0) ? $0 : nil }
            ?? (session.app_id.hasPrefix("web-") ? nil : candidates.first(where: matchesApp))
        guard let application else { completion?(.unavailable); return }
        let activated = application.activate(options: [.activateAllWindows])
        completion?(activated ? .application : .unavailable)
        guard AXIsProcessTrusted() else { return }
        DispatchQueue.global(qos: .userInitiated).async { [self] in
            let axApplication = AXUIElementCreateApplication(application.processIdentifier)
            AXUIElementSetMessagingTimeout(axApplication, 0.06)
            let budget = Budget(end: ProcessInfo.processInfo.systemUptime + 0.45)
            let windows = elements(attribute(axApplication, kAXWindowsAttribute as String)).prefix(10)
            // Only raise a unique match. If the title changed, activation is application-level.
            var matching: [AXUIElement] = []
            for window in windows {
                guard budget.available else { return }
                if let requested = session.window_id,
                   let actual = attribute(window, "AXWindowNumber") as? NSNumber,
                   actual.intValue == requested {
                    matching.append(window)
                } else if string(attribute(window, kAXTitleAttribute as String)) == session.title {
                    matching.append(window)
                }
            }
            if matching.count == 1, let window = matching.first {
                AXUIElementPerformAction(window, kAXRaiseAction as CFString)
            }
        }
    }

    private func activateCLIContainer(pid: Int32?, completion: ((Bool) -> Void)?) {
        guard let pid, pid > 1 else { completion?(false); return }
        let guiApplications = NSWorkspace.shared.runningApplications.filter {
            !$0.isTerminated && $0.activationPolicy != .prohibited && $0.bundleIdentifier != nil
                && $0.processIdentifier != ProcessInfo.processInfo.processIdentifier
        }
        let guiPIDs = Set(guiApplications.map(\.processIdentifier))
        DispatchQueue.global(qos: .userInitiated).async {
            let parents = Self.readProcessParents()
            let ancestor = parents.flatMap { Self.nearestGUIAncestor(pid: pid, parents: $0, guiPIDs: guiPIDs) }
            DispatchQueue.main.async {
                guard let ancestor,
                      let application = guiApplications.first(where: { $0.processIdentifier == ancestor }),
                      !application.isTerminated else { completion?(false); return }
                let activated = application.activate(options: [.activateAllWindows])
                completion?(activated)
            }
        }
    }

    /// Reads only numeric process relationships, on explicit activation and off the UI queue.
    /// The child process and pipe are bounded by time and bytes; no process arguments are read.
    static func readProcessParents() -> [Int32: Int32]? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/ps")
        process.arguments = ["-axo", "pid=,ppid="]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do { try process.run() } catch {
            pipe.fileHandleForReading.closeFile()
            pipe.fileHandleForWriting.closeFile()
            return nil
        }
        pipe.fileHandleForWriting.closeFile()
        defer {
            if process.isRunning {
                process.terminate()
                if process.isRunning { _ = Darwin.kill(process.processIdentifier, SIGKILL) }
            }
            pipe.fileHandleForReading.closeFile()
        }
        let descriptor = pipe.fileHandleForReading.fileDescriptor
        let flags = fcntl(descriptor, F_GETFL)
        guard flags >= 0, fcntl(descriptor, F_SETFL, flags | O_NONBLOCK) >= 0 else { return nil }
        let deadline = ProcessInfo.processInfo.systemUptime + 1.0
        var output = Data()
        var buffer = [UInt8](repeating: 0, count: 8192)
        while ProcessInfo.processInfo.systemUptime < deadline {
            let count = Darwin.read(descriptor, &buffer, buffer.count)
            if count > 0 {
                guard output.count + count <= 262_144 else { return nil }
                output.append(contentsOf: buffer.prefix(count))
            } else if count < 0 && errno != EAGAIN && errno != EINTR {
                return nil
            } else if !process.isRunning {
                guard process.terminationStatus == 0, let text = String(data: output, encoding: .utf8) else { return nil }
                return processParentMap(text)
            }
            var descriptorToPoll = pollfd(fd: descriptor, events: Int16(POLLIN), revents: 0)
            let remainingMS = Int32(max(0, min(20, (deadline - ProcessInfo.processInfo.systemUptime) * 1000)))
            _ = Darwin.poll(&descriptorToPoll, 1, remainingMS)
        }
        return nil
    }

    static func processParentMap(_ text: String) -> [Int32: Int32] {
        var parents: [Int32: Int32] = [:]
        for line in text.split(separator: "\n").prefix(16_384) {
            let fields = line.split(whereSeparator: { $0 == " " || $0 == "\t" })
            guard fields.count == 2, let pid = Int32(fields[0]), let parent = Int32(fields[1]),
                  pid > 1, parent >= 0 else { continue }
            parents[pid] = parent
        }
        return parents
    }

    static func nearestGUIAncestor(pid: Int32, parents: [Int32: Int32], guiPIDs: Set<Int32>) -> Int32? {
        guard pid > 1 else { return nil }
        var current = pid
        var visited: Set<Int32> = [pid]
        for _ in 0..<32 {
            guard let parent = parents[current], parent > 1, visited.insert(parent).inserted else { return nil }
            if guiPIDs.contains(parent) { return parent }
            current = parent
        }
        return nil
    }

    static func requestAccessibility() {
        // Open the destination directly; querying or opening settings must not
        // add a second permission prompt that needs to be dismissed.
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility") {
            NSWorkspace.shared.open(url)
        }
    }

    struct AccessibilityProbe {
        let granted: Bool
        let appsProbed: Int
        let windowsRead: Int
        let sampleName: String?
    }

    /// End-to-end read-only check that the accessibility permission actually
    /// works right now: bounded to Finder plus a few regular apps, 0.3 s
    /// messaging timeout each, never activates anything or reads window content.
    static func accessibilityProbe() -> AccessibilityProbe {
        let granted = AXIsProcessTrusted()
        guard granted else { return AccessibilityProbe(granted: false, appsProbed: 0, windowsRead: 0, sampleName: nil) }
        let running = NSWorkspace.shared.runningApplications
        let finder = running.first { $0.bundleIdentifier == "com.apple.finder" }
        let others = running.filter {
            !$0.isTerminated && $0.activationPolicy == .regular
                && $0.processIdentifier != ProcessInfo.processInfo.processIdentifier
                && $0.bundleIdentifier != "com.apple.finder" && $0.bundleIdentifier != nil
        }.sorted { ($0.localizedName ?? "") < ($1.localizedName ?? "") }
        let candidates = [finder] + others.map { Optional($0) }
        var probed = 0
        var windowsRead = 0
        var sampleName: String?
        for application in candidates.compactMap({ $0 }).prefix(6) {
            let element = AXUIElementCreateApplication(application.processIdentifier)
            AXUIElementSetMessagingTimeout(element, 0.3)
            var value: CFTypeRef?
            guard AXUIElementCopyAttributeValue(element, kAXWindowsAttribute as CFString, &value) == .success else { continue }
            probed += 1
            let count = (value as? [AnyObject])?.count ?? 0
            windowsRead += count
            if sampleName == nil { sampleName = application.localizedName }
            if probed >= 2 && windowsRead > 0 { break }
        }
        return AccessibilityProbe(granted: true, appsProbed: probed, windowsRead: windowsRead, sampleName: sampleName)
    }

    private func placeholder(_ app: AppRecord, evidence: String) -> SessionRecord {
        Self.placeholderRecord(app, evidence: evidence)
    }

    static func placeholderRecord(_ app: AppRecord, evidence: String) -> SessionRecord {
        SessionRecord(id: "app:\(app.id):\(app.pid)", app_id: app.id, app_name: app.name,
                      title: "\(app.name) · 会话待识别", project: "", status: "unknown",
                      evidence: evidence, updated_at: Date().timeIntervalSince1970,
                      source: "window", target: "", pid: app.pid, window_id: nil)
    }

    /// 预算耗尽时复用上一轮该应用的真实窗口行（附说明）；无历史才退回占位。
    /// 返回的是副本，追加说明不污染下一轮复用的原始行。
    static func carriedRows(previous: [SessionRecord]?, app: AppRecord, note: String) -> [SessionRecord] {
        guard let previous = previous, !previous.isEmpty else {
            return [placeholderRecord(app, evidence: "已发现应用；" + note + "，会话状态待确认")]
        }
        var rows = previous
        for index in rows.indices { rows[index].evidence += "；" + note }
        return rows
    }

    private func readStatus(_ window: AXUIElement, appID: String, budget: Budget) -> (status: String, evidence: String, title: String?) {
        var stack = [(window, 0)]
        var visited = 0
        var seen = Set<CFHashCode>()
        var runningLabel: String?
        var waitingLabel: String?
        var conversationTitle: String?
        while let (element, depth) = stack.popLast(), visited < 240 && budget.available {
            visited += 1
            guard seen.insert(CFHash(element)).inserted else { continue }
            let values = attributes(element, [kAXRoleAttribute as String, "AXHidden", kAXChildrenAttribute as String])
            if (values[safe: 1] as? NSNumber)?.boolValue == true { continue }
            let role = string(values[safe: 0])
            if appID == "coze", role == kAXGroupRole as String {
                for value in attributes(element, [kAXTitleAttribute as String, kAXDescriptionAttribute as String]) {
                    if let title = Self.conversationHeading(appID: appID, role: role, label: string(value)) {
                        conversationTitle = title
                    }
                }
            }
            if appID == "coze", role == kAXStaticTextRole as String, depth <= 8,
               let title = Self.conversationHeading(appID: appID, role: role,
                    label: string(attribute(element, kAXValueAttribute as String)), depth: depth) {
                conversationTitle = title
            }
            if role == kAXButtonRole as String && budget.available {
                let button = attributes(element, [kAXEnabledAttribute as String, kAXTitleAttribute as String,
                                                  kAXDescriptionAttribute as String, kAXHelpAttribute as String])
                let enabled = (button[safe: 0] as? NSNumber)?.boolValue == true
                for value in button.dropFirst() {
                    let label = string(value)
                    if let status = Self.buttonStatus(label: label, enabled: enabled, appID: appID) {
                        if status == "waiting" { waitingLabel = label }
                        else { runningLabel = label }
                    }
                }
            }
            // Composer controls are normally at the end of the page. Walk those
            // first, and do not spend the entire budget on old transcript nodes.
            if depth < 24 && role != kAXStaticTextRole as String && role != kAXTextAreaRole as String {
                let children = elements(values[safe: 2])
                let selected = children.count > 40 ? Array(children.prefix(4)) + Array(children.suffix(8)) : children
                for child in selected.prefix(max(0, 400 - stack.count)) { stack.append((child, depth + 1)) }
            }
        }
        if let label = waitingLabel { return ("waiting", "可用操作按钮“\(label)”表明正在等待处理", conversationTitle) }
        if let label = runningLabel { return ("running", "可用操作按钮“\(label)”表明正在生成", conversationTitle) }
        return ("unknown", "已读取窗口；没有明确的运行或等待操作信号", conversationTitle)
    }

    static func conversationHeading(appID: String, role: String, label: String, depth: Int = 99) -> String? {
        guard appID == "coze", role == kAXGroupRole as String ||
                (role == kAXStaticTextRole as String && depth <= 8) else { return nil }
        let title = label.trimmingCharacters(in: .whitespacesAndNewlines)
        guard title.hasSuffix(" 在线"), title.count <= 80, !title.contains("\n") else { return nil }
        let name = String(title.dropLast(3)).trimmingCharacters(in: .whitespaces)
        return name.isEmpty ? nil : name
    }

    // Button-only evidence deliberately excludes chat text and generic Stop/Allow controls.
    static func buttonStatus(label: String, enabled: Bool, appID: String = "") -> String? {
        guard enabled else { return nil }
        let normalized = label.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if appID == "coze" && normalized == "中断运行" { return "running" }
        let running: Set<String> = ["stop generating", "stop generation", "stop response", "stop streaming",
                                    "stop responding", "stop thinking", "停止生成", "停止回复", "停止响应",
                                    "停止输出", "停止思考", "停止推理", "中止生成"]
        let waiting: Set<String> = ["allow once", "allow this time", "approve once", "approve and run",
                                    "允许一次", "仅允许一次", "本次允许", "批准并运行", "批准此次操作"]
        if running.contains(normalized) { return "running" }
        if waiting.contains(normalized) { return "waiting" }
        return nil
    }

    static func browserService(_ title: String) -> (id: String, name: String)? {
        var normalized = title.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        for suffix in [" - google chrome", " — google chrome", " - microsoft edge", " — mozilla firefox",
                       " - mozilla firefox", " — safari", " - safari", " - brave", " - chromium"] {
            if normalized.hasSuffix(suffix) { normalized = String(normalized.dropLast(suffix.count)) }
        }
        let names: [(String, String, [String])] = [
            ("chatgpt", "ChatGPT", ["chatgpt"]), ("claude", "Claude", ["claude"]),
            ("gemini", "Gemini", ["gemini", "google gemini"]),
            ("deepseek", "DeepSeek", ["deepseek"]), ("grok", "Grok", ["grok"]),
            ("kimi", "Kimi", ["kimi", "kimi.ai"]), ("doubao", "豆包", ["豆包", "doubao"]),
            ("qianwen", "千问", ["千问", "通义千问", "qwen chat"]),
            ("yuanbao", "元宝", ["腾讯元宝", "元宝"]), ("perplexity", "Perplexity", ["perplexity"])
        ]
        let separators = [" - ", " – ", " — ", " | ", " · ", "：", ": "]
        for (id, name, aliases) in names {
            for alias in aliases {
                if normalized == alias || separators.contains(where: {
                    normalized.hasPrefix(alias + $0) || normalized.hasSuffix($0 + alias)
                }) { return (id, name) }
            }
        }
        return nil
    }

    static func safeDeepLink(_ target: String, appID: String) -> URL? {
        guard let components = URLComponents(string: target),
              components.user == nil, components.password == nil,
              components.port == nil, components.fragment == nil else { return nil }
        if appID == "codex", components.query == nil, components.scheme == "codex", components.host == "threads" {
            let parts = components.path.split(separator: "/", omittingEmptySubsequences: false)
            guard parts.count == 2, parts[0].isEmpty, UUID(uuidString: String(parts[1])) != nil else { return nil }
            return components.url
        }
        // Verified in WorkBuddy's bundled main/index.js navigateToSession handler.
        if appID == "workbuddy", components.query == nil, components.scheme == "workbuddy", components.host == "chat" {
            let parts = components.path.split(separator: "/", omittingEmptySubsequences: false)
            guard parts.count == 2, parts[0].isEmpty, !parts[1].isEmpty, parts[1].count <= 256,
                  parts[1] != ".", parts[1] != "..",
                  parts[1].unicodeScalars.allSatisfy({ !CharacterSet.controlCharacters.contains($0) }) else { return nil }
            return components.url
        }
        // ZCode's own CLI helper generates zcode://workspace/open?path=<encoded>
        // with encodeURIComponent; the Electron main opens that absolute workspace.
        if appID == "zcode", components.scheme == "zcode", components.host == "workspace" {
            guard components.path == "/open",
                  let items = components.queryItems, items.count == 1,
                  items.first?.name == "path",
                  let workspace = items.first?.value, workspace.hasPrefix("/"),
                  workspace.count <= 512,
                  workspace.split(separator: "/").allSatisfy({ $0 != "." && $0 != ".." }),
                  workspace.unicodeScalars.allSatisfy({ !CharacterSet.controlCharacters.contains($0) })
            else { return nil }
            return components.url
        }
        return nil
    }

    private func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
        var value: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success else { return nil }
        return value
    }

    private func attributes(_ element: AXUIElement, _ names: [String]) -> [Any] {
        var result: CFArray?
        guard AXUIElementCopyMultipleAttributeValues(element, names as CFArray,
                AXCopyMultipleAttributeOptions(rawValue: 0), &result) == .success,
              let result else { return [] }
        return result as [AnyObject]
    }

    private func string(_ value: Any?) -> String { value as? String ?? "" }

    private func elements(_ value: Any?) -> [AXUIElement] {
        guard let values = value as? [AnyObject] else { return [] }
        return values.compactMap { item in
            guard CFGetTypeID(item) == AXUIElementGetTypeID() else { return nil }
            return unsafeBitCast(item, to: AXUIElement.self)
        }
    }
}

private extension Array {
    subscript(safe index: Int) -> Element? { indices.contains(index) ? self[index] : nil }
}

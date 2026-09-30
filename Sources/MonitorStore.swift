import AppKit
import SwiftUI
import ApplicationServices
import ServiceManagement

struct ConversationGroup: Identifiable {
    let id: String
    var sessions: [SessionRecord]
    var primary: SessionRecord { sessions[0] }
}

final class MonitorStore: ObservableObject {
    @Published var sessions: [SessionRecord] = []
    @Published var apps: [AppRecord] = []
    @Published var accessibility = false
    @Published var accessibilityCheck: AccessibilityCheckState = .idle
    @Published var accessibilityCheckDetail = "点击查询，检查辅助功能是否实际可用"
    @Published var accessibilityCheckLabel = "未查询"
    private var awaitingPermissionChange = false
    @Published var windowPermissionResult = PermissionProbeResult.idle
    @Published var helperPermissionResult = PermissionProbeResult.idle
    @Published var permissionCheckedAt: Date?
    @Published var permissionRepairMessage: String?
    @Published var permissionRestarting = false
    private let permissionHealthBridge = ContinuationBridge()
    private var permissionQueryGeneration = 0
    private var permissionNativeRevision = 0
    private(set) var permissionRepairActive = false
    @Published var lastUpdate: Date?
    @Published var errors: [String] = []
    @Published var actionMessage: String?
    @Published private(set) var removedSessions: [RemovedSession]
    @Published private(set) var undoRemovalKey: String?
    private let removalDefaults: UserDefaults
    private let writeHealthDiagnostics: Bool
    private var messageGeneration = UUID()
    @Published var paused = false
    @Published var autoContinueEnabled: Bool
    @Published var autoQueueInsertionEnabled: Bool
    @Published private(set) var keepAwakeEnabled: Bool
    @Published private(set) var keepAwakeStatus = KeepAwakeStatus()
    private let keepAwake = KeepAwakeController()
    @Published var promptRules: PromptRules
    @Published private(set) var supervisedSessionIDs: Set<String>
    @Published private(set) var supervisedAppIDs: Set<String>
    static let supervisionDefaultsKey = "supervisedSessionIDs.v1"
    static let appSupervisionDefaultsKey = "supervisedAppIDs.v1"
    @Published var recoverySummary = "自动继续正在检查"
    var onRecoveryNotice: ((ContinuationNotice) -> Void)?
    private let continuation: ContinuationController
    private let queueInsertion = QueueInsertionController()
    @Published var expanded = false
    // Native menu tracking extends the floating panel’s hover region.
    var settingsMenuTracking = false
    @Published private(set) var expandedApps: Set<String> = []
    // 控制位固定在右侧：展开时箭头贴面板最右，收起胶囊整体贴屏幕最右缘；
    // 锚点与拖拽热区随之固定走右控分支。
    @Published var handleOnLeft = false
    @Published var expansionHeightLimit: CGFloat = 548
    @Published var transparency = 0.75 {
        didSet { UserDefaults.standard.set(transparency, forKey: "backgroundTransparency") }
    }
    @Published var frostLevel: FrostLevel = .medium {
        didSet { UserDefaults.standard.set(frostLevel.rawValue, forKey: "frostLevel") }
    }
    @Published var pausedAt: Date?
    private var clocks = SessionClocks()
    private var appIcons: [String: NSImage] = [:]
    @Published var query = ""
    @Published var recent = false
    @Published var selectedApp = "all"
    @Published var pins: Set<String>
    @Published var labels: [String: String]
    @Published var extraBundleIDs: [String]
    @Published var loginEnabled = false
    var onResize: ((Bool) -> Void)?
    private let monitor = NativeMonitor()
    private let nativeQueue = DispatchQueue(label: "radar.native", qos: .utility)
    private let decoderQueue = DispatchQueue(label: "radar.decode", qos: .utility)
    private var nativeSessions: [SessionRecord] = []
    private var localSessions: [SessionRecord] = []
    private var process: Process?
    private var outputPipe: Pipe?
    private var timer: Timer?
    private var scanning = false
    private var buffer = Data()
    private var lastLocalUpdate = Date.distantPast
    private var lastLaunch = Date.distantPast
    private var collectorLastProgress = ProcessInfo.processInfo.systemUptime
    private var collectorRecoveryPending = false
    private(set) var collectorRestarts = 0
    private var hasSnapshot = false
    private var stopping = false
    private var collectorErrors: [String] = []
    private var transportErrors: [String] = []
    private var collectorGeneration = 0
    private var decoderGeneration = 0 // accessed only on decoderQueue

    init(defaults: UserDefaults = .standard, writeHealthDiagnostics: Bool = true,
         continuation: ContinuationController? = nil) {
        removalDefaults = defaults
        self.writeHealthDiagnostics = writeHealthDiagnostics
        self.continuation = continuation ?? ContinuationController(countDefaults: defaults)
        removedSessions = defaults.data(forKey: "removedSessions.v1")
            .flatMap { try? JSONDecoder().decode([RemovedSession].self, from: $0) } ?? []
        autoContinueEnabled = defaults.bool(forKey: "autoContinueOptIn.v2")
        autoQueueInsertionEnabled = defaults.object(forKey: "autoQueueInsertion.v1") as? Bool ?? true
        keepAwakeEnabled = defaults.bool(forKey: "keepAwakeEnabled.v1")
        promptRules = PromptRules.load(from: defaults)
        supervisedSessionIDs = Set((defaults.stringArray(forKey: Self.supervisionDefaultsKey) ?? []).filter { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty })
        supervisedAppIDs = Set((defaults.stringArray(forKey: Self.appSupervisionDefaultsKey) ?? []).filter { !$0.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty })
        let savedTransparency = defaults.double(forKey: "backgroundTransparency")
        if (0.70...0.80).contains(savedTransparency) { transparency = savedTransparency }
        if let savedFrost = defaults.object(forKey: "frostLevel") as? Int,
           let level = FrostLevel(rawValue: savedFrost) { frostLevel = level }
        pins = Set(defaults.stringArray(forKey: "pinnedSessions") ?? [])
        labels = defaults.dictionary(forKey: "projectLabels") as? [String: String] ?? [:]
        extraBundleIDs = defaults.stringArray(forKey: "extraBundleIDs") ?? []
        if #available(macOS 13.0, *) { loginEnabled = SMAppService.mainApp.status == .enabled }
        self.continuation.rulesProvider = { [weak self] in self?.promptRules ?? PromptRules() }
        self.continuation.setSupervisedIDs(supervisedSessionIDs)
        keepAwake.onStatusChange = { [weak self] status in
            self?.keepAwakeStatus = status
            self?.writeDiagnostic()
        }
    }

    func start() {
        keepAwake.setEnabled(keepAwakeEnabled)
        queueInsertion.onResult = { [weak self] in self?.showMessage($0) }
        queueInsertion.start(enabled: autoQueueInsertionEnabled)
        continuation.onSummary = { [weak self] in self?.recoverySummary = $0 }
        continuation.onNotice = { [weak self] in self?.onRecoveryNotice?($0) }
        continuation.start(enabled: autoContinueEnabled && !permissionRepairActive)
        startCollector()
        refreshNative()
        let timer = Timer(timeInterval: 3, repeats: true) { [weak self] _ in
            guard let self = self, !self.paused else { return }
            self.refreshNative()
            self.recoverUnresponsiveCollector()
            if self.process?.isRunning != true && Date().timeIntervalSince(self.lastLaunch) > 6 {
                self.startCollector()
            }
            self.rebuild()
        }
        self.timer = timer
        RunLoop.main.add(timer, forMode: .common)
    }

    func stop() {
        keepAwake.stop()
        queueInsertion.stop()
        continuation.stop()
        permissionQueryGeneration += 1
        permissionHealthBridge.cancel()
        stopping = true
        collectorGeneration += 1
        timer?.invalidate()
        outputPipe?.fileHandleForReading.readabilityHandler = nil
        process?.terminate()
    }

    func togglePause() {
        paused.toggle()
        queueInsertion.configure(enabled: autoQueueInsertionEnabled, paused: paused)
        pausedAt = paused ? Date() : nil
        continuation.configure(enabled: autoContinueEnabled && !permissionRepairActive, paused: paused)
        if paused {
            collectorGeneration += 1
            outputPipe?.fileHandleForReading.readabilityHandler = nil
            process?.terminate()
            process = nil
        } else {
            startCollector()
            refreshNative()
        }
        writeDiagnostic()
    }

    func toggleKeepAwake() {
        keepAwakeEnabled.toggle()
        removalDefaults.set(keepAwakeEnabled, forKey: "keepAwakeEnabled.v1")
        keepAwake.setEnabled(keepAwakeEnabled)
        showMessage(keepAwakeStatus.summary)
        writeDiagnostic()
    }

    private func startCollector() {
        guard !paused, !stopping, process?.isRunning != true else { return }
        guard let resourcePath = Bundle.main.resourcePath else { return }
        let script = resourcePath + "/collector/main.py"
        guard FileManager.default.fileExists(atPath: script) else {
            transportErrors = ["缺少本地采集组件，请重新构建应用"]
            rebuild()
            return
        }
        let task = Process()
        let pipe = Pipe()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        task.arguments = ["-B", "-u", script, "--watch"]
        task.standardOutput = pipe
        task.standardError = FileHandle.nullDevice
        task.environment = ProcessInfo.processInfo.environment.merging(["PYTHONDONTWRITEBYTECODE": "1"]) { _, new in new }
        collectorGeneration += 1
        let generation = collectorGeneration
        decoderQueue.async { [weak self] in
            self?.buffer.removeAll()
            self?.decoderGeneration = generation
        }
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { handle.readabilityHandler = nil; return }
            self?.decoderQueue.async { [weak self] in self?.consume(data, generation: generation) }
        }
        if hasSnapshot { collectorRestarts += 1 }   // 首次启动不计入；此后为断联自愈
        lastLaunch = Date()
        collectorLastProgress = ProcessInfo.processInfo.systemUptime
        collectorRecoveryPending = false
        do {
            try task.run()
            process = task
            outputPipe = pipe
        } catch {
            transportErrors = ["本地采集器未能启动：需要系统 Python 3"]
            rebuild()
        }
    }

    private func recoverUnresponsiveCollector() {
        guard !stopping, !paused, !collectorRecoveryPending,
              let task = process, task.isRunning,
              ProcessInfo.processInfo.systemUptime - collectorLastProgress >= 45 else { return }
        // A live PID does not prove a healthy pipe or adapter. Invalidate its
        // callbacks first, then replace only this app-owned collector process.
        collectorRecoveryPending = true
        collectorGeneration += 1
        outputPipe?.fileHandleForReading.readabilityHandler = nil
        transportErrors = ["本地采集器持续无响应，正在重新启动；旧状态保持待确认"]
        task.terminate()
        DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
            if task.isRunning { kill(task.processIdentifier, SIGKILL) }
        }
    }

    private func consume(_ data: Data, generation: Int) {
        guard generation == decoderGeneration else { return }
        buffer.append(data)
        if buffer.count > 8_000_000 { buffer.removeAll(); return }
        while let newline = buffer.firstIndex(of: 10) {
            let line = Data(buffer[..<newline])
            buffer.removeSubrange(...newline)
            do {
                let snapshot = try JSONDecoder().decode(CollectorSnapshot.self, from: line)
                DispatchQueue.main.async { [weak self] in
                    guard let self = self, !self.paused, generation == self.collectorGeneration else { return }
                    self.localSessions = snapshot.sessions
                    self.collectorErrors = snapshot.errors
                    self.transportErrors = []
                    self.lastLocalUpdate = Date(timeIntervalSince1970: snapshot.collected_at)
                    self.collectorLastProgress = ProcessInfo.processInfo.systemUptime
                    self.hasSnapshot = true
                    self.rebuild()
                }
            } catch {
                DispatchQueue.main.async { [weak self] in
                    guard let self = self, generation == self.collectorGeneration else { return }
                    self.transportErrors = ["会话数据格式异常，正在重试"]
                    self.rebuild()
                }
            }
        }
    }

    func refreshNative() {
        guard !scanning, !paused else { return }
        scanning = true
        let extras = extraBundleIDs
        nativeQueue.async { [weak self] in
            guard let self = self else { return }
            let scan = self.monitor.scan(extraBundleIDs: extras)
            DispatchQueue.main.async {
                self.scanning = false
                guard !self.paused else { return }
                self.apps = scan.apps
                self.nativeSessions = scan.sessions
                self.accessibility = scan.accessibility
                if !scan.accessibility {
                    self.recordWindowPermissionRevocation()
                }
                if self.awaitingPermissionChange && scan.accessibility {
                    self.awaitingPermissionChange = false
                    self.verifyAccessibility(reveal: false)
                }
                self.rebuild()
            }
        }
    }

    private func rebuild() {
        let stale = hasSnapshot && Date().timeIntervalSince(lastLocalUpdate) > 15
        errors = collectorErrors + transportErrors
        if stale { errors.append("会话采集暂未更新，活动状态已降级") }
        if !hasSnapshot && Date().timeIntervalSince(lastLaunch) > 12 { errors.append("正在等待本地会话采集器") }
        let liveIDs = Set(apps.map(\.id))
        let locals = localSessions.map { session -> SessionRecord in
            var s = session
            if stale && s.isActive {
                s.status = "unknown"
                s.evidence = "采集已过期；" + s.evidence
                s.status_reason = "采集器超过 15 秒没有更新；后台重试中，暂不显示旧的活动状态"
            }
            if s.pid == nil { s.pid = apps.first(where: { $0.id == s.app_id })?.pid }
            return s
        }
        // A local adapter covers hidden sessions too. Only retain window fallbacks
        // for apps with no recent local sessions, avoiding double-counted tasks.
        let covered = stale ? Set<String>() : Set(locals.filter { $0.isReadable && Date().timeIntervalSince1970 - $0.updated_at < 86400 && liveIDs.contains($0.app_id) }.map(\.app_id))
        let fallback = nativeSessions.filter { !covered.contains($0.app_id) }
        var unique: [String: SessionRecord] = [:]
        for item in locals + fallback { unique[item.id] = item }
        sessions = clocks.update(Array(unique.values), now: Date().timeIntervalSince1970).sorted {
            if pins.contains($0.id) != pins.contains($1.id) { return pins.contains($0.id) }
            if $0.priority != $1.priority { return $0.priority < $1.priority }
            return $0.updated_at > $1.updated_at
        }
        lastUpdate = hasSnapshot ? lastLocalUpdate : nil
        refreshSupervision()
        continuation.observe(sessions, fresh: hasSnapshot && !stale && !paused,
                             excludedIDs: removedSessionIDs)
        onResize?(expanded)
        writeDiagnostic()
    }

    var ongoingSessions: [SessionRecord] {
        displaySessions.filter { $0.isActive }
    }

    var compactCountLabel: String {
        guard hasSnapshot, paused || Date().timeIntervalSince(lastLocalUpdate) <= 15 else { return "…" }
        return String(ongoingSessions.count)
    }

    /// All current tasks plus one latest conversation for each other open app.
    /// Keep recent failures visible even if another task in the same app is live.
    /// 非活动条目只有结束/更新在当天才可上屏；进行中的任务不受影响。
    private func isTodayRow(_ row: SessionRecord) -> Bool {
        if row.isActive { return true }
        return Calendar.current.isDateInToday(Date(timeIntervalSince1970: row.ended_at ?? row.updated_at))
    }

    var displaySessions: [SessionRecord] {
        let now = Date().timeIntervalSince1970
        let liveIDs = Set(apps.map(\.id))
        let readable = sessions.filter(\.isReadable)
        var result = readable.filter { $0.isActive }
        let failures = Dictionary(grouping: readable.filter {
            $0.status == "interrupted" && $0.user_stopped != true &&
                now - ($0.ended_at ?? $0.updated_at) < 86400
        }, by: \.app_id)
        for rows in failures.values {
            // 同一应用的多条中断对话都要可见：十分钟内的每条都上屏，
            // 更早的只保留最新一条，避免旧失败挤掉新任务。
            for latest in rows.sorted(by: { ($0.ended_at ?? $0.updated_at) > ($1.ended_at ?? $1.updated_at) }) {
                let failureTime = latest.ended_at ?? latest.updated_at
                let latestTask = sessions.filter { $0.app_id == latest.app_id }.max(by: { $0.updated_at < $1.updated_at })
                if (latestTask?.id == latest.id || now - failureTime < 600) && !result.contains(where: { $0.id == latest.id }) {
                    result.append(latest)
                }
            }
        }
        let represented = Set(result.map(\.app_id))
        let ids = liveIDs.union(sessions.filter {
            $0.source == "window" || ($0.app_id == "claude-code" && $0.pid != nil)
        }.map(\.app_id))
        for id in ids.subtracting(represented).filter({ !$0.hasPrefix("browser-") }) {
            let rows = sessions.filter { $0.app_id == id }
            if let latest = rows.max(by: { $0.updated_at < $1.updated_at }), latest.isReadable,
               isTodayRow(latest) {
                result.append(latest)
            }
        }
        // Keep a stable app/order while timers and status refresh; no bouncing list.
        // Select current conversations first, then exclude them. Filtering
        // before selection would resurrect older history after every removal.
        return result.filter { !isRemoved($0) }.sorted {
            // 中断的对话最需要处理，排在最前；手动停止不算中断；运行中的次之。
            let leftRank = $0.userStoppedInterruption ? 0 : ($0.isActive ? 1 : 2)
            let rightRank = $1.userStoppedInterruption ? 0 : ($1.isActive ? 1 : 2)
            if leftRank != rightRank { return leftRank < rightRank }
            if $0.app_name != $1.app_name { return $0.app_name < $1.app_name }
            return $0.id < $1.id
        }
    }

    var interruptedCount: Int {
        displaySessions.filter {
            ($0.status == "interrupted" && $0.user_stopped != true) || $0.status == "stalled"
        }.count
    }
    var conversationGroups: [ConversationGroup] {
        var order: [String] = []
        var grouped: [String: [SessionRecord]] = [:]
        for session in displaySessions {
            if grouped[session.app_id] == nil { order.append(session.app_id) }
            grouped[session.app_id, default: []].append(session)
        }
        // Keep the current task in front; the disclosure reveals every other
        // readable conversation already collected for that application.
        let shown = Set(grouped.values.flatMap { $0.map(\.id) })
        // 久远对话只留在后台：分组展开只补充当天的其余会话，且有界封顶。
        let allReadable = sessions.filter {
            $0.isReadable && !isRemoved($0) && !shown.contains($0.id) && isTodayRow($0)
        }.sorted { $0.updated_at > $1.updated_at }.prefix(12)
        for session in allReadable where grouped[session.app_id] != nil {
            grouped[session.app_id, default: []].append(session)
        }
        return order.compactMap { id in
            guard let sessions = grouped[id] else { return nil }
            return ConversationGroup(id: id, sessions: sessions)
        }
    }

    func isAppGroupExpanded(_ appID: String) -> Bool { expandedApps.contains(appID) }

    func setAppGroupExpanded(_ appID: String, expanded shouldExpand: Bool) {
        guard conversationGroups.contains(where: { $0.id == appID && $0.sessions.count > 1 }),
              expandedApps.contains(appID) != shouldExpand else { return }
        if shouldExpand { expandedApps.insert(appID) }
        else { expandedApps.remove(appID) }
        onResize?(expanded)
    }

    func toggleAppGroup(_ appID: String) {
        setAppGroupExpanded(appID, expanded: !isAppGroupExpanded(appID))
    }

    var panelSize: NSSize {
        guard expanded else { return NSSize(width: 60, height: 32) }
        let screenHeight = NSScreen.main?.visibleFrame.height ?? 800
        let groups = conversationGroups
        let contentHeight: CGFloat = groups.isEmpty ? 66 : groups.reduce(CGFloat(8)) { height, group in
            let primaryHeight: CGFloat = ["interrupted", "stalled"].contains(group.primary.status) ? 76 : 62
            let extrasHeight: CGFloat = isAppGroupExpanded(group.id)
                ? group.sessions.dropFirst().reduce(CGFloat(0)) { sum, session in
                    sum + (["interrupted", "stalled"].contains(session.status) ? 54 : 40) + 2
                } : 0
            return height + primaryHeight + extrasHeight + 4
        }
        let height = 40 + contentHeight + 28 + 30 + (actionMessage == nil ? 0 : 24)
        return NSSize(width: 238, height: min(max(112, screenHeight - 80), CGFloat(height), expansionHeightLimit))
    }

    var visibleSessions: [SessionRecord] {
        let liveIDs = Set(apps.map(\.id))
        let now = Date().timeIntervalSince1970
        return sessions.filter { s in
            guard !isRemoved(s) else { return false }
            let isCurrent = s.status == "running" || s.status == "waiting" || s.source == "window" ||
                (now - s.updated_at < 86400 && (liveIDs.contains(s.app_id) || s.app_id == "claude-code")) || pins.contains(s.id)
            guard recent || isCurrent else { return false }
            guard selectedApp == "all" || s.app_id == selectedApp else { return false }
            return query.isEmpty || [s.title, s.project, s.app_name, labels[s.id] ?? ""].joined(separator: " ").localizedCaseInsensitiveContains(query)
        }
    }

    var activeCount: Int { sessions.filter { !isRemoved($0) && $0.status == "running" }.count }
    var waitingCount: Int { sessions.filter { !isRemoved($0) && $0.status == "waiting" }.count }
    var appCount: Int { Set(apps.map(\.id) + sessions.filter { $0.app_id == "claude-code" && $0.pid != nil }.map(\.app_id)).count }
    var appOptions: [(String, String)] {
        var names: [String: String] = [:]
        for s in sessions { names[s.app_id] = s.app_name }
        for app in apps { names[app.id] = app.name }
        return names.sorted { $0.value < $1.value }
    }

    func setExpanded(_ shouldExpand: Bool) {
        guard expanded != shouldExpand else { return }
        expanded = shouldExpand
        onResize?(expanded)
    }
    func toggleExpanded() { setExpanded(!expanded) }
    func togglePin(_ session: SessionRecord) {
        if pins.contains(session.id) { pins.remove(session.id) } else { pins.insert(session.id) }
        UserDefaults.standard.set(Array(pins), forKey: "pinnedSessions")
        rebuild()
    }
    func setLabel(_ label: String, for session: SessionRecord) {
        let clean = label.trimmingCharacters(in: .whitespacesAndNewlines)
        labels[session.id] = clean.isEmpty ? nil : clean
        UserDefaults.standard.set(labels, forKey: "projectLabels")
    }
    func openSession(_ session: SessionRecord) {
        monitor.activate(session) { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .unavailable:
                self.showMessage(session.app_id == "claude-code" ? "未找到承载应用，请手动前往 CLI 会话" : "应用未运行或无法定位，请先打开对应应用")
            case .application:
                self.showMessage("已切换到应用，请在其中选择对应对话")
            case .workspaceLink:
                self.showMessage("已请求打开对应工作区；ZCode 的链接不指定会话，请在工作区内选择该对话")
            case .conversationLink:
                break
            }
        }
    }

    @discardableResult func openNoticeSession(sessionID: String) -> Bool {
        guard let session = sessions.first(where: { $0.id == sessionID }) else {
            showMessage("原会话暂未读取到，请在对应应用中打开")
            return false
        }
        openSession(session)
        return true
    }

    func isRemoved(_ session: SessionRecord) -> Bool {
        let key = RemovedSession.key(for: session)
        return removedSessions.contains { $0.id == key }
    }

    private var removedSessionIDs: Set<String> {
        Set(sessions.filter { isRemoved($0) }.map(\.id))
    }

    func removeSession(_ session: SessionRecord) {
        guard !isRemoved(session) else { return }
        let record = RemovedSession(session)
        removedSessions.insert(record, at: 0)
        setSupervision(false, for: session)
        // Cancel queued/in-flight input immediately, without waiting for polling.
        continuation.exclude(removedSessionIDs.union([session.id]))
        saveRemovals()
        showMessage("已移除 · 原聊天保留", undoRemoval: record.id)
    }

    func restoreSession(_ key: String) {
        guard removedSessions.contains(where: { $0.id == key }) else { return }
        removedSessions.removeAll { $0.id == key }
        saveRemovals()
        showMessage("已恢复会话监控")
    }

    func undoRemoval() {
        if let key = undoRemovalKey { restoreSession(key) }
    }

    func restoreAllSessions() {
        removedSessions.removeAll()
        saveRemovals()
        showMessage("已恢复全部会话监控")
    }

    private func saveRemovals() {
        if let data = try? JSONEncoder().encode(removedSessions) {
            removalDefaults.set(data, forKey: "removedSessions.v1")
        }
        onResize?(expanded)
        writeDiagnostic()
    }

    private func showMessage(_ message: String, undoRemoval: String? = nil) {
        let generation = UUID()
        messageGeneration = generation
        actionMessage = message
        undoRemovalKey = undoRemoval
        onResize?(expanded)
        DispatchQueue.main.asyncAfter(deadline: .now() + (undoRemoval == nil ? 6 : 12)) { [weak self] in
            if self?.messageGeneration == generation {
                self?.actionMessage = nil
                self?.undoRemovalKey = nil
                if let self = self { self.onResize?(self.expanded) }
            }
        }
    }
    func openProject(_ session: SessionRecord) {
        guard session.project.hasPrefix("/"), FileManager.default.fileExists(atPath: session.project) else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: session.project))
    }
    func requestPermission() {
        awaitingPermissionChange = true
        NativeMonitor.requestAccessibility()
    }

    /// Results are queried separately; system trust alone never proves window access.
    func verifyAccessibility(reveal: Bool = true) {
        if reveal && !expanded { expanded = true; onResize?(true) }
        guard accessibilityCheck != .checking, !permissionRestarting else { return }
        permissionQueryGeneration += 1
        let generation = permissionQueryGeneration
        let nativeRevision = permissionNativeRevision
        accessibilityCheck = .checking
        accessibilityCheckLabel = "查询中"
        windowPermissionResult = .checking
        helperPermissionResult = .checking
        accessibilityCheckDetail = "正在分别检查实际窗口读取和输入恢复组件…"
        permissionHealthBridge.run(["mode": "health"]) { [weak self] code, _, _ in
            guard let self = self, !self.stopping, !self.permissionRestarting,
                  self.permissionQueryGeneration == generation else { return }
            self.helperPermissionResult = .helper(code)
            self.updatePermissionResult()
        }
        nativeQueue.async { [weak self] in
            let probe = NativeMonitor.accessibilityProbe()
            DispatchQueue.main.async {
                guard let self = self, !self.stopping, !self.permissionRestarting,
                      self.permissionQueryGeneration == generation,
                      self.permissionNativeRevision == nativeRevision else { return }
                self.accessibility = probe.granted
                self.windowPermissionResult = .window(granted: probe.granted, apps: probe.appsProbed, windows: probe.windowsRead)
                self.updatePermissionResult()
            }
        }
    }
    private func updatePermissionResult() {
        accessibilityCheckLabel = PermissionProbeResult.combinedLabel(window: windowPermissionResult, helper: helperPermissionResult)
        let checking = windowPermissionResult.state == .checking || helperPermissionResult.state == .checking
        accessibilityCheck = checking ? .checking : (windowPermissionResult.state == .ready && helperPermissionResult.state == .ready ? .passed : .failed)
        accessibilityCheckDetail = "窗口读取：\(windowPermissionResult.label)\n\(windowPermissionResult.detail)\n输入恢复组件：\(helperPermissionResult.label)\n\(helperPermissionResult.detail)"
        if !checking { permissionCheckedAt = Date() }
        writeDiagnostic()
    }
    func recordWindowPermissionRevocation() {
        permissionNativeRevision += 1
        accessibility = false
        windowPermissionResult = .window(granted: false, apps: 0, windows: 0)
        updatePermissionResult()
    }
    func beginPermissionRepair() {
        guard !permissionRepairActive else { return }
        permissionRepairActive = true
        continuation.configure(enabled: false, paused: paused)
    }
    func endPermissionRepair() {
        guard permissionRepairActive, !stopping else { return }
        permissionRepairActive = false
        continuation.configure(enabled: autoContinueEnabled, paused: paused)
    }
    func repairAccessibility() {
        beginPermissionRepair()
        PermissionRecoveryController.shared.show(store: self)
        verifyAccessibility(reveal: false)
    }
    func permissionWindowBecameActive() {
        guard PermissionRecoveryController.shared.isVisible, !permissionRestarting,
              permissionCheckedAt.map({ Date().timeIntervalSince($0) > 2 }) ?? true else { return }
        verifyAccessibility(reveal: false)
    }
    func revealFormalApplication() {
        let url = URL(fileURLWithPath: PermissionRestartPlan.formalPath)
        guard FileManager.default.fileExists(atPath: url.path) else {
            permissionRepairMessage = "正式应用不存在：\(url.path)"; return
        }
        NSWorkspace.shared.activateFileViewerSelecting([url])
        permissionRepairMessage = "已在 Finder 选中正式应用；系统设置点击加号后可选择它。"
    }
    func copyFormalApplicationPath() {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(PermissionRestartPlan.formalPath, forType: .string)
        permissionRepairMessage = "已复制正式路径。系统文件选择框按 ⌘⇧G 后粘贴即可定位。"
    }
    func restartFormalApplication() {
        guard !permissionRestarting else { return }
        let url = URL(fileURLWithPath: PermissionRestartPlan.formalPath)
        guard let formal = Bundle(url: url), formal.bundleIdentifier == PermissionRestartPlan.bundleID,
              formal.executableURL.map({ FileManager.default.isExecutableFile(atPath: $0.path) }) == true else {
            permissionRepairMessage = "正式路径中的任务雷达不存在或无法启动，请先安装正式版。"; return
        }
        let pid = ProcessInfo.processInfo.processIdentifier
        let existing = NSRunningApplication.runningApplications(withBundleIdentifier: PermissionRestartPlan.bundleID)
        guard !existing.contains(where: { !PermissionRestartPlan.shouldRestartInstance(path: $0.bundleURL?.path, pid: $0.processIdentifier, currentPID: pid) }) else {
            permissionRepairMessage = "检测到其他位置运行的任务雷达副本，请先退出该副本，再从这里重启正式版。"; return
        }
        let apps = existing.filter { PermissionRestartPlan.shouldRestartInstance(path: $0.bundleURL?.path, pid: $0.processIdentifier, currentPID: pid) }
        guard let arguments = PermissionRestartPlan.arguments(waitingFor: apps.map(\.processIdentifier) + [pid]) else { return }
        let helper = Process()
        helper.executableURL = URL(fileURLWithPath: "/bin/sh")
        helper.arguments = arguments
        helper.standardOutput = FileHandle.nullDevice
        helper.standardError = FileHandle.nullDevice
        helper.standardInput = FileHandle.nullDevice
        do { try helper.run() }
        catch { permissionRepairMessage = "重启调度未能启动：\(error.localizedDescription)"; return }
        permissionRestarting = true
        permissionQueryGeneration += 1
        permissionHealthBridge.cancel()
        windowPermissionResult = .idle; helperPermissionResult = .idle
        permissionRepairMessage = "正在退出当前实例；正式版启动后将自动打开此窗口并重新查询。"
        stop()
        for app in apps where app.processIdentifier != pid { app.terminate() }
        NSApp.terminate(nil)
    }

    func toggleAutoContinue() {
        setAutoContinue(!autoContinueEnabled)
    }

    func toggleAutoQueueInsertion() {
        autoQueueInsertionEnabled.toggle()
        removalDefaults.set(autoQueueInsertionEnabled, forKey: "autoQueueInsertion.v1")
        queueInsertion.configure(enabled: autoQueueInsertionEnabled, paused: paused)
        showMessage(autoQueueInsertionEnabled ? "新排队消息自动插队已开启" : "新排队消息自动插队已关闭")
        writeDiagnostic()
    }

    func setAutoContinue(_ enabled: Bool) {
        guard autoContinueEnabled != enabled else { return }
        autoContinueEnabled = enabled
        removalDefaults.set(enabled, forKey: "autoContinueOptIn.v2")
        continuation.configure(enabled: enabled && !permissionRepairActive, paused: paused)
        writeDiagnostic()
    }

    /// 执行规则变更必须取消旧排队，避免已关闭的限流恢复或旧提示词继续发送。
    /// 旧版会话跟催词不参与执行，单独修改它们不影响其他会话的恢复。
    func applyRules(_ rules: PromptRules) {
        let previous = promptRules
        promptRules = rules.normalized()
        promptRules.save(to: removalDefaults)
        var previousExecution = previous
        var currentExecution = promptRules
        previousExecution.followUps = [:]
        currentExecution.followUps = [:]
        if previousExecution != currentExecution {
            continuation.configure(enabled: autoContinueEnabled && !permissionRepairActive, paused: paused)
        }
        writeDiagnostic()
    }

    func isSupervised(_ session: SessionRecord) -> Bool {
        supervisedSessionIDs.contains(session.id) || supervisedAppIDs.contains(session.app_id)
    }

    func isAppSupervised(_ appID: String) -> Bool { supervisedAppIDs.contains(appID) }

    /// Provider scope follows new conversations; only unique, readable, visible
    /// identities enter the controller. A scope change never starts old rounds.
    var effectiveSupervisedIDs: Set<String> {
        let unique = Dictionary(grouping: sessions, by: \.id)
        let appRows = sessions.filter {
            supervisedAppIDs.contains($0.app_id) && unique[$0.id]?.count == 1 &&
                $0.isReadable && !isRemoved($0)
        }
        return supervisedSessionIDs.union(appRows.map(\.id))
    }

    func refreshSupervision() {
        let unique = Dictionary(grouping: sessions, by: \.id)
        let temporarilyUnavailable = Set(sessions.filter {
            supervisedAppIDs.contains($0.app_id) && $0.status == "unknown" &&
                unique[$0.id]?.count == 1 && !isRemoved($0)
        }.map(\.id))
        continuation.setSupervisedIDs(effectiveSupervisedIDs,
                                     temporarilyUnavailable: temporarilyUnavailable)
    }

    func setAppSupervision(_ enabled: Bool, appID: String) {
        guard sessions.contains(where: { $0.app_id == appID }) || supervisedAppIDs.contains(appID) else { return }
        var updated = supervisedAppIDs
        if enabled { updated.insert(appID) } else { updated.remove(appID) }
        guard updated != supervisedAppIDs else { return }
        supervisedAppIDs = updated
        removalDefaults.set(updated.sorted(), forKey: Self.appSupervisionDefaultsKey)
        refreshSupervision()
        writeDiagnostic()
    }

    func toggleSupervision(for session: SessionRecord) {
        setSupervision(!isSupervised(session), for: session)
    }

    func setSupervision(_ enabled: Bool, for session: SessionRecord) {
        guard enabled else { removeSupervision(sessionID: session.id); return }
        // A stale card cannot mark a different or unreadable conversation.
        let matches = sessions.filter { $0.id == session.id }
        guard matches.count == 1, let current = matches.first,
              current.isReadable, !isRemoved(current) else { return }
        var updated = supervisedSessionIDs
        updated.insert(session.id)
        guard updated != supervisedSessionIDs else { return }
        supervisedSessionIDs = updated
        removalDefaults.set(updated.sorted(), forKey: Self.supervisionDefaultsKey)
        // This is a per-session change, not a global mode reset.
        refreshSupervision()
        writeDiagnostic()
    }

    /// 取消持久化选择不依赖当前采集结果；会话消失或不可读时仍可撤销。
    func removeSupervision(sessionID: String) {
        guard supervisedSessionIDs.contains(sessionID) else { return }
        supervisedSessionIDs.remove(sessionID)
        removalDefaults.set(supervisedSessionIDs.sorted(), forKey: Self.supervisionDefaultsKey)
        refreshSupervision()
        writeDiagnostic()
    }

    func supervisionGroups(includeCompleted: Bool) -> [ConversationGroup] {
        let unique = Dictionary(grouping: sessions, by: \.id)
        let now = Date().timeIntervalSince1970
        // 同一应用的多个对话框都必须可勾选：运行/等待/停滞常驻，中断、空闲、
        // 待确认在 24 小时内列出，已完成按开关显示。少了这条，同应用第二条
        // 被中断的对话无法标记，狂暴恢复也就无从谈起。
        var candidates = sessions.filter {
            unique[$0.id]?.count == 1 && $0.isReadable && !isRemoved($0) &&
                (supervisedSessionIDs.contains($0.id) || $0.status == "running" || $0.status == "waiting" ||
                    $0.status == "stalled" ||
                    (($0.status == "interrupted" || $0.status == "idle" || $0.status == "unknown") &&
                        now - $0.updated_at < 86400) ||
                    (includeCompleted && $0.status == "completed"))
        }
        // Keep a provider switch visible even when all of its observed rounds
        // completed. This fallback is only a selector, never an auto-start.
        let visibleApps = Set(candidates.map(\.app_id))
        let available = sessions.filter { unique[$0.id]?.count == 1 && $0.isReadable && !isRemoved($0) }
        for (appID, rows) in Dictionary(grouping: available, by: \.app_id) where !visibleApps.contains(appID) {
            if let latest = rows.max(by: { $0.updated_at < $1.updated_at }) { candidates.append(latest) }
        }
        return Dictionary(grouping: candidates, by: \.app_id).map { appID, rows in
            ConversationGroup(id: appID, sessions: rows.sorted {
                if ($0.status == "running") != ($1.status == "running") { return $0.status == "running" }
                if $0.updated_at != $1.updated_at { return $0.updated_at > $1.updated_at }
                return $0.id < $1.id
            })
        }.sorted { $0.primary.app_name < $1.primary.app_name }
    }

    var markedCompletedCount: Int {
        sessions.filter { isSupervised($0) && !isRemoved($0) && $0.isReadable && $0.status == "completed" }.count
    }

    func supervisionDetail(for session: SessionRecord) -> String {
        guard isSupervised(session) else { return "未标记持续监督" }
        if paused || permissionRepairActive { return "已标记持续监督；监督暂时暂停" }
        if promptRules.completionMode != "rage" { return "已标记持续监督；协作模式下完成只提醒" }
        if !autoContinueEnabled { return "已标记持续监督；自动发送总开关未开启" }
        if let reason = ContinuationPolicy.followupRestriction(session) { return "已标记持续监督；" + reason }
        return "已标记持续监督；新的明确完成轮次将继续优化，意外中断按规则恢复"
    }

    /// 单个会话的「完成后自动发送」提示词；nil 表示移除。
    func setFollowUp(_ text: String?, for session: SessionRecord) {
        var rules = promptRules
        if let cleaned = text.map(PromptRules.sanitize), !cleaned.isEmpty {
            rules.followUps[session.id] = cleaned
        } else {
            rules.followUps[session.id] = nil
        }
        applyRules(rules)
    }

    func showPromptSettings() { PromptSettingsController.shared.showGlobal(store: self) }
    func showFollowUpEditor(_ session: SessionRecord) {
        if !expanded { expanded = true; onResize?(true) }
        PromptSettingsController.shared.showFollowUp(store: self, session: session)
    }
    func startRageForCompleted() { continuation.startRageForCompleted() }
    func retryContinuation(sessionID: String, key: String) {
        continuation.requestManualRetry(sessionID: sessionID, key: key)
    }
    func cancelContinuation(sessionID: String, key: String) {
        continuation.cancelCurrent(sessionID: sessionID, key: key)
    }
    func cancelContinuation() { continuation.cancelCurrent() }
    func showContinuationDiagnostics() {
        verifyAccessibility()
    }
    func previewContinuationNotice() {
        onRecoveryNotice?(ContinuationNotice(title: "AI 监督 · 提醒预览", conversation: "中断后返回原会话",
                                              message: "自动发送关闭时只提醒。狂暴模式已标记的可恢复中断，发现后 1 秒排定，再核验会话、覆盖输入框并发送继续提示词。这次仅预览。"))
    }
    func applicationIcon(for session: SessionRecord) -> NSImage? {
        if let app = apps.first(where: { $0.id == session.app_id || ($0.pid == session.pid && session.app_id.hasPrefix("web-")) }), !app.path.isEmpty {
            if let cached = appIcons[app.path] { return cached }
            // Cline's Dock uses its bundled Hologram variant in the user's
            // reference. Launch Services still returns the monochrome base icon.
            let clineIcon = session.app_id == "cline"
                ? NSImage(contentsOfFile: app.path + "/Contents/Resources/icons/app/macos/hologram.png") : nil
            let icon = clineIcon ?? NSWorkspace.shared.icon(forFile: app.path)
            appIcons[app.path] = icon
            appIcons[session.app_id] = icon
            return icon
        }
        if let cached = appIcons[session.app_id] { return cached }
        if session.app_id == "claude-code" || session.app_id == "web-claude" {
            if let cached = appIcons["claude-code"] { return cached }
            if let path = Bundle.main.path(forResource: "claude-code", ofType: "png", inDirectory: "icons"),
               let icon = NSImage(contentsOfFile: path) {
                appIcons["claude-code"] = icon
                return icon
            }
        }
        return nil
    }
    func showSessionDetails(_ session: SessionRecord) {
        let formatter = DateFormatter()
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss"
        func stamp(_ value: Double?) -> String {
            value.map { formatter.string(from: Date(timeIntervalSince1970: $0)) } ?? "数据源未提供"
        }
        showDiagnostic(title: session.app_name + " · 状态与计时", text: "\(session.title)\n\n\(session.statusLabel)\n\(session.statusReason)\n\n计时依据\n\(session.timingExplanation)\n开始：\(stamp(session.started_at))\n结束：\(stamp(session.ended_at))\n\n状态证据\n\(session.evidence)")
    }
    var backgroundDiagnostic: String {
        var lines = [accessibility ? "窗口权限：辅助功能已开启" : "窗口权限：任务雷达尚未获得辅助功能授权；扣子等依赖窗口读取的应用无法取得对话信息。本地会话读取不受影响。"]
        lines.append("自动发现：已开启。每轮检查运行中的应用，识别 AI 名称/标识；授权后轮询未知应用的明确生成按钮，识别后自动记住。能确认标题和状态的会话自动显示，无须逐个手动添加。")
        for app in apps.sorted(by: { $0.name < $1.name }) {
            let rows = sessions.filter { $0.app_id == app.id }
            let unreadable = rows.filter { !$0.isReadable }
            let visible = displaySessions.filter { $0.app_id == app.id }.count
            let unread = unreadable.max(by: { $0.updated_at < $1.updated_at })
            let reason = unread.map { $0.status == "unknown" ? $0.statusReason : "本地记录缺少可识别的对话标题，已留在后台" }
            lines.append("\(app.name) · 已显示 \(visible) 个对话" + (reason.map { "\n后台：" + $0 } ?? (rows.isEmpty ? "\n本轮没有可读取的会话数据" : "\n本地会话数据可读取")))
        }
        if !errors.isEmpty { lines.append("采集错误\n" + errors.joined(separator: "\n")) }
        return lines.joined(separator: "\n\n")
    }
    func showBackgroundDiagnostics() { showDiagnostic(title: "后台读取情况", text: backgroundDiagnostic) }
    private func showDiagnostic(title: String, text: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = "本机只读诊断 · " + DateFormatter.localizedString(from: Date(), dateStyle: .none, timeStyle: .short)
        alert.addButton(withTitle: "知道了")
        alert.window.appearance = NSAppearance(named: .darkAqua)
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 0, width: 390, height: 290))
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        let content = NSTextView(frame: scroll.bounds)
        content.isEditable = false
        content.isSelectable = true
        content.drawsBackground = false
        content.textColor = .white
        content.font = .systemFont(ofSize: 12)
        content.textContainerInset = NSSize(width: 8, height: 8)
        content.autoresizingMask = [.width]
        content.isVerticallyResizable = true
        content.textContainer?.widthTracksTextView = true
        content.string = text
        scroll.documentView = content
        alert.accessoryView = scroll
        alert.window.level = .floating
        alert.runModal()
    }
    func addApplication() {
        let picker = NSOpenPanel()
        picker.title = "选择要监控的 AI 应用"
        picker.directoryURL = URL(fileURLWithPath: "/Applications")
        picker.allowedContentTypes = [.applicationBundle]
        picker.canChooseDirectories = false
        picker.allowsMultipleSelection = false
        picker.begin { [weak self] response in
            guard response == .OK, let url = picker.url, let id = Bundle(url: url)?.bundleIdentifier, let self = self else { return }
            if !self.extraBundleIDs.contains(id) { self.extraBundleIDs.append(id) }
            UserDefaults.standard.set(self.extraBundleIDs, forKey: "extraBundleIDs")
            self.refreshNative()
        }
    }
    func toggleLogin() {
        if #available(macOS 13.0, *) {
            do {
                if loginEnabled { try SMAppService.mainApp.unregister() }
                else { try SMAppService.mainApp.register() }
                loginEnabled = SMAppService.mainApp.status == .enabled
            } catch { showMessage("登录启动设置未完成：请在系统设置中检查登录项") }
        }
    }

    private func writeDiagnostic() {
        guard writeHealthDiagnostics else { return }
        // Minimal local health receipt; no conversation bodies, paths or titles.
        let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/AgentRadar")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let info: [String: Any] = ["observed_at": Date().timeIntervalSince1970, "local_update": lastLocalUpdate.timeIntervalSince1970,
                                  "accessibility": accessibility, "apps": apps.map(\.name), "session_count": sessions.count,
                                  "accessibility_check": accessibilityCheckLabel,
                                  "window_permission_state": windowPermissionResult.state.rawValue,
                                  "window_permission_label": windowPermissionResult.label,
                                  "window_permission_detail": windowPermissionResult.detail,
                                  "helper_permission_state": helperPermissionResult.state.rawValue,
                                  "permission_repair_active": permissionRepairActive,
                                  "helper_permission_label": helperPermissionResult.label,
                                  "helper_permission_detail": helperPermissionResult.detail,
                                  "permission_checked_at": permissionCheckedAt?.timeIntervalSince1970 as Any? ?? NSNull(),
                                  "app_path": Bundle.main.bundleURL.path,
                                  "app_version": Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "unknown",
                                  "running": activeCount, "waiting": waitingCount, "interrupted": interruptedCount,
                                  "visible_rows": displaySessions.count, "background_transparency": transparency,
                                  "removed_sessions": removedSessions.count,
                                  "frost_level": frostLevel.label,
                                  "auto_continue_enabled": autoContinueEnabled,
                                  "auto_queue_insertion_enabled": autoQueueInsertionEnabled,
                                  "collector_restarts": collectorRestarts,
                                  "follow_up_armed": promptRules.followUps.count,
                                  "supervised_sessions": supervisedSessionIDs.count,
                                  "supervised_apps": supervisedAppIDs.count,
                                  "effective_supervised_sessions": effectiveSupervisedIDs.count,
                                  "completion_mode": promptRules.completionMode,
                                  "keep_awake_enabled": keepAwakeEnabled,
                                  "keep_awake_active": keepAwakeStatus.fullyActive,
                                  "keep_awake_detail": keepAwakeStatus.detail,
                                  "rate_limit_wake": promptRules.rateLimitWakeEnabled,
                                  "continuation_status": recoverySummary,
                                  "unreadable_background_rows": sessions.filter { !$0.isReadable }.count,
                                  "errors": errors, "paused": paused]
        if let data = try? JSONSerialization.data(withJSONObject: info, options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: dir.appendingPathComponent("health.json"), options: .atomic)
        }
    }
}

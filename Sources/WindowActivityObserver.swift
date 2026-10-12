import AppKit
import ApplicationServices
import Darwin
import Foundation

/// 窗口结构通知。故意不包含 AXValueChanged：回调只允许记下“需要重读”，不能读取或修改输入内容。
enum WindowActivityNotifications {
    static let structural: [String] = [
        kAXWindowCreatedNotification as String,
        kAXUIElementDestroyedNotification as String,
        kAXTitleChangedNotification as String,
        kAXMainWindowChangedNotification as String,
        kAXFocusedWindowChangedNotification as String,
        kAXWindowMiniaturizedNotification as String,
        kAXWindowDeminiaturizedNotification as String,
        kAXWindowMovedNotification as String,
        kAXWindowResizedNotification as String,
        kAXApplicationHiddenNotification as String,
        kAXApplicationShownNotification as String,
        kAXApplicationActivatedNotification as String,
        kAXApplicationDeactivatedNotification as String,
        kAXSheetCreatedNotification as String,
        kAXDrawerCreatedNotification as String
    ]
    static let rebuild: Set<String> = [
        kAXWindowCreatedNotification as String,
        kAXUIElementDestroyedNotification as String,
        kAXSheetCreatedNotification as String,
        kAXDrawerCreatedNotification as String
    ]
    static func accepts(_ name: String) -> Bool { structural.contains(name) }
}

enum WindowRefreshReason: String, Equatable {
    case activity
    case windowRebuilt
    case processExited
    case permissionRevoked
    case sleepWake
    case unsupportedFallback
    case partialBudgetExhausted
    case fullDue

    var rank: Int {
        switch self {
        case .permissionRevoked: return 80
        case .sleepWake: return 70
        case .partialBudgetExhausted: return 60
        case .fullDue: return 50
        case .unsupportedFallback: return 40
        case .windowRebuilt: return 30
        case .processExited: return 20
        case .activity: return 10
        }
    }

    static func moreSevere(_ left: WindowRefreshReason?, _ right: WindowRefreshReason) -> WindowRefreshReason {
        guard let left, left.rank >= right.rank else { return right }
        return left
    }
}

enum WindowRefreshPlan: Equatable {
    case partial(pids: [Int32], reason: WindowRefreshReason)
    case full(WindowRefreshReason)

    var isFull: Bool {
        if case .full = self { return true }
        return false
    }

    static func merging(_ existing: WindowRefreshPlan?, _ incoming: WindowRefreshPlan, limit: Int) -> WindowRefreshPlan {
        guard let existing else { return incoming }
        switch (existing, incoming) {
        case (.full(let left), .full(let right)):
            return .full(WindowRefreshReason.moreSevere(left, right))
        case (.full(let reason), .partial):
            return .full(reason)
        case (.partial, .full(let reason)):
            return .full(reason)
        case (.partial(let leftPIDs, let leftReason), .partial(let rightPIDs, let rightReason)):
            var seen = Set<Int32>()
            let merged = (leftPIDs + rightPIDs).filter { seen.insert($0).inserted }
            if merged.count > max(1, limit) { return .full(.partialBudgetExhausted) }
            return .partial(pids: merged, reason: WindowRefreshReason.moreSevere(leftReason, rightReason))
        }
    }
}

struct WindowActivityBudget: Equatable {
    var coalesce: TimeInterval = 0.35
    var maxCoalesceDelay: TimeInterval = 0.80
    var minimumGap: TimeInterval = 0.40
    var maxPartialsBetweenFull = 2
    var maxDirtyPIDs = 8
    var fullScanInterval: TimeInterval = 3
    static let standard = WindowActivityBudget()
}

enum WindowActivityInstallResult: Equatable {
    case observing(supported: [String], unsupported: [String])
    case unsupported(unsupported: [String])
    case permissionDenied
    case processUnavailable
    case failed(Int32)
}

protocol WindowActivityInstalling: AnyObject {
    func install(pid: Int32, notifications: [String], handler: @escaping (Int32, String) -> Void) -> WindowActivityInstallResult
    func uninstall(pid: Int32)
    func uninstallAll()
    func processIsAlive(_ pid: Int32) -> Bool
}

struct WindowActivityScheduler {
    var now: () -> TimeInterval
    var schedule: (_ delay: TimeInterval, _ work: @escaping () -> Void) -> UInt64
    var cancel: (_ token: UInt64) -> Void
}

/// 主队列上的合并计时。延迟有上限，取消后的回调直接丢弃。
final class MainQueueActivityScheduler {
    private var next: UInt64 = 1
    private var cancelled = Set<UInt64>()

    func make() -> WindowActivityScheduler {
        WindowActivityScheduler(
            now: { ProcessInfo.processInfo.systemUptime },
            schedule: { [weak self] delay, work in
                guard let self else { return 0 }
                let token = self.next
                self.next += 1
                let clamped = min(max(0, delay), 3.5)
                DispatchQueue.main.asyncAfter(deadline: .now() + clamped) { [weak self] in
                    guard let self, self.cancelled.remove(token) == nil else { return }
                    work()
                }
                return token
            },
            cancel: { [weak self] token in self?.cancelled.insert(token) }
        )
    }
}

/// AXObserver 的注册、移除和运行循环。回调只把通知名交出去，不读取属性，不执行动作。
final class AXWindowActivityInstaller: WindowActivityInstalling {
    private final class Box {
        let pid: Int32
        let handler: (Int32, String) -> Void
        private let lock = NSLock()
        private var cancelled = false
        init(pid: Int32, handler: @escaping (Int32, String) -> Void) {
            self.pid = pid
            self.handler = handler
        }
        func cancel() { lock.lock(); cancelled = true; lock.unlock() }
        func emit(_ name: String) {
            lock.lock()
            let dead = cancelled
            lock.unlock()
            guard !dead else { return }
            handler(pid, name)
        }
    }

    private final class Registration {
        let observer: AXObserver
        let element: AXUIElement
        let box: Box
        var notifications: [String]
        var runLoopAdded: Bool
        init(observer: AXObserver, element: AXUIElement, box: Box, notifications: [String], runLoopAdded: Bool) {
            self.observer = observer
            self.element = element
            self.box = box
            self.notifications = notifications
            self.runLoopAdded = runLoopAdded
        }
    }

    private var registrations: [Int32: Registration] = [:]
    var registeredPIDs: [Int32] { registrations.keys.sorted() }

    static func failure(for error: AXError) -> WindowActivityInstallResult? {
        switch error {
        case .success, .notificationAlreadyRegistered:
            return nil
        case .apiDisabled:
            return .permissionDenied
        case .notificationUnsupported, .notImplemented:
            return .unsupported(unsupported: [])
        case .invalidUIElement, .cannotComplete, .invalidUIElementObserver:
            return .processUnavailable
        default:
            return .failed(error.rawValue)
        }
    }

    func processIsAlive(_ pid: Int32) -> Bool {
        guard pid > 1 else { return false }
        if kill(pid, 0) == 0 { return true }
        return errno != ESRCH
    }

    func install(pid: Int32, notifications: [String], handler: @escaping (Int32, String) -> Void) -> WindowActivityInstallResult {
        precondition(Thread.isMainThread, "AXWindowActivityInstaller 只能在主线程注册或移除")
        uninstall(pid: pid)
        guard processIsAlive(pid) else { return .processUnavailable }
        let box = Box(pid: pid, handler: handler)
        var created: AXObserver?
        let createError = AXObserverCreate(pid, Self.notificationThunk, &created)
        guard createError == .success, let observer = created else {
            return Self.failure(for: createError) ?? .failed(createError.rawValue)
        }
        let element = AXUIElementCreateApplication(pid)
        AXUIElementSetMessagingTimeout(element, 0.10)
        let pointer = Unmanaged.passUnretained(box).toOpaque()
        // notificationThunk 只转发通知名，不能在这里读取 element。
        var supported: [String] = []
        var unsupported: [String] = []
        for name in notifications {
            let error = AXObserverAddNotification(observer, element, name as CFString, pointer)
            if Self.failure(for: error) == nil {
                supported.append(name)
                continue
            }
            if error == .notificationUnsupported || error == .notImplemented {
                unsupported.append(name)
                continue
            }
            remove(notifications: supported, observer: observer, element: element)
            return Self.failure(for: error) ?? .failed(error.rawValue)
        }
        guard !supported.isEmpty else {
            return .unsupported(unsupported: unsupported)
        }
        CFRunLoopAddSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(observer), .commonModes)
        registrations[pid] = Registration(observer: observer, element: element, box: box,
                                           notifications: supported, runLoopAdded: true)
        return .observing(supported: supported, unsupported: unsupported)
    }

    func uninstall(pid: Int32) {
        precondition(Thread.isMainThread, "AXWindowActivityInstaller 只能在主线程注册或移除")
        guard let registration = registrations.removeValue(forKey: pid) else { return }
        registration.box.cancel()
        if registration.runLoopAdded {
            CFRunLoopRemoveSource(CFRunLoopGetMain(), AXObserverGetRunLoopSource(registration.observer), .commonModes)
            registration.runLoopAdded = false
        }
        remove(notifications: registration.notifications, observer: registration.observer, element: registration.element)
    }

    func uninstallAll() {
        for pid in Array(registrations.keys) { uninstall(pid: pid) }
    }

    private static let notificationThunk: AXObserverCallback = { _, _, notification, refcon in
        guard let refcon else { return }
        Unmanaged<Box>.fromOpaque(refcon).takeUnretainedValue().emit(notification as String)
    }

    private func remove(notifications: [String], observer: AXObserver, element: AXUIElement) {
        for name in notifications {
            AXObserverRemoveNotification(observer, element, name as CFString)
        }
    }
}

/// 主线程限定的 AX 通知合并器。它不扫描窗口，只告诉 MonitorStore 该全量还是按 PID 局部重读。
///
/// MonitorStore 接入步骤见 `monitorStoreIntegrationSteps`。本类型不会切换窗口，也不会操作输入。
final class WindowActivityObserver {
    static let monitorStoreIntegrationSteps = """
    接入只改 MonitorStore 的刷新入口。不要改软件识别、按钮判断或发送队列。观察者只合并失效；真正读窗口仍走 NativeMonitor 现有只读路径。

    1. 保留 start、取消暂停和现有 3 秒定时器对 monitor.scan 的调用。全量扫描回到主线程并写入结果后，调用 windowActivity.noteFullScanCompleted()。通知缺失时轮询必须继续，不能关掉。
    2. 在主线程创建并持有 WindowActivityObserver()。start 里设置 onRequestsChanged：只有 !scanning 且 !paused 时才 takeRequest()。partial 调用 monitor.scanPartial(pids:extraBundleIDs:)；若 disposition 是 escalatedToFull，按全量结果入库并 noteFullScanCompleted()。full 调用 monitor.scan 并 noteFullScanCompleted()。
    3. 每次写入扫描结果后，用合并快照里全部 app 的 pid 调用 replaceTargets，并用 scan.accessibility 调用 noteAccessibilityTrusted。局部快照已经保留未刷新的应用，不能只用本次 PID 覆盖目标列表。
    4. togglePause 同步调用 setPaused。stop 调用 windowActivity.stop()。暂停期间只记账，不交付扫描，也不在回调里激活应用。
    5. 定时器若碰到 scanning，记下 fullScanPending；当前扫描结束后优先做全量 scan，不要接着跑局部。没有待执行全量时，如果 hasReadyRequest 再 takeRequest 一次。观察者在两次局部之后会停止交付，直到 noteFullScanCompleted 或满 3 秒改交付一次全量。scanPartial 自己也会在第 3 次、基线超过 3 秒、PID 超过 8 个、没有基线或辅助功能状态变化时改走 scan。
    6. fallbackActive 为真时仍然不能关闭定时器。权限撤销、通知不受支持、PID 退出、窗口重建失败和休眠唤醒都靠这条轮询兜底。回调禁止执行辅助功能动作、写入或复制属性、前置窗口、激活应用和写输入。
    7. scripts/test.sh 需编译并运行 Sources/Models.swift、Sources/NativeMonitor.swift、Sources/WindowActivityObserver.swift 与 tests/window_activity_observer_checks.swift。scripts/build.sh 已经包含 Sources/*.swift，不要排除新文件。
    """

    private struct Target {
        var generation: UInt64
        var supported: [String]
        var unsupported: [String]
    }

    private let installer: any WindowActivityInstalling
    private let scheduler: WindowActivityScheduler
    private let schedulerOwner: AnyObject?
    private let budget: WindowActivityBudget
    private var targets: [Int32: Target] = [:]
    private var desiredPIDs: [Int32] = []
    private var pollingPIDs = Set<Int32>()
    private var dirty: [Int32] = []
    private var pendingRemoval: [Int32] = []
    private var rebindPIDs = Set<Int32>()
    private var generationCounter: UInt64 = 0
    private var coalesceToken: UInt64?
    private var gapLocked = false
    private var burstStarted: TimeInterval?
    private var lastEmitAt: TimeInterval = -1_000_000
    private var lastFullAt: TimeInterval = 0
    private var partialsSinceFull = 0
    private var ready: WindowRefreshPlan?
    private var paused = false
    private var sleeping = false
    private var permissionBlocked = false
    private var fallbackHold = false
    private var awaitingFull = false
    private var heldFull: WindowRefreshReason?
    private var stopped = false
    private var tornDown = false
    private var systemTokens: [NSObjectProtocol] = []

    var onRequestsChanged: (() -> Void)?
    var hasReadyRequest: Bool { ready != nil }
    var observingPIDs: [Int32] { targets.keys.sorted() }
    var pollingOnlyPIDs: [Int32] { pollingPIDs.sorted() }
    var fallbackActive: Bool { permissionBlocked || sleeping || fallbackHold || !pollingPIDs.isEmpty }

    init(installer: any WindowActivityInstalling = AXWindowActivityInstaller(),
         scheduler: WindowActivityScheduler? = nil,
         schedulerOwner: AnyObject? = nil,
         budget: WindowActivityBudget = .standard,
         observeSystemEvents: Bool = true) {
        precondition(Thread.isMainThread, "WindowActivityObserver 必须在主线程创建")
        self.installer = installer
        self.budget = budget
        let resolved: WindowActivityScheduler
        let owner: AnyObject?
        if let scheduler {
            resolved = scheduler
            owner = schedulerOwner
        } else {
            let made = MainQueueActivityScheduler()
            resolved = made.make()
            owner = made
        }
        self.scheduler = resolved
        self.schedulerOwner = owner
        self.lastFullAt = resolved.now()
        if observeSystemEvents { installSystemObservers() }
    }

    deinit {
        if Thread.isMainThread {
            tearDownOnMain()
        } else {
            DispatchQueue.main.sync { tearDownOnMain() }
        }
    }

    func replaceTargets(_ pids: [Int32]) {
        assertMain()
        guard !stopped else { return }
        desiredPIDs = Self.uniquePositive(pids)
        syncRegistrations()
        guard !paused, !sleeping, !permissionBlocked, !awaitingFull else { return }
        if !pendingRemoval.isEmpty { noteDirtyAndArm() }
    }

    func setPaused(_ value: Bool) {
        assertMain()
        guard !stopped, paused != value else { return }
        paused = value
        if value {
            cancelCoalesce()
            burstStarted = nil
            return
        }
        if !sleeping && !permissionBlocked { syncRegistrations() }
        if let heldFull {
            let reason = heldFull
            self.heldFull = nil
            if !permissionBlocked { performRebinds() }
            if permissionBlocked && reason != .permissionRevoked { return }
            publishFullNow(reason)
            return
        }
        guard !permissionBlocked, !sleeping else { return }
        if !dirty.isEmpty || !pendingRemoval.isEmpty || !rebindPIDs.isEmpty { noteDirtyAndArm() }
    }

    func stop() {
        assertMain()
        tearDownOnMain()
    }

    func noteProcessExited(_ pid: Int32) {
        assertMain()
        guard !stopped, pid > 0 else { return }
        let tracked = targets[pid] != nil || pollingPIDs.contains(pid) || desiredPIDs.contains(pid)
        guard tracked else { return }
        installer.uninstall(pid: pid)
        targets.removeValue(forKey: pid)
        pollingPIDs.remove(pid)
        desiredPIDs.removeAll { $0 == pid }
        dirty.removeAll { $0 == pid }
        rebindPIDs.remove(pid)
        appendRemoval(pid)
        guard !paused, !sleeping, !permissionBlocked, !awaitingFull else { return }
        noteDirtyAndArm()
    }

    func noteAccessibilityTrusted(_ trusted: Bool) {
        assertMain()
        guard !stopped else { return }
        if !trusted {
            enterPermissionFallback()
            return
        }
        guard permissionBlocked else { return }
        permissionBlocked = false
        heldFull = nil
        awaitingFull = false
        if case .some(.full(.permissionRevoked)) = ready { ready = nil }
        pollingPIDs.removeAll()
        guard !sleeping, !paused else { return }
        syncRegistrations()
    }

    func noteSleep() {
        assertMain()
        guard !stopped, !sleeping else { return }
        sleeping = true
        cancelCoalesce()
        burstStarted = nil
        rebindPIDs.removeAll()
        dirty.removeAll()
        installer.uninstallAll()
        targets.removeAll()
    }

    func noteWake() {
        assertMain()
        guard !stopped, sleeping else { return }
        sleeping = false
        guard !permissionBlocked else { return }
        pollingPIDs.removeAll()
        syncRegistrations()
        if paused {
            heldFull = WindowRefreshReason.moreSevere(heldFull, .sleepWake)
            return
        }
        publishFullNow(.sleepWake)
    }

    /// 一次全量 scan 已经覆盖当前画面后调用。这会恢复局部额度，并取消观察者自己补的全量。
    func noteFullScanCompleted() {
        assertMain()
        guard !stopped else { return }
        partialsSinceFull = 0
        fallbackHold = false
        awaitingFull = false
        lastFullAt = scheduler.now()
        cancelCoalesce()
        burstStarted = nil
        guard !permissionBlocked, !paused, !sleeping else { return }
        if !dirty.isEmpty || !pendingRemoval.isEmpty || !rebindPIDs.isEmpty { noteDirtyAndArm() }
    }

    func takeRequest() -> WindowRefreshPlan? {
        assertMain()
        let plan = ready
        ready = nil
        return plan
    }

    private func installSystemObservers() {
        let center = NSWorkspace.shared.notificationCenter
        systemTokens.append(center.addObserver(forName: NSWorkspace.didTerminateApplicationNotification, object: nil, queue: .main) { [weak self] note in
            let application = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
            guard let pid = application?.processIdentifier else { return }
            self?.noteProcessExited(pid)
        })
        systemTokens.append(center.addObserver(forName: NSWorkspace.willSleepNotification, object: nil, queue: .main) { [weak self] _ in
            self?.noteSleep()
        })
        systemTokens.append(center.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            self?.noteWake()
        })
    }

    private func removeSystemObservers() {
        let center = NSWorkspace.shared.notificationCenter
        for token in systemTokens { center.removeObserver(token) }
        systemTokens.removeAll()
    }

    private func tearDownOnMain() {
        guard Thread.isMainThread else {
            DispatchQueue.main.sync { self.tearDownOnMain() }
            return
        }
        guard !tornDown else { return }
        tornDown = true
        stopped = true
        cancelCoalesce()
        removeSystemObservers()
        installer.uninstallAll()
        targets.removeAll()
        desiredPIDs.removeAll()
        pollingPIDs.removeAll()
        dirty.removeAll()
        pendingRemoval.removeAll()
        rebindPIDs.removeAll()
        ready = nil
        heldFull = nil
        onRequestsChanged = nil
        _ = schedulerOwner
    }

    private func syncRegistrations() {
        let wanted = desiredPIDs
        let wantedSet = Set(wanted)
        for pid in Array(targets.keys) where !wantedSet.contains(pid) {
            installer.uninstall(pid: pid)
            targets.removeValue(forKey: pid)
            appendRemoval(pid)
        }
        let droppedPolling = pollingPIDs.subtracting(wantedSet)
        pollingPIDs = pollingPIDs.intersection(wantedSet)
        for pid in droppedPolling.sorted() { appendRemoval(pid) }
        guard !permissionBlocked, !sleeping else { return }
        for pid in wanted where targets[pid] == nil && !pollingPIDs.contains(pid) {
            install(pid)
            if permissionBlocked { return }
        }
    }

    private func install(_ pid: Int32) {
        generationCounter += 1
        let generation = generationCounter
        targets[pid] = Target(generation: generation, supported: [], unsupported: [])
        let names = WindowActivityNotifications.structural
        let result = installer.install(pid: pid, notifications: names) { [weak self] eventPID, name in
            self?.ingest(pid: eventPID, notification: name, generation: generation)
        }
        switch result {
        case .observing(let supported, let unsupported):
            guard !supported.isEmpty else {
                forgetFailedInstall(pid, polling: true)
                return
            }
            targets[pid] = Target(generation: generation, supported: supported, unsupported: unsupported)
            pollingPIDs.remove(pid)
        case .unsupported:
            forgetFailedInstall(pid, polling: true)
        case .permissionDenied:
            targets.removeValue(forKey: pid)
            enterPermissionFallback()
        case .processUnavailable:
            targets.removeValue(forKey: pid)
            rebindPIDs.remove(pid)
            if installer.processIsAlive(pid) {
                pollingPIDs.insert(pid)
                installer.uninstall(pid: pid)
            } else {
                pollingPIDs.remove(pid)
                desiredPIDs.removeAll { $0 == pid }
                dirty.removeAll { $0 == pid }
                installer.uninstall(pid: pid)
                appendRemoval(pid)
            }
        case .failed:
            forgetFailedInstall(pid, polling: true)
        }
    }

    private func forgetFailedInstall(_ pid: Int32, polling: Bool) {
        targets.removeValue(forKey: pid)
        rebindPIDs.remove(pid)
        installer.uninstall(pid: pid)
        if polling { pollingPIDs.insert(pid) }
    }

    private func enterPermissionFallback() {
        let first = !permissionBlocked
        permissionBlocked = true
        cancelCoalesce()
        burstStarted = nil
        installer.uninstallAll()
        targets.removeAll()
        rebindPIDs.removeAll()
        dirty.removeAll()
        pendingRemoval.removeAll()
        pollingPIDs = Set(desiredPIDs)
        guard first else { return }
        if paused || sleeping {
            heldFull = .permissionRevoked
            return
        }
        publishFullNow(.permissionRevoked)
    }

    private func ingest(pid: Int32, notification: String, generation: UInt64) {
        if !Thread.isMainThread {
            DispatchQueue.main.async { [weak self] in
                self?.ingest(pid: pid, notification: notification, generation: generation)
            }
            return
        }
        guard !stopped, !permissionBlocked else { return }
        guard let target = targets[pid], target.generation == generation else { return }
        guard WindowActivityNotifications.accepts(notification) else { return }
        if WindowActivityNotifications.rebuild.contains(notification) { rebindPIDs.insert(pid) }
        appendDirty(pid)
        guard !paused, !sleeping, !awaitingFull, !fallbackHold else { return }
        noteDirtyAndArm()
    }

    private func noteDirtyAndArm() {
        guard !stopped, !paused, !sleeping, !permissionBlocked, !awaitingFull, !gapLocked else { return }
        let now = scheduler.now()
        if burstStarted == nil { burstStarted = now }
        let elapsed = Self.millis(now) - Self.millis(burstStarted ?? now)
        let cap = Self.millis(budget.maxCoalesceDelay) - elapsed
        if cap <= 0 {
            armCoalesce(delay: 0, locked: true)
            return
        }
        let delay = min(Self.millis(budget.coalesce), cap)
        armCoalesce(delay: Double(delay) / 1000, locked: false)
    }

    private func armCoalesce(delay: TimeInterval, locked: Bool) {
        if gapLocked && !locked { return }
        if let coalesceToken { scheduler.cancel(coalesceToken) }
        gapLocked = locked
        coalesceToken = scheduler.schedule(max(0, delay)) { [weak self] in
            guard let self else { return }
            self.coalesceToken = nil
            self.gapLocked = false
            self.flush()
        }
    }

    private func cancelCoalesce() {
        if let coalesceToken { scheduler.cancel(coalesceToken) }
        coalesceToken = nil
        gapLocked = false
    }

    private func flush() {
        guard !stopped else { return }
        guard !paused, !sleeping else { return }
        let didRebind = !rebindPIDs.isEmpty
        performRebinds()
        guard !permissionBlocked else { return }
        guard !dirty.isEmpty || !pendingRemoval.isEmpty else { return }
        let now = scheduler.now()
        let sinceEmit = Self.millis(now) - Self.millis(lastEmitAt)
        let gap = Self.millis(budget.minimumGap)
        if sinceEmit < gap {
            burstStarted = nil
            armCoalesce(delay: Double(gap - sinceEmit) / 1000, locked: true)
            return
        }
        if partialsSinceFull >= budget.maxPartialsBetweenFull {
            fallbackHold = true
            let remain = Self.millis(budget.fullScanInterval) - (Self.millis(now) - Self.millis(lastFullAt))
            if remain <= 0 {
                dirty.removeAll()
                pendingRemoval.removeAll()
                publishFullNow(.partialBudgetExhausted)
            } else {
                armCoalesce(delay: Double(remain) / 1000, locked: true)
            }
            return
        }
        var seen = Set<Int32>()
        let pids = (dirty + pendingRemoval).filter { seen.insert($0).inserted }
        let limit = max(1, budget.maxDirtyPIDs)
        let reason: WindowRefreshReason = didRebind ? .windowRebuilt : (dirty.isEmpty ? .processExited : .activity)
        if pids.count > limit {
            let nowPIDs = Array(pids.prefix(limit))
            let sent = Set(nowPIDs)
            dirty = dirty.filter { !sent.contains($0) }
            pendingRemoval = pendingRemoval.filter { !sent.contains($0) }
            publishPartial(pids: nowPIDs, reason: reason)
            if !dirty.isEmpty || !pendingRemoval.isEmpty { noteDirtyAndArm() }
            return
        }
        dirty.removeAll()
        pendingRemoval.removeAll()
        publishPartial(pids: pids, reason: reason)
    }

    private func performRebinds() {
        let pids = rebindPIDs.sorted()
        rebindPIDs.removeAll()
        for pid in pids {
            guard !permissionBlocked, desiredPIDs.contains(pid) else { continue }
            installer.uninstall(pid: pid)
            targets.removeValue(forKey: pid)
            install(pid)
            if permissionBlocked { return }
        }
    }

    private func publishPartial(pids: [Int32], reason: WindowRefreshReason) {
        partialsSinceFull += 1
        lastEmitAt = scheduler.now()
        burstStarted = nil
        publish(.partial(pids: pids, reason: reason))
    }

    private func publishFullNow(_ reason: WindowRefreshReason) {
        cancelCoalesce()
        burstStarted = nil
        dirty.removeAll()
        pendingRemoval.removeAll()
        rebindPIDs.removeAll()
        fallbackHold = false
        awaitingFull = true
        lastEmitAt = scheduler.now()
        publish(.full(reason))
    }

    private func publish(_ plan: WindowRefreshPlan) {
        ready = WindowRefreshPlan.merging(ready, plan, limit: budget.maxDirtyPIDs)
        onRequestsChanged?()
    }

    private func appendDirty(_ pid: Int32) {
        guard pid > 0, !dirty.contains(pid) else { return }
        dirty.append(pid)
    }

    private func appendRemoval(_ pid: Int32) {
        guard pid > 0, !pendingRemoval.contains(pid) else { return }
        pendingRemoval.append(pid)
    }

    private func assertMain() {
        precondition(Thread.isMainThread, "WindowActivityObserver 限制在主线程")
    }

    private static func uniquePositive(_ pids: [Int32]) -> [Int32] {
        var seen = Set<Int32>()
        return pids.filter { $0 > 0 && seen.insert($0).inserted }
    }

    private static func millis(_ time: TimeInterval) -> Int { Int((time * 1000).rounded()) }
}

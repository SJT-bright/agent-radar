import ApplicationServices
import Foundation

private let titleNotification = kAXTitleChangedNotification as String
private let destroyedNotification = kAXUIElementDestroyedNotification as String
private let createdNotification = kAXWindowCreatedNotification as String
private let valueNotification = kAXValueChangedNotification as String

private let debounceBudget = WindowActivityBudget(
    coalesce: 0.30, maxCoalesceDelay: 0.80, minimumGap: 0.40,
    maxPartialsBetweenFull: 2, maxDirtyPIDs: 8, fullScanInterval: 3)
private let fastBudget = WindowActivityBudget(
    coalesce: 0.10, maxCoalesceDelay: 0.40, minimumGap: 0,
    maxPartialsBetweenFull: 8, maxDirtyPIDs: 8, fullScanInterval: 30)

private final class ManualActivityClock {
    private var nowMS = 0
    private var nextID: UInt64 = 1
    private var items: [UInt64: (due: Int, work: () -> Void)] = [:]
    private var draining = false

    var scheduler: WindowActivityScheduler {
        WindowActivityScheduler(
            now: { [self] in Double(self.nowMS) / 1000 },
            schedule: { [self] delay, work in self.schedule(delay, work) },
            cancel: { [self] token in self.items[token] = nil })
    }

    func advance(ms: Int) {
        precondition(ms >= 0)
        nowMS += ms
        drain()
    }

    private func schedule(_ delay: TimeInterval, _ work: @escaping () -> Void) -> UInt64 {
        let id = nextID
        nextID += 1
        items[id] = (nowMS + max(0, Int((delay * 1000).rounded())), work)
        return id
    }

    private func drain() {
        if draining { return }
        draining = true
        defer { draining = false }
        while true {
            let due = items.filter { $0.value.due <= nowMS }.sorted { lhs, rhs in
                if lhs.value.due != rhs.value.due { return lhs.value.due < rhs.value.due }
                return lhs.key < rhs.key
            }
            guard let first = due.first else { break }
            items.removeValue(forKey: first.key)
            first.value.work()
        }
    }
}

private final class FakeWindowActivityInstaller: WindowActivityInstalling {
    enum Call: Equatable {
        case install(Int32)
        case uninstall(Int32)
        case uninstallAll
    }

    var calls: [Call] = []
    var installed = Set<Int32>()
    var handlers: [Int32: (Int32, String) -> Void] = [:]
    var requested: [String] = []
    var results: [Int32: WindowActivityInstallResult] = [:]
    var alive: Set<Int32>?

    func install(pid: Int32, notifications: [String], handler: @escaping (Int32, String) -> Void) -> WindowActivityInstallResult {
        calls.append(.install(pid))
        requested = notifications
        let result = results[pid] ?? .observing(supported: notifications, unsupported: [])
        if case .observing(let supported, _) = result, !supported.isEmpty {
            installed.insert(pid)
            handlers[pid] = handler
        } else {
            installed.remove(pid)
            handlers.removeValue(forKey: pid)
        }
        return result
    }

    func uninstall(pid: Int32) {
        calls.append(.uninstall(pid))
        installed.remove(pid)
        handlers.removeValue(forKey: pid)
    }

    func uninstallAll() {
        calls.append(.uninstallAll)
        installed.removeAll()
        handlers.removeAll()
    }

    func processIsAlive(_ pid: Int32) -> Bool { alive?.contains(pid) ?? true }

    func emit(_ pid: Int32, _ name: String) { handlers[pid]?(pid, name) }

    func installCount() -> Int { calls.filter { if case .install = $0 { return true }; return false }.count }
}

private final class ActivityFixture {
    let clock: ManualActivityClock
    let installer: FakeWindowActivityInstaller
    let observer: WindowActivityObserver

    init(_ budget: WindowActivityBudget) {
        let clock = ManualActivityClock()
        let installer = FakeWindowActivityInstaller()
        observer = WindowActivityObserver(installer: installer, scheduler: clock.scheduler,
                                           budget: budget, observeSystemEvents: false)
        self.clock = clock
        self.installer = installer
    }
}

private func session(_ id: String, pid: Int32?, title: String) -> SessionRecord {
    SessionRecord(id: id, app_id: "codex", app_name: "Codex", title: title, project: "",
                  status: "unknown", evidence: "e", updated_at: 1,
                  source: pid == nil ? "local-session" : "window", target: "", pid: pid, window_id: nil)
}

private func app(_ pid: Int32) -> AppRecord {
    AppRecord(id: "app-\(pid)", name: "App \(pid)", bundleID: "com.example.\(pid)", pid: pid, path: "")
}

@main
struct WindowActivityObserverChecks {
    static func main() {
        var count = 0
        func check(_ condition: Bool, _ message: String) {
            precondition(condition, message)
            count += 1
        }
        testContracts(check)
        testMergeAndBudget(check)
        testDebounceAndStarvation(check)
        testCoalesceCap(check)
        testPause(check)
        testRemovalAndStop(check)
        testUnsupportedPermissionAndSleep(check)
        testRebuild(check)
        testDirtyCap(check)
        testRealInstaller(check)
        print("window activity checks passed: \(count)")
    }

    static func testContracts(_ check: (Bool, String) -> Void) {
        check(!WindowActivityNotifications.structural.contains(valueNotification), "结构通知不含输入值变化")
        check(!WindowActivityNotifications.rebuild.isDisjoint(with: [destroyedNotification, createdNotification]), "窗口创建和销毁会重新绑定")
        check(AXWindowActivityInstaller.failure(for: .apiDisabled) == .permissionDenied, "API 停用视为权限撤销")
        check(AXWindowActivityInstaller.failure(for: .notificationUnsupported) == .unsupported(unsupported: []), "不支持的通知单独分类")
        check(AXWindowActivityInstaller.failure(for: .notImplemented) == .unsupported(unsupported: []), "未实现的通知单独分类")
        check(AXWindowActivityInstaller.failure(for: .invalidUIElement) == .processUnavailable, "元素失效视为进程或窗口不可用")
        check(AXWindowActivityInstaller.failure(for: .cannotComplete) == .processUnavailable, "消息失败视为进程不可用")
        check(AXWindowActivityInstaller.failure(for: .success) == nil, "成功不是失败")
        check(AXWindowActivityInstaller.failure(for: .notificationAlreadyRegistered) == nil, "重复注册按成功处理")
        let steps = WindowActivityObserver.monitorStoreIntegrationSteps
        for token in ["monitor.scan", "scanPartial", "noteFullScanCompleted", "takeRequest", "replaceTargets",
                      "setPaused", "noteAccessibilityTrusted", "fallbackActive", "fullScanPending"] {
            check(steps.contains(token), "接入步骤包含 \(token)")
        }
        let source = try! String(contentsOfFile: "Sources/WindowActivityObserver.swift", encoding: .utf8)
        for banned in ["AXUIElementPerformAction", "AXUIElementSetAttributeValue", "kAXRaiseAction", "AXUIElementCopyAttributeValue"] {
            check(!source.contains(banned), "观察者源码不能包含 \(banned)")
        }
        let merged = WindowRefreshPlan.merging(.partial(pids: [1, 2], reason: .activity),
                                                .partial(pids: [2, 3], reason: .windowRebuilt), limit: 8)
        check(merged == .partial(pids: [1, 2, 3], reason: .windowRebuilt), "局部请求合并 PID 并保留更强原因")
        check(WindowRefreshPlan.merging(.partial(pids: [1], reason: .activity), .full(.sleepWake), limit: 8) == .full(.sleepWake),
              "全量请求覆盖局部请求")
        check(WindowRefreshPlan.merging(.full(.fullDue), .full(.permissionRevoked), limit: 8) == .full(.permissionRevoked),
              "权限撤销优先于普通全量")
        check(WindowRefreshPlan.merging(.partial(pids: [1, 2], reason: .activity),
                                         .partial(pids: [3, 4, 5], reason: .activity), limit: 4) == .full(.partialBudgetExhausted),
              "合并后 PID 超限升级为全量")
    }

    static func testMergeAndBudget(_ check: (Bool, String) -> Void) {
        let limits = PartialRefreshLimits(maxPartialsBetweenFull: 2, maxBaselineAge: 3, maxPIDs: 8, partialBudget: 1)
        func decide(partials: Int, seconds: TimeInterval, count: Int, baseline: Bool = true,
                    trusted: Bool = true, baselineTrusted: Bool = true) -> PartialRefreshDisposition {
            NativeMonitor.partialRefreshDisposition(
                hasBaseline: baseline, partialsSinceFull: partials, secondsSinceFull: seconds,
                requestedCount: count, trusted: trusted, baselineTrusted: baselineTrusted, limits: limits)
        }
        check(decide(partials: 0, seconds: 0, count: 1) == .partial, "限额内允许局部扫描")
        check(decide(partials: 1, seconds: 2.9, count: 8) == .partial, "未超龄且未超次数时仍局部")
        check(decide(partials: 2, seconds: 0, count: 1) == .full(.partialBudget), "第三次局部必须升为全量")
        check(decide(partials: 0, seconds: 3, count: 1) == .full(.baselineStale), "基线超过 3 秒必须全量")
        check(decide(partials: 0, seconds: 0, count: 9) == .full(.tooManyPIDs), "PID 过多必须全量")
        check(decide(partials: 0, seconds: 0, count: 0) == .unchanged, "空请求不扫描")
        check(decide(partials: 0, seconds: 0, count: 0, baseline: false) == .full(.noBaseline), "没有基线时建立全量")
        check(decide(partials: 0, seconds: 0, count: 1, baseline: false) == .full(.noBaseline), "没有基线不能局部")
        check(decide(partials: 0, seconds: 0, count: 1, trusted: false) == .full(.accessibilityChanged), "失去权限时全量")
        check(decide(partials: 0, seconds: 0, count: 1, baselineTrusted: false) == .full(.accessibilityChanged), "权限恢复时全量")
        check(decide(partials: 9, seconds: 9, count: 1, trusted: false) == .full(.accessibilityChanged), "权限变化优先于局部额度")
        check(NativeMonitor.uniquePIDs([2, 0, 2, -1, 3]) == [2, 3], "局部 PID 去重并丢掉无效值")

        let baseline = NativeScan(
            sessions: [session("local", pid: nil, title: "保持"), session("w1", pid: 1, title: "甲"),
                       session("w2a", pid: 2, title: "乙旧一"), session("w2b", pid: 2, title: "乙旧二"),
                       session("w3", pid: 3, title: "丙")],
            apps: [app(1), app(2), app(3)], accessibility: true)
        let merged = NativeMonitor.mergePartialSnapshot(
            baseline: baseline,
            freshSessions: [session("w2", pid: 2, title: "乙新"), session("w4", pid: 4, title: "丁")],
            freshApps: [app(2), app(4)], refreshedPIDs: [2, 4], removedPIDs: [3])
        check(merged.sessions.map(\.title) == ["保持", "甲", "乙新", "丁"], "局部合并保持其他行并替换目标 PID")
        check(merged.apps.map(\.pid) == [1, 2, 4], "退出的应用被移除，新 PID 附在后面")
        check(merged.accessibility, "局部合并不改变权限位")
    }

    static func testDebounceAndStarvation(_ check: (Bool, String) -> Void) {
        let fx = ActivityFixture(debounceBudget)
        fx.observer.replaceTargets([10, 11])
        check(fx.installer.installed == [10, 11], "目标 PID 完成注册")
        check(fx.installer.requested == WindowActivityNotifications.structural, "注册列表就是结构通知")
        var callbacks = 0
        fx.observer.onRequestsChanged = { callbacks += 1 }
        for _ in 0..<20 { fx.installer.emit(10, titleNotification) }
        fx.installer.emit(11, kAXFocusedWindowChangedNotification as String)
        fx.clock.advance(ms: 290)
        check(fx.observer.takeRequest() == nil, "去抖时间内不交付")
        fx.clock.advance(ms: 10)
        check(fx.observer.takeRequest() == .partial(pids: [10, 11], reason: .activity), "多次通知合并为一次局部刷新")
        check(callbacks == 1, "合并后只通知一次有请求可取")
        fx.installer.emit(10, titleNotification)
        fx.clock.advance(ms: 300)
        check(fx.observer.takeRequest() == nil, "两次局部之间保留最小间隔")
        fx.clock.advance(ms: 100)
        check(fx.observer.takeRequest() == .partial(pids: [10], reason: .activity), "间隔结束后第二次局部刷新")
        fx.installer.emit(10, titleNotification)
        fx.clock.advance(ms: 400)
        check(fx.observer.takeRequest() == nil, "局部额度用尽后不再交付局部")
        check(fx.observer.fallbackActive, "额度用尽后依赖全量轮询")
        fx.clock.advance(ms: 1890)
        check(fx.observer.takeRequest() == nil, "未到全量期限不补扫")
        fx.clock.advance(ms: 10)
        check(fx.observer.takeRequest() == .full(.partialBudgetExhausted), "局部不能把全量无限推迟")
        fx.installer.emit(10, titleNotification)
        fx.clock.advance(ms: 1000)
        check(fx.observer.takeRequest() == nil, "全量尚未确认时不再交付")
        fx.observer.noteFullScanCompleted()
        check(fx.observer.takeRequest() == nil, "确认全量时不把积压立刻冲出去")
        check(!fx.observer.fallbackActive, "全量完成后取消兜底暂停")
        fx.clock.advance(ms: 300)
        check(fx.observer.takeRequest() == .partial(pids: [10], reason: .activity), "全量确认后恢复有界局部刷新")

        let preempt = ActivityFixture(debounceBudget)
        preempt.observer.replaceTargets([10])
        preempt.installer.emit(10, titleNotification)
        preempt.clock.advance(ms: 300)
        check(preempt.observer.takeRequest() == .partial(pids: [10], reason: .activity), "预抢测试的第一次局部")
        preempt.installer.emit(10, titleNotification)
        preempt.clock.advance(ms: 400)
        check(preempt.observer.takeRequest() == .partial(pids: [10], reason: .activity), "预抢测试的第二次局部")
        preempt.installer.emit(10, titleNotification)
        preempt.observer.noteFullScanCompleted()
        preempt.clock.advance(ms: 400)
        check(preempt.observer.takeRequest() == .partial(pids: [10], reason: .activity), "定时全量已确认时不再另补全量")
        preempt.clock.advance(ms: 3000)
        check(preempt.observer.takeRequest() == nil, "没有新通知就不再扫描")
    }

    static func testCoalesceCap(_ check: (Bool, String) -> Void) {
        let fx = ActivityFixture(debounceBudget)
        fx.observer.replaceTargets([4])
        fx.installer.emit(4, titleNotification)
        for _ in 0..<3 {
            fx.clock.advance(ms: 200)
            fx.installer.emit(4, titleNotification)
        }
        fx.clock.advance(ms: 200)
        check(fx.observer.takeRequest() == .partial(pids: [4], reason: .activity), "连续通知最多推迟到合并上限")
        fx.clock.advance(ms: 800)
        check(fx.observer.takeRequest() == nil, "同一次突发只交付一轮")
    }

    static func testPause(_ check: (Bool, String) -> Void) {
        let fx = ActivityFixture(fastBudget)
        fx.observer.replaceTargets([10])
        fx.installer.emit(10, titleNotification)
        fx.observer.setPaused(true)
        fx.clock.advance(ms: 1000)
        check(fx.observer.takeRequest() == nil, "暂停期间通知不交付")
        check(fx.installer.installed == [10], "暂停不拆除观察者")
        fx.observer.setPaused(false)
        check(fx.observer.takeRequest() == nil, "恢复后先去抖")
        fx.clock.advance(ms: 100)
        check(fx.observer.takeRequest() == .partial(pids: [10], reason: .activity), "恢复后只交付一次合并结果")
        fx.installer.emit(10, valueNotification)
        fx.clock.advance(ms: 200)
        check(fx.observer.takeRequest() == nil, "输入值变化不触发刷新")
    }

    static func testRemovalAndStop(_ check: (Bool, String) -> Void) {
        let fx = ActivityFixture(fastBudget)
        fx.observer.replaceTargets([10, 11])
        let stale = fx.installer.handlers[10]
        fx.observer.replaceTargets([11])
        check(fx.installer.installed == [11], "去掉的 PID 会移除注册")
        fx.clock.advance(ms: 100)
        check(fx.observer.takeRequest() == .partial(pids: [10], reason: .processExited), "移除 PID 触发一次局部刷新")
        fx.observer.replaceTargets([10, 11])
        stale?(10, titleNotification)
        fx.clock.advance(ms: 500)
        check(fx.observer.takeRequest() == nil, "旧注册世代的回调被忽略")
        fx.observer.noteProcessExited(999)
        fx.clock.advance(ms: 200)
        check(fx.observer.takeRequest() == nil && fx.installer.installed == [10, 11], "未跟踪的进程退出被忽略")
        fx.observer.noteProcessExited(10)
        check(fx.installer.installed == [11], "进程退出时移除对应观察者")
        fx.clock.advance(ms: 100)
        check(fx.observer.takeRequest() == .partial(pids: [10], reason: .processExited), "进程退出进入局部刷新")
        fx.observer.noteProcessExited(10)
        fx.clock.advance(ms: 200)
        check(fx.observer.takeRequest() == nil, "重复的退出通知不重复刷新")
        let leftover = fx.installer.handlers[11]
        fx.observer.stop()
        check(fx.installer.installed.isEmpty, "停止后没有保留注册")
        check(fx.installer.calls.contains(.uninstallAll), "停止时卸载全部运行循环源")
        leftover?(11, titleNotification)
        fx.clock.advance(ms: 200)
        check(fx.observer.takeRequest() == nil, "停止后的回调不再刷新")
        fx.observer.replaceTargets([11])
        check(fx.installer.installed.isEmpty, "停止后不再接受新目标")
    }

    static func testUnsupportedPermissionAndSleep(_ check: (Bool, String) -> Void) {
        let fx = ActivityFixture(fastBudget)
        fx.installer.results[10] = .unsupported(unsupported: WindowActivityNotifications.structural)
        fx.observer.replaceTargets([10, 11])
        check(fx.observer.observingPIDs == [11], "不支持通知的 PID 不留观察者")
        check(fx.observer.pollingOnlyPIDs == [10], "不支持通知的 PID 改走轮询")
        check(fx.observer.fallbackActive, "存在轮询 PID 时兜底为真")
        check(fx.observer.takeRequest() == nil, "不支持本身不制造扫描请求")
        fx.installer.emit(10, titleNotification)
        fx.installer.emit(11, titleNotification)
        fx.clock.advance(ms: 100)
        check(fx.observer.takeRequest() == .partial(pids: [11], reason: .activity), "其他 PID 的通知仍然有效")

        let denied = ActivityFixture(fastBudget)
        denied.installer.results[12] = .permissionDenied
        denied.observer.replaceTargets([12])
        check(denied.observer.observingPIDs.isEmpty, "权限失败时不保留观察者")
        check(denied.observer.takeRequest() == .full(.permissionRevoked), "权限撤销交付一次全量")
        check(denied.observer.fallbackActive, "权限撤销后必须继续轮询")
        denied.observer.noteAccessibilityTrusted(false)
        check(denied.observer.takeRequest() == nil, "重复的权限撤销不重复交付")
        denied.installer.results[12] = nil
        denied.observer.noteAccessibilityTrusted(true)
        check(denied.observer.observingPIDs == [12], "权限恢复后重新注册")
        check(!denied.observer.fallbackActive, "权限恢复且注册成功后取消兜底")
        check(denied.observer.takeRequest() == nil, "权限恢复不额外制造全量")

        let paused = ActivityFixture(fastBudget)
        paused.observer.replaceTargets([8])
        paused.observer.setPaused(true)
        paused.observer.noteAccessibilityTrusted(false)
        check(paused.installer.installed.isEmpty, "暂停中的权限撤销也会清理注册")
        paused.clock.advance(ms: 500)
        check(paused.observer.takeRequest() == nil, "暂停时不交付权限全量")
        paused.observer.setPaused(false)
        check(paused.observer.takeRequest() == .full(.permissionRevoked), "恢复暂停后交付积压的权限全量")

        let restored = ActivityFixture(fastBudget)
        restored.observer.replaceTargets([8])
        restored.observer.setPaused(true)
        restored.observer.noteAccessibilityTrusted(false)
        restored.observer.noteAccessibilityTrusted(true)
        restored.observer.setPaused(false)
        check(restored.observer.takeRequest() == nil, "暂停期间权限已经恢复就不再交付撤销")
        check(restored.observer.observingPIDs == [8], "取消暂停时按恢复后的权限重新注册")

        let sleep = ActivityFixture(fastBudget)
        sleep.observer.replaceTargets([8])
        sleep.observer.noteSleep()
        check(sleep.installer.installed.isEmpty, "休眠时拆除观察者和运行循环源")
        sleep.installer.emit(8, titleNotification)
        sleep.clock.advance(ms: 500)
        check(sleep.observer.takeRequest() == nil, "休眠期间不交付")
        sleep.observer.noteWake()
        check(sleep.observer.observingPIDs == [8], "唤醒后按原 PID 重新注册")
        check(sleep.observer.takeRequest() == .full(.sleepWake), "唤醒交付一次全量")

        let sleepPaused = ActivityFixture(fastBudget)
        sleepPaused.observer.replaceTargets([8])
        sleepPaused.observer.setPaused(true)
        sleepPaused.observer.noteSleep()
        sleepPaused.observer.noteWake()
        check(sleepPaused.observer.takeRequest() == nil, "暂停中的唤醒先不交付")
        check(sleepPaused.observer.observingPIDs == [8], "暂停中的唤醒仍然重新注册")
        sleepPaused.observer.setPaused(false)
        check(sleepPaused.observer.takeRequest() == .full(.sleepWake), "取消暂停后交付唤醒全量")
    }

    static func testRebuild(_ check: (Bool, String) -> Void) {
        let fx = ActivityFixture(fastBudget)
        fx.observer.replaceTargets([5])
        let before = fx.installer.installCount()
        fx.installer.emit(5, destroyedNotification)
        fx.clock.advance(ms: 100)
        check(fx.installer.installCount() == before + 1, "窗口销毁后重新注册观察者")
        check(fx.observer.observingPIDs == [5], "重绑成功后继续观察")
        check(fx.observer.takeRequest() == .partial(pids: [5], reason: .windowRebuilt), "窗口重建触发局部重读")

        fx.installer.results[5] = .processUnavailable
        fx.installer.emit(5, createdNotification)
        fx.clock.advance(ms: 100)
        check(fx.observer.observingPIDs.isEmpty, "重绑失败后移除观察者")
        check(fx.observer.pollingOnlyPIDs == [5], "重绑失败改由轮询兜底")
        check(fx.observer.fallbackActive, "重绑失败时兜底为真")
        check(fx.observer.takeRequest() == .partial(pids: [5], reason: .windowRebuilt), "重绑失败仍安排一次局部重读")
    }

    static func testDirtyCap(_ check: (Bool, String) -> Void) {
        var budget = fastBudget
        budget.maxDirtyPIDs = 4
        let fx = ActivityFixture(budget)
        fx.observer.replaceTargets([1, 2, 3, 4, 5])
        for pid in [Int32(1), 2, 3, 4, 5] { fx.installer.emit(pid, titleNotification) }
        fx.clock.advance(ms: 100)
        check(fx.observer.takeRequest() == .partial(pids: [1, 2, 3, 4], reason: .activity), "单次局部 PID 有上限")
        fx.clock.advance(ms: 100)
        check(fx.observer.takeRequest() == .partial(pids: [5], reason: .activity), "超出的 PID 留到下一次有界局部")
    }

    static func testRealInstaller(_ check: (Bool, String) -> Void) {
        let transient = WindowActivityObserver(observeSystemEvents: true)
        transient.stop()
        check(true, "系统睡眠和进程退出观察可以注册后立即停止")

        let installer = AXWindowActivityInstaller()
        defer { installer.uninstallAll() }
        let dead = installer.install(pid: -1, notifications: WindowActivityNotifications.structural) { _, _ in
            preconditionFailure("已退出进程不能交付通知")
        }
        check(dead == .processUnavailable, "无效 PID 不创建观察者")
        check(installer.registeredPIDs.isEmpty, "无效 PID 不加入运行循环")
        if !installer.processIsAlive(2_100_000_001) {
            let missing = installer.install(pid: 2_100_000_001, notifications: WindowActivityNotifications.structural) { _, _ in
                preconditionFailure("不存在的进程不能交付通知")
            }
            check(missing == .processUnavailable, "不存在的 PID 直接失败")
        }
        let own = ProcessInfo.processInfo.processIdentifier
        let result = installer.install(pid: own, notifications: WindowActivityNotifications.structural) { _, _ in }
        switch result {
        case .observing(let supported, let unsupported):
            check(!supported.isEmpty, "可用时至少注册一类结构通知")
            check(Set(supported).isDisjoint(with: Set(unsupported)), "支持和不受支持的通知不重叠")
            check(!supported.contains(valueNotification), "真实注册不包含输入值通知")
            check(installer.registeredPIDs == [own], "成功后运行循环源只保留目标 PID")
        case .permissionDenied, .unsupported, .processUnavailable, .failed:
            check(installer.registeredPIDs.isEmpty, "注册失败不能把运行循环源留下来")
        }
        installer.uninstall(pid: own)
        installer.uninstallAll()
        check(installer.registeredPIDs.isEmpty, "移除后运行循环源被清空")
        installer.uninstall(pid: own)
        check(installer.registeredPIDs.isEmpty, "重复移除不会复活观察者")
    }
}

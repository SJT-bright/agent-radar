import Foundation

private final class AssertionProbe: KeepAwakeAssertionBackend {
    var nextID: UInt32 = 1
    var active: Set<UInt32> = []
    var creates: [KeepAwakeAssertionKind] = []
    var releases: [UInt32] = []
    var failCreate: Set<KeepAwakeAssertionKind> = []
    var failRelease: Set<UInt32> = []

    func create(_ kind: KeepAwakeAssertionKind) -> Result<UInt32, KeepAwakeFailure> {
        creates.append(kind)
        if failCreate.contains(kind) { return .failure(.init(message: "test create failure")) }
        defer { nextID += 1 }
        active.insert(nextID)
        return .success(nextID)
    }
    func isActive(_ id: UInt32) -> Bool { active.contains(id) }
    func release(_ id: UInt32) -> Result<Void, KeepAwakeFailure> {
        releases.append(id)
        if failRelease.contains(id) { return .failure(.init(message: "test release failure")) }
        active.remove(id)
        return .success(())
    }
}

@main
struct KeepAwakeChecks {
    static func main() {
        var count = 0
        func check(_ result: Bool, _ message: String) {
            precondition(result, message)
            count += 1
        }
        let backend = AssertionProbe()
        let controller = KeepAwakeController(backend: backend, observeLifecycle: false)
        var notifications = 0
        controller.onStatusChange = { _ in notifications += 1 }
        check(!controller.status.requested && backend.creates.isEmpty, "init must not enable without user preference")
        controller.setEnabled(true)
        check(controller.status.fullyActive && backend.active.count == 2, "both power assertions are held")
        check(notifications == 1, "publish actual activation")
        controller.setEnabled(true)
        controller.recheck()
        check(backend.creates.count == 2, "enable and recheck are idempotent")
        check(notifications == 1, "unchanged status does not churn the UI")
        backend.active.remove(1)
        controller.recheck()
        check(backend.creates.count == 3 && backend.active.count == 2, "lost assertion after wake is reacquired")
        check(controller.status.fullyActive, "reacquired state is verified")
        backend.failRelease.insert(2)
        controller.setEnabled(false)
        check(!controller.status.requested && controller.status.displayActive, "release failure remains visible")
        check(controller.status.summary == "夜间常亮关闭待重试", "do not report disabled while still keeping display awake")
        check(controller.status.errors.count == 1, "release failure has a reason")
        backend.failRelease.remove(2)
        controller.recheck()
        check(backend.active.isEmpty && !controller.status.hasActiveAssertion, "retry releases retained ownership")
        let releaseCount = backend.releases.count
        controller.setEnabled(false)
        check(backend.releases.count == releaseCount, "repeated disable does not double release")

        let partial = AssertionProbe()
        partial.failCreate.insert(.display)
        let second = KeepAwakeController(backend: partial, observeLifecycle: false)
        second.setEnabled(true)
        check(!second.status.fullyActive && second.status.systemActive && !second.status.displayActive,
              "partial creation must never be presented as full keep-awake")
        check(second.status.errors.count == 1, "partial failure carries the real cause")
        partial.failCreate.remove(.display)
        second.recheck()
        check(second.status.fullyActive && partial.creates.count == 3, "retry only creates the missing assertion")
        second.stop()
        check(partial.active.isEmpty && !second.status.requested, "stop releases both and clears request")

        let failing = AssertionProbe()
        var third: KeepAwakeController? = KeepAwakeController(backend: failing, observeLifecycle: false)
        third?.setEnabled(true)
        failing.active.remove(1)
        failing.failRelease.insert(1)
        third?.recheck()
        check(failing.creates.count == 2, "failed stale release cannot create duplicate ownership")
        check(third?.status.errors.isEmpty == false, "stale release failure is observable")
        failing.failRelease.remove(1)
        third?.recheck()
        check(third?.status.fullyActive == true, "stale release retry restores coverage")
        third = nil
        check(failing.active.isEmpty, "deinit releases live assertions")
        print("keep awake checks: \(count) passed")
    }
}

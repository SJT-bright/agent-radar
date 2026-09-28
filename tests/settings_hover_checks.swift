import Foundation

@main
struct SettingsHoverChecks {
    static func main() {
        var checks = 0
        func check(_ condition: Bool, _ message: String) {
            precondition(condition, message)
            checks += 1
        }
        var state = SettingsMenuHoverState()
        check(state.entered(), "pointer entry schedules the menu")
        state.exited()
        check(!state.open(explicitClick: false), "leaving before dwell cancels hover opening")
        check(state.entered(), "reentry schedules again")
        check(state.open(explicitClick: false), "dwell opens the menu")
        check(!state.open(explicitClick: true), "click while tracking cannot open a second menu")
        state.exited()
        check(!state.armed, "menu tracking exit must not rearm")
        check(!state.entered(), "tracking enter cannot schedule another popup")
        check(state.closed(pointerInside: true), "close releases menu tracking")
        check(!state.entered(), "dismissal over trigger cannot immediately reopen")
        check(!state.open(explicitClick: false), "stale dwell after dismissal cannot reopen")
        check(state.open(explicitClick: true), "explicit click still works without pointer exit")
        state.closed(pointerInside: true)
        state.exited()
        check(state.entered(), "leaving and returning rearms hover")
        check(state.open(explicitClick: false), "rearmed hover opens again")
        state.closed(pointerInside: false)
        check(state.armed, "dismissal outside leaves the next entry enabled")
        check(!state.closed(pointerInside: true), "duplicate close callback does not change state")
        check(state.entered(), "entry after outside dismissal opens again")

        var panel = PanelHoverState()
        panel.presentationChanged(expanded: true, pointerInside: true)
        check(state.open(explicitClick: false), "open menu while moving outside panel")
        check(panel.update(expanded: true, pointerInside: state.isOpen, now: 1) == nil,
              "menu tracking keeps the expanded panel available")
        state.closed(pointerInside: false)
        check(panel.update(expanded: true, pointerInside: state.isOpen, now: 2) == false,
              "after menu dismissal outside the panel can collapse")
        print("settings hover checks: \(checks) passed")
    }
}

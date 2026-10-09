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

        var departure = SettingsMenuDepartureState()
        check(!departure.shouldClose(pointerInside: true, now: 10), "button and menu stay open while hovered")
        check(!departure.shouldClose(pointerInside: false, now: 10.1), "leaving starts grace instead of flashing closed")
        check(!departure.shouldClose(pointerInside: false, now: 10.2), "small gap allows movement into the menu")
        check(!departure.shouldClose(pointerInside: true, now: 10.25), "menu entry cancels the pending departure")
        check(departure.outsideSince == nil, "reentry clears stale departure deadline")
        check(!departure.shouldClose(pointerInside: false, now: 11), "new exit starts a new deadline")
        check(departure.shouldClose(pointerInside: false, now: 11.19), "leaving button and menu automatically closes")

        var keyboard = SettingsMenuDepartureState()
        keyboard.holdForKeyboard(pointer: CGPoint(x: 120, y: 300))
        check(!keyboard.shouldClose(pointerInside: false, pointer: CGPoint(x: 120, y: 300), now: 20),
              "AXPress does not require the hardware pointer to enter the trigger")
        check(!keyboard.shouldClose(pointerInside: false, pointer: CGPoint(x: 120, y: 300), now: 30),
              "keyboard menu remains available past the departure grace")
        check(!keyboard.shouldClose(pointerInside: false, pointer: CGPoint(x: 121, y: 300), now: 31),
              "tiny pointer drift does not dismiss keyboard navigation")
        check(!keyboard.shouldClose(pointerInside: false, pointer: CGPoint(x: 125, y: 300), now: 32),
              "real mouse movement resumes pointer control with a fresh grace")
        check(keyboard.keyboardPointer == nil, "real movement releases keyboard hold")
        check(keyboard.shouldClose(pointerInside: false, pointer: CGPoint(x: 125, y: 300), now: 32.19),
              "pointer departure closes normally after keyboard control ends")
        keyboard.holdForKeyboard(pointer: CGPoint(x: 120, y: 300))
        keyboard.resumePointer()
        check(keyboard.keyboardPointer == nil && keyboard.outsideSince == nil,
              "entering an actual menu row resumes pointer tracking immediately")
        print("settings hover checks: \(checks) passed")
    }
}

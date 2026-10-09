import Foundation

/// Pointer-driven presentation for the whole floating panel. The small
/// per-application disclosures are deliberately outside this state machine.
struct PanelHoverState {
    private(set) var enteredExpanded = false
    private(set) var suppressExpandUntilExit = false

    mutating func presentationChanged(expanded: Bool, pointerInside: Bool) {
        enteredExpanded = expanded && pointerInside
        suppressExpandUntilExit = !expanded && pointerInside
    }

    /// Returns a requested panel presentation, or nil if nothing should change.
    /// 指针离开展开面板立即收回，无宽限；拖动面板期间视为在面板内，
    /// 避免快速拖动掠过面板边界时被误收起。
    mutating func update(expanded: Bool, pointerInside: Bool,
                         dragging: Bool = false, now: TimeInterval) -> Bool? {
        if expanded {
            if pointerInside || dragging {
                enteredExpanded = true
                return nil
            }
            guard enteredExpanded else { return nil }
            enteredExpanded = false
            return false
        }

        enteredExpanded = false
        if !pointerInside {
            suppressExpandUntilExit = false
            return nil
        }
        guard !suppressExpandUntilExit else { return nil }
        return true
    }
}

/// Shared by hover entry, click and dismissal. The popup is asynchronous;
/// dismissal over its trigger must still require a real exit before reopening.
struct SettingsMenuHoverState {
    private(set) var pointerInside = false
    private(set) var isOpen = false
    private(set) var armed = true

    mutating func entered() -> Bool {
        pointerInside = true
        return armed && !isOpen
    }

    mutating func exited() {
        pointerInside = false
        if !isOpen { armed = true }
    }

    mutating func open(explicitClick: Bool) -> Bool {
        guard !isOpen, explicitClick || (pointerInside && armed) else { return false }
        isOpen = true
        armed = false
        return true
    }

    @discardableResult
    mutating func closed(pointerInside: Bool) -> Bool {
        guard isOpen else { return false }
        isOpen = false
        self.pointerInside = pointerInside
        armed = !pointerInside
        return true
    }
}

/// A brief grace period spans the small gap from trigger to menu and between
/// submenu windows. It is retargetable rather than a queue of delayed closes.
struct SettingsMenuDepartureState {
    static let grace: TimeInterval = 0.18
    private(set) var outsideSince: TimeInterval?
    private(set) var keyboardPointer: CGPoint?

    /// VoiceOver/keyboard activation need not move the hardware pointer onto
    /// the trigger. Keep the menu available until pointer control resumes.
    mutating func holdForKeyboard(pointer: CGPoint) {
        keyboardPointer = pointer
        outsideSince = nil
    }

    mutating func resumePointer() {
        keyboardPointer = nil
        outsideSince = nil
    }

    mutating func shouldClose(pointerInside: Bool, pointer: CGPoint? = nil, now: TimeInterval) -> Bool {
        if let origin = keyboardPointer {
            guard let pointer, hypot(pointer.x - origin.x, pointer.y - origin.y) >= 2 else { return false }
            resumePointer()
        }
        if pointerInside { outsideSince = nil; return false }
        guard let outsideSince else { self.outsideSince = now; return false }
        return now - outsideSince >= Self.grace
    }
}

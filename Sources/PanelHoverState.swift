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

/// Pure interaction state shared by native hover entry, click and menu dismissal.
/// A menu owns the pointer while tracking, so its generated exit events must not
/// arm another popup before dismissal.
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

import SwiftUI

/// Shared feedback for every in-window action. Native menus retain system highlighting.
struct RadarButtonStyle: ButtonStyle {
    var cornerRadius: CGFloat = 7
    var inset: CGFloat = 0
    var hoverOpacity: Double = 0.18

    func makeBody(configuration: Configuration) -> some View {
        Feedback(configuration: configuration, cornerRadius: cornerRadius,
                 inset: inset, hoverOpacity: hoverOpacity)
    }

    private struct Feedback: View {
        let configuration: ButtonStyle.Configuration
        let cornerRadius: CGFloat
        let inset: CGFloat
        let hoverOpacity: Double
        @State private var hovered = false
        @Environment(\.isEnabled) private var enabled
        @Environment(\.accessibilityReduceMotion) private var reduceMotion

        var body: some View {
            let active = enabled && (hovered || configuration.isPressed)
            configuration.label
                .padding(.horizontal, inset).padding(.vertical, inset > 0 ? 5 : 0)
                .background(RoundedRectangle(cornerRadius: cornerRadius)
                    .fill(.white.opacity(active ? (configuration.isPressed ? 0.28 : hoverOpacity) : 0)))
                .overlay(RoundedRectangle(cornerRadius: cornerRadius)
                    .strokeBorder(.white.opacity(active ? 0.42 : 0), lineWidth: 0.7)
                    .allowsHitTesting(false))
                .contentShape(RoundedRectangle(cornerRadius: cornerRadius))
                .opacity(enabled ? 1 : 0.45)
                .scaleEffect(enabled && configuration.isPressed && !reduceMotion ? 0.96 : 1)
                .onHover { hovered = $0 }
                .animation(reduceMotion ? nil : .easeOut(duration: 0.12), value: active)
                .animation(reduceMotion ? nil : .easeOut(duration: 0.08), value: configuration.isPressed)
        }
    }
}

/// Checkboxes, steppers and other native controls keep their semantics and hit areas.
private struct RadarHoverHighlight: ViewModifier {
    @State private var hovered = false
    @Environment(\.isEnabled) private var enabled
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    func body(content: Content) -> some View {
        content
            .background(RoundedRectangle(cornerRadius: 6).fill(.white.opacity(hovered && enabled ? 0.12 : 0)))
            .overlay(RoundedRectangle(cornerRadius: 6)
                .strokeBorder(.white.opacity(hovered && enabled ? 0.3 : 0), lineWidth: 0.7)
                .allowsHitTesting(false))
            .onHover { hovered = $0 }
            .animation(reduceMotion ? nil : .easeOut(duration: 0.12), value: hovered && enabled)
    }
}

extension View {
    func radarHoverHighlight() -> some View { modifier(RadarHoverHighlight()) }
}

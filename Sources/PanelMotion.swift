import Foundation

/// Analytic critically damped spring: retargeting preserves position and velocity.
/// No animation queue or input lock; repeated clicks immediately change direction.
struct PanelMotion {
    var width: Double
    var height: Double
    var widthVelocity = 0.0
    var heightVelocity = 0.0
    var target: NSSize

    init(size: NSSize, target: NSSize) {
        width = size.width
        height = size.height
        self.target = target
    }

    mutating func advance(by seconds: Double) -> NSSize {
        let dt = min(max(seconds, 0), 0.05)
        let omega = 22.0
        func step(_ value: Double, _ velocity: Double, _ target: Double) -> (Double, Double) {
            let offset = value - target
            let c = velocity + omega * offset
            let decay = exp(-omega * dt)
            return (target + (offset + c * dt) * decay, (velocity - omega * c * dt) * decay)
        }
        (width, widthVelocity) = step(width, widthVelocity, target.width)
        (height, heightVelocity) = step(height, heightVelocity, target.height)
        return NSSize(width: width, height: height)
    }

    var settled: Bool { abs(width - target.width) < 0.25 && abs(height - target.height) < 0.5 }
}

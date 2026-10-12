namespace AgentRadar.Windows.Ui;

/// <summary>
/// Pointer presentation for the floating panel. Expand waits 80ms so a pass
/// across the header does not open it. Collapse waits 180ms. A collapse while
/// the pointer is still inside stays closed until the pointer really leaves.
/// </summary>
internal sealed class PanelHover
{
    public const double ExpandDelay = 0.08;
    public const double CollapseDelay = 0.18;

    private bool _enteredExpanded;
    private bool _suppressUntilExit;
    private double? _insideSince;
    private double? _outsideSince;

    public void PresentationChanged(bool expanded, bool pointerInside)
    {
        _enteredExpanded = expanded && pointerInside;
        _suppressUntilExit = !expanded && pointerInside;
        _insideSince = null;
        _outsideSince = null;
    }

    public bool? Update(bool expanded, bool pointerInside, bool dragging, double now)
    {
        if (expanded)
        {
            _insideSince = null;
            if (pointerInside || dragging)
            {
                _enteredExpanded = true;
                _outsideSince = null;
                return null;
            }
            if (!_enteredExpanded) return null;
            _outsideSince ??= now;
            if (now - _outsideSince.Value < CollapseDelay) return null;
            _enteredExpanded = false;
            _outsideSince = null;
            return false;
        }

        _enteredExpanded = false;
        _outsideSince = null;
        if (!pointerInside)
        {
            _suppressUntilExit = false;
            _insideSince = null;
            return null;
        }
        if (_suppressUntilExit) return null;
        _insideSince ??= now;
        if (now - _insideSince.Value < ExpandDelay) return null;
        _insideSince = null;
        return true;
    }
}

internal sealed class PanelMotion
{
    public double Width;
    public double Height;
    public double WidthVelocity;
    public double HeightVelocity;
    public double TargetWidth;
    public double TargetHeight;

    public PanelMotion(double width, double height, double targetWidth, double targetHeight)
    {
        Width = width;
        Height = height;
        TargetWidth = targetWidth;
        TargetHeight = targetHeight;
    }

    public bool Settled => System.Math.Abs(Width - TargetWidth) < 0.25 && System.Math.Abs(Height - TargetHeight) < 0.5;

    public void Advance(double seconds)
    {
        var dt = System.Math.Clamp(seconds, 0, 0.05);
        const double omega = 22.0;
        Step(ref Width, ref WidthVelocity, TargetWidth, omega, dt);
        Step(ref Height, ref HeightVelocity, TargetHeight, omega, dt);
    }

    private static void Step(ref double value, ref double velocity, double target, double omega, double dt)
    {
        var offset = value - target;
        var c = velocity + omega * offset;
        var decay = System.Math.Exp(-omega * dt);
        value = target + (offset + c * dt) * decay;
        velocity = (velocity - omega * c * dt) * decay;
    }
}

internal static class MotionPreference
{
    public static bool Reduce =>
        !System.Windows.SystemParameters.ClientAreaAnimation || !System.Windows.SystemParameters.MenuAnimation;
}

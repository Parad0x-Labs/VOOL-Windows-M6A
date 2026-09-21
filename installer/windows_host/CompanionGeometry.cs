namespace Vool.WindowsHost;

internal readonly record struct PetRect(double X, double Y, double Width, double Height)
{
    internal double Right => X + Width;
    internal double Bottom => Y + Height;

    internal bool Contains(double x, double y) =>
        x >= X && x <= Right && y >= Y && y <= Bottom;
}

internal static class CompanionGeometry
{
    internal const double WindowSize = 176;
    internal const double CanvasSize = 132;
    internal const double ControlWidth = 72;
    internal const double ControlHeight = 22;
    internal const double ControlInset = 4;
    internal const double MinimumVisible = 40;

    internal static bool PointHitsPet(double screenX, double screenY, PetRect frame,
        PetRect? answerRegion = null)
    {
        if (answerRegion?.Contains(screenX, screenY) == true)
            return false;
        var x = screenX - frame.X;
        var y = screenY - frame.Y;
        var scaleX = frame.Width / WindowSize;
        var scaleY = frame.Height / WindowSize;
        var canvasRegion = CanvasRegion(frame);
        var control = new PetRect(frame.Width - (ControlInset + ControlWidth) * scaleX,
            ControlInset * scaleY, ControlWidth * scaleX, ControlHeight * scaleY);
        return canvasRegion.Contains(x, y) || control.Contains(x, y);
    }

    internal static bool PointHitsCanvas(double screenX, double screenY, PetRect frame) =>
        CanvasRegion(frame).Contains(screenX - frame.X, screenY - frame.Y);

    private static PetRect CanvasRegion(PetRect frame)
    {
        var scaleX = frame.Width / WindowSize;
        var scaleY = frame.Height / WindowSize;
        return new PetRect((frame.Width - CanvasSize * scaleX) / 2,
            (frame.Height - CanvasSize * scaleY) / 2, CanvasSize * scaleX, CanvasSize * scaleY);
    }

    internal static PetRect Clamp(PetRect frame, IReadOnlyList<PetRect> screens)
    {
        if (screens.Count == 0)
            return frame;
        foreach (var screen in screens)
        {
            var overlapWidth = Math.Max(0, Math.Min(frame.Right, screen.Right) - Math.Max(frame.X, screen.X));
            var overlapHeight = Math.Max(0, Math.Min(frame.Bottom, screen.Bottom) - Math.Max(frame.Y, screen.Y));
            if (overlapWidth >= Math.Min(MinimumVisible, frame.Width)
                && overlapHeight >= Math.Min(MinimumVisible, frame.Height))
                return frame;
        }
        var centerX = frame.X + frame.Width / 2;
        var centerY = frame.Y + frame.Height / 2;
        var nearest = screens.MinBy(screen =>
            Math.Pow(centerX - (screen.X + screen.Width / 2), 2)
            + Math.Pow(centerY - (screen.Y + screen.Height / 2), 2));
        var x = Math.Clamp(frame.X, nearest.X, nearest.X + Math.Max(0, nearest.Width - frame.Width));
        var y = Math.Clamp(frame.Y, nearest.Y, nearest.Y + Math.Max(0, nearest.Height - frame.Height));
        return frame with { X = x, Y = y };
    }

    internal static PetRect DefaultFrame(PetRect primary) => new(
        primary.Right - WindowSize - 48,
        primary.Bottom - WindowSize - 96,
        WindowSize,
        WindowSize);

    internal static bool TryYieldRect(double x, double y, double width, double height, out PetRect rect)
    {
        rect = default;
        if (!double.IsFinite(x) || !double.IsFinite(y) || !double.IsFinite(width)
            || !double.IsFinite(height) || width <= 0 || height <= 0
            || width > 20000 || height > 20000)
            return false;
        rect = new PetRect(x, y, width, height);
        return true;
    }
}

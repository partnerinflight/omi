namespace SecondBrain.Status;

public readonly record struct Bounds(double Left, double Top, double Width, double Height);

public static class WindowBounds
{
    // Fits saved window bounds onto the current screen: at least the minimum size, no larger than
    // the screen, and moved back into view if the monitor it was on is gone.
    public static Bounds Fit(Bounds saved, Bounds screen, double minWidth, double minHeight)
    {
        double width = Math.Min(Math.Max(saved.Width, minWidth), Math.Max(minWidth, screen.Width));
        double height = Math.Min(Math.Max(saved.Height, minHeight), Math.Max(minHeight, screen.Height));
        double left = Math.Clamp(saved.Left, screen.Left, Math.Max(screen.Left, screen.Left + screen.Width - width));
        double top = Math.Clamp(saved.Top, screen.Top, Math.Max(screen.Top, screen.Top + screen.Height - height));
        return new(left, top, width, height);
    }
}

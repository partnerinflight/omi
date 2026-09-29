namespace SecondBrain.Status.Tests;

static class WindowBoundsTests
{
    static readonly Bounds Screen = new(0, 0, 1920, 1040);

    public static void Run()
    {
        var inside = new Bounds(100, 80, 1100, 760);
        Check.Equal(inside, WindowBounds.Fit(inside, Screen, 900, 620), "bounds already on screen are kept");
        Check.Equal(new Bounds(820, 80, 1100, 760), WindowBounds.Fit(new(3000, 80, 1100, 760), Screen, 900, 620), "window from a removed right-hand monitor is pulled back");
        Check.Equal(new Bounds(0, 0, 1920, 1040), WindowBounds.Fit(new(-50, -50, 3000, 2000), Screen, 900, 620), "oversized window shrinks to the screen");
        Check.Equal(new Bounds(100, 80, 900, 620), WindowBounds.Fit(new(100, 80, 400, 300), Screen, 900, 620), "undersized window grows to the minimum");
        var twoMonitors = new Bounds(-1920, 0, 3840, 1080);
        Check.Equal(new Bounds(-1500, 100, 1100, 760), WindowBounds.Fit(new(-1500, 100, 1100, 760), twoMonitors, 900, 620), "left-hand monitor with negative coordinates");
        Check.Equal(new Bounds(0, 0, 900, 620), WindowBounds.Fit(new(50, 50, 1100, 760), new Bounds(0, 0, 800, 600), 900, 620), "screen smaller than the minimum pins to the top-left");
    }
}

using System.Text.Json;
using System.Windows;
using SecondBrain.Status;

namespace SecondBrain.Tray;

// Best-effort per-user window placement; failures fall back to defaults.
sealed record WindowSettings(double Left, double Top, double Width, double Height, bool Maximized, string Page)
{
    static readonly string SettingsPath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "SecondBrain", "tray.json");

    public static WindowSettings? Load()
    {
        try
        {
            var settings = JsonSerializer.Deserialize<WindowSettings>(File.ReadAllText(SettingsPath));
            return settings is not null && double.IsFinite(settings.Left) && double.IsFinite(settings.Top) && settings.Width > 0 && settings.Height > 0 ? settings : null;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or JsonException) { return null; }
    }

    public static void Save(Window window, string page)
    {
        var bounds = window.WindowState == WindowState.Normal ? new Rect(window.Left, window.Top, window.ActualWidth, window.ActualHeight) : window.RestoreBounds;
        if (bounds.IsEmpty || bounds.Width <= 0) return;
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(SettingsPath)!);
            var settings = new WindowSettings(bounds.Left, bounds.Top, bounds.Width, bounds.Height, window.WindowState == WindowState.Maximized, page);
            File.WriteAllText(SettingsPath, JsonSerializer.Serialize(settings));
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException) { }
    }

    public void ApplyTo(Window window)
    {
        var screen = new Bounds(SystemParameters.VirtualScreenLeft, SystemParameters.VirtualScreenTop, SystemParameters.VirtualScreenWidth, SystemParameters.VirtualScreenHeight);
        var fit = WindowBounds.Fit(new Bounds(Left, Top, Width, Height), screen, window.MinWidth, window.MinHeight);
        window.WindowStartupLocation = WindowStartupLocation.Manual;
        (window.Left, window.Top, window.Width, window.Height) = (fit.Left, fit.Top, fit.Width, fit.Height);
        if (Maximized) window.WindowState = WindowState.Maximized;
    }
}

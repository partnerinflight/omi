using System.Windows;
using System.Windows.Media;
using SecondBrain.Status;

namespace SecondBrain.Tray;

static class Theme
{
    // Fluent's theme-aware status colors; they follow light/dark mode.
    public static Brush HealthBrush(Health health) => (Brush)Application.Current.FindResource(health switch
    {
        Health.Running => "SystemFillColorSuccessBrush",
        Health.Attention => "SystemFillColorCautionBrush",
        _ => "SystemFillColorCriticalBrush",
    });
}

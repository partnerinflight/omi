using System.Windows;
using SecondBrain.Status;
using Forms = System.Windows.Forms;

namespace SecondBrain.Tray;

public partial class HoverCard : Window
{
    public HoverCard() => InitializeComponent();

    public void Update(ServiceStatus status)
    {
        Dot.Fill = Theme.HealthBrush(status.Health);
        TitleText.Text = status.Title;
        ReasonText.Text = status.Reason;
        PendingText.Text = status.Pending.ToString();
        ProcessingText.Text = status.Processing.ToString();
        FailedText.Text = status.Failed.ToString();
        SpeakersText.Text = status.UnidentifiedSpeakers == 0
            ? "No speakers waiting to be identified"
            : $"{Formats.Plural(status.UnidentifiedSpeakers, "speaker")} to identify";
    }

    public void ShowNearCursor()
    {
        if (IsVisible) return;
        // Show off-screen first so the measured size is known before positioning.
        Left = -32000;
        Top = -32000;
        Show();
        // Cursor and working area are in device pixels; WPF positions in device-independent units.
        var toDip = PresentationSource.FromVisual(this)!.CompositionTarget!.TransformFromDevice;
        var cursor = Forms.Cursor.Position;
        var area = Forms.Screen.FromPoint(cursor).WorkingArea;
        var at = toDip.Transform(new Point(cursor.X, cursor.Y));
        var topLeft = toDip.Transform(new Point(area.Left, area.Top));
        var bottomRight = toDip.Transform(new Point(area.Right, area.Bottom));
        Left = Math.Clamp(at.X - ActualWidth, topLeft.X, Math.Max(topLeft.X, bottomRight.X - ActualWidth));
        Top = Math.Clamp(at.Y - ActualHeight - 12, topLeft.Y, Math.Max(topLeft.Y, bottomRight.Y - ActualHeight));
    }
}

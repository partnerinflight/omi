using System.Windows;
using System.Windows.Controls;
using SecondBrain.Status;

namespace SecondBrain.Tray;

public sealed record StageChip(string Name, bool Current, bool Done)
{
    public string Label => Done ? "✓ " + Name : Name;
}

public sealed record EventRow(string Time, string Kind, string Detail);

public partial class OverviewPage : UserControl
{
    // Stage names written by src/second_brain/runtime.py STAGES, in pipeline order.
    static readonly string[] Stages = ["segmentation", "transcribing", "scoring", "refining", "routing", "publishing", "speakers"];
    string? shownStage = "";
    IReadOnlyList<StatusEvent>? shownEvents;

    public event Action? ReviewSpeakersRequested;

    public OverviewPage() => InitializeComponent();

    void Review_Click(object sender, RoutedEventArgs e) => ReviewSpeakersRequested?.Invoke();

    public void Update(ServiceStatus status)
    {
        var now = DateTimeOffset.UtcNow;
        bool up = status.Health is Health.Running or Health.Attention;
        HealthDot.Fill = Theme.HealthBrush(status.Health);
        TitleText.Text = status.Health switch
        {
            Health.Running => "Running",
            Health.Attention => "Needs attention",
            Health.Stopped => "Stopped",
            _ => "Unavailable",
        };
        ReasonText.Text = status.Reason;
        ReceiverValue.Text = !up ? "—" : status.Receiver.Listening ? "Listening" : "Offline";
        ReceiverCaption.Text = up
            ? $"Port {status.Receiver.Port} · {Formats.Plural(status.Receiver.ActiveUploads, "active upload")} · {status.Receiver.SessionsOk} ok / {status.Receiver.SessionsFailed} failed sessions"
            : "No live status";
        PendingValue.Text = status.Pending.ToString();
        QueueCaption.Text = $"pending · {status.Processing} processing";
        CompleteValue.Text = status.Complete.ToString();
        FailedValue.Text = status.Failed.ToString();
        if (status.Failed > 0) FailedValue.SetResourceReference(TextBlock.ForegroundProperty, "SystemFillColorCriticalBrush");
        else FailedValue.ClearValue(TextBlock.ForegroundProperty);

        ActivityText.Text = up ? status.Activity : "Not running";
        InStageText.Text = up && status.InStage is { } inStage ? $"for {Formats.Duration(inStage)}" : "";
        Busy.Visibility = up && status.Stage is not null ? Visibility.Visible : Visibility.Collapsed;
        if (status.Stage != shownStage)
        {
            shownStage = status.Stage;
            int index = Array.IndexOf(Stages, status.Stage);
            StageStrip.ItemsSource = Stages.Select((name, i) => new StageChip(name, i == index, index >= 0 && i < index)).ToList();
        }

        string saved = $"{status.People} {(status.People == 1 ? "person" : "people")} saved";
        SpeakersText.Text = status.UnidentifiedSpeakers == 0
            ? $"No speakers waiting · {saved}"
            : $"{Formats.Plural(status.UnidentifiedSpeakers, "speaker")} to identify · {saved}";

        if (shownEvents is null || !status.Events.SequenceEqual(shownEvents))
        {
            shownEvents = status.Events;
            RecentEvents.ItemsSource = status.Events.Take(5).Select(e => new EventRow(Formats.Time(e.Time, now), e.Kind, e.Detail)).ToList();
            NoEventsText.Visibility = status.Events.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        }
    }
}

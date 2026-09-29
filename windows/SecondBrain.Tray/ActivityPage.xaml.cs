using System.Windows.Controls;
using SecondBrain.Status;

namespace SecondBrain.Tray;

public sealed record JobRow(string Job, string State, string Stage, int Attempts, string Error, string Updated);

public partial class ActivityPage : UserControl
{
    IReadOnlyList<StatusEvent>? shownEvents;
    IReadOnlyList<JobInfo>? shownJobs;

    public ActivityPage() => InitializeComponent();

    public void Update(ServiceStatus status)
    {
        var now = DateTimeOffset.UtcNow;
        if (shownEvents is null || !status.Events.SequenceEqual(shownEvents))
        {
            shownEvents = status.Events;
            EventsGrid.ItemsSource = status.Events.Select(e => new EventRow(Formats.Time(e.Time, now), e.Kind, e.Detail)).ToList();
        }
        if (shownJobs is null || !status.Jobs.SequenceEqual(shownJobs))
        {
            shownJobs = status.Jobs;
            JobsGrid.ItemsSource = status.Jobs.Select(j => new JobRow(j.ShortId, j.State, j.Stage, j.Attempts, j.Error ?? "", Formats.Time(j.Updated, now))).ToList();
        }
    }
}

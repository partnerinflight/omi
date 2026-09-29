using System.ComponentModel;
using System.Windows;
using System.Windows.Controls;
using SecondBrain.Status;

namespace SecondBrain.Tray;

public partial class MainWindow : Window
{
    readonly OverviewPage overview = new();
    readonly ActivityPage activity = new();
    readonly SpeakersPage speakers;
    readonly PeoplePage people;
    readonly Dictionary<string, FrameworkElement> pages;
    bool exiting;

    internal MainWindow(ReviewSession session)
    {
        InitializeComponent();
        speakers = new SpeakersPage(session);
        people = new PeoplePage(session);
        pages = new() { ["Overview"] = overview, ["Speakers"] = speakers, ["People"] = people, ["Activity"] = activity };
        people.OpenSpeakerRequested += id =>
        {
            Navigate("Speakers");
            speakers.Select(id);
        };
        overview.ReviewSpeakersRequested += () => Navigate("Speakers");
        // Poll the speaker catalog only while the window is visible.
        IsVisibleChanged += (_, _) =>
        {
            if (IsVisible) session.Start();
            else { session.Stop(); speakers.StopPlayback(); }
        };
        var settings = WindowSettings.Load();
        settings?.ApplyTo(this);
        Navigate(settings?.Page ?? "Overview");
    }

    public string CurrentPage => (Nav.SelectedItem as ListBoxItem)?.Tag as string ?? "Overview";

    public void Navigate(string page) =>
        Nav.SelectedItem = Nav.Items.OfType<ListBoxItem>().FirstOrDefault(i => (string)i.Tag == page) ?? Nav.Items[0];

    void Nav_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (Nav.SelectedItem is ListBoxItem { Tag: string page } && pages.TryGetValue(page, out var element)) PageHost.Content = element;
    }

    public void Update(ServiceStatus status)
    {
        ServiceDot.Fill = Theme.HealthBrush(status.Health);
        ServiceText.Text = status.Health switch
        {
            Health.Running => "Service running",
            Health.Attention => "Service needs attention",
            Health.Stopped => "Service stopped",
            _ => "Service unavailable",
        };
        SpeakersBadge.Visibility = status.UnidentifiedSpeakers > 0 ? Visibility.Visible : Visibility.Collapsed;
        SpeakersBadgeText.Text = status.UnidentifiedSpeakers.ToString();
        overview.Update(status);
        activity.Update(status);
    }

    protected override void OnClosing(CancelEventArgs e)
    {
        WindowSettings.Save(this, CurrentPage);
        // Closing only hides the window; the tray keeps running until Quit.
        if (!exiting) { e.Cancel = true; Hide(); }
        base.OnClosing(e);
    }

    public void CloseForReal()
    {
        exiting = true;
        Close();
    }
}

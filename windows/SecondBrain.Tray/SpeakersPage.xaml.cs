using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Input;
using System.Windows.Media;
using SecondBrain.Status;

namespace SecondBrain.Tray;

public sealed record SpeakerRow(Speaker Speaker, string GroupHeader)
{
    public string Title => Speaker.Title;
    public string StateText => Formats.State(Speaker);
}

public partial class SpeakersPage : UserControl
{
    readonly ReviewSession session;
    readonly ClipPlayer player = new();
    readonly bool ready;
    bool filling;

    internal SpeakersPage(ReviewSession session)
    {
        this.session = session;
        InitializeComponent();
        ready = true;
        session.CatalogChanged += Fill;
        session.MessageChanged += ShowMessage;
        session.Applied += OnApplied;
        PreviewKeyDown += OnKey;
        Unloaded += (_, _) => player.Stop();
        ShowMessage();
        Fill();
    }

    Speaker? Selected => (SpeakerList.SelectedItem as SpeakerRow)?.Speaker;

    public void StopPlayback() => player.Stop();

    public void Select(string observationId)
    {
        if (session.Catalog?.Find(observationId) is not { } speaker) return;
        if (!speaker.Unidentified) ShowAll.IsChecked = true;
        SearchBox.Text = "";
        SelectById(observationId);
    }

    void SelectById(string id)
    {
        var row = SpeakerList.Items.OfType<SpeakerRow>().FirstOrDefault(r => r.Speaker.Id == id);
        if (row is null) return;
        SpeakerList.SelectedItem = row;
        SpeakerList.ScrollIntoView(row);
    }

    void Fill()
    {
        if (!ready || session.Catalog is not { } catalog) return;
        string? selectedId = Selected?.Id;
        string typed = NameBox.Text;
        var scroll = FindScrollViewer(SpeakerList);
        double offset = scroll?.VerticalOffset ?? 0;
        bool unidentifiedOnly = UnidentifiedOnly.IsChecked == true;

        var rows = catalog.Recordings(unidentifiedOnly, SearchBox.Text)
            .SelectMany(r => r.Speakers.Select(s => new SpeakerRow(s, $"{r.Recorded} · {Formats.Plural(r.SpeakerCount, "speaker")} · {r.UnidentifiedCount} unidentified")))
            .ToList();
        var view = new ListCollectionView(rows);
        view.GroupDescriptions.Add(new PropertyGroupDescription("Speaker.Job"));
        filling = true;
        SpeakerList.ItemsSource = view;
        var again = rows.FirstOrDefault(r => r.Speaker.Id == selectedId) ?? rows.FirstOrDefault();
        SpeakerList.SelectedItem = again;
        NameBox.ItemsSource = catalog.People.Select(p => p.Name).ToList();
        filling = false;

        EmptyText.Text = catalog.Speakers.Length == 0 ? "No speakers to review yet. New recordings appear after transcription."
            : unidentifiedOnly && string.IsNullOrWhiteSpace(SearchBox.Text) ? "Everyone is identified. Choose All to review or correct names."
            : "No speakers match this search.";
        EmptyText.Visibility = rows.Count == 0 ? Visibility.Visible : Visibility.Collapsed;

        bool same = again is not null && again.Speaker.Id == selectedId;
        ShowSelected(keepClips: same);
        if (same) NameBox.Text = typed;
        if (scroll is not null)
        {
            SpeakerList.UpdateLayout();
            scroll.ScrollToVerticalOffset(offset);
        }
    }

    void ShowSelected(bool keepClips = false)
    {
        var speaker = Selected;
        DetailPanel.Visibility = speaker is null ? Visibility.Hidden : Visibility.Visible;
        if (!keepClips)
        {
            player.Stop();
            ClipList.ItemsSource = speaker?.Clips.Select(c => new ClipRow(c)).ToList();
            if (ClipList.Items.Count > 0) ClipList.SelectedIndex = 0;
            NameBox.Text = speaker?.Name ?? "";
        }
        if (speaker is null) return;
        SpeakerTitle.Text = speaker.Title;
        StateText.Text = Formats.State(speaker);
        RecordedText.Text = $"{speaker.Display} · recorded {speaker.Recorded} · {Formats.Plural(speaker.Clips.Length, "clip")}";
        EmbeddingText.Text = $"Voice matching: {speaker.EmbeddingStatus}";
    }

    void ShowMessage()
    {
        MessageText.Text = session.Message;
        AssignButton.IsEnabled = ClearButton.IsEnabled = !session.Pending;
    }

    void SpeakerList_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!filling) ShowSelected();
    }

    void Filter_Changed(object sender, RoutedEventArgs e) => Fill();

    void Search_TextChanged(object sender, TextChangedEventArgs e)
    {
        SearchHint.Visibility = SearchBox.Text.Length == 0 ? Visibility.Visible : Visibility.Collapsed;
        Fill();
    }

    void Assign_Click(object sender, RoutedEventArgs e) => Assign();

    void Clear_Click(object sender, RoutedEventArgs e)
    {
        if (Selected is { } speaker) session.Send("clear", speaker.Id, null, "");
    }

    void Assign()
    {
        if (Selected is not { } speaker) return;
        if (ReviewClient.ValidateName(NameBox.Text) is { } error) { session.Report(error); return; }
        string name = NameBox.Text.Trim();
        string? person = session.Catalog?.People.FirstOrDefault(p => string.Equals(p.Name, name, StringComparison.CurrentCultureIgnoreCase))?.Id;
        session.Send("assign", speaker.Id, person, name);
    }

    // Quick naming: after an acknowledged assignment in the Unidentified view, open the next unnamed speaker.
    void OnApplied(string action, string? observation)
    {
        if (action != "assign" || UnidentifiedOnly.IsChecked != true || session.Catalog is not { } catalog) return;
        if (catalog.NextUnidentified(observation, SearchBox.Text) is not { } next) return;
        SelectById(next.Id);
        NameBox.Text = "";
        NameBox.Focus();
    }

    void OnKey(object sender, KeyEventArgs e)
    {
        if (e.Key == Key.F && Keyboard.Modifiers == ModifierKeys.Control)
        {
            SearchBox.Focus();
            SearchBox.SelectAll();
            e.Handled = true;
        }
        else if (e.Key == Key.Enter && NameBox.IsKeyboardFocusWithin && !NameBox.IsDropDownOpen)
        {
            Assign();
            e.Handled = true;
        }
        else if (e.Key == Key.Space && Keyboard.FocusedElement is not TextBox && ClipList.SelectedItem is ClipRow clip)
        {
            Toggle(clip);
            e.Handled = true;
        }
    }

    void PlayButton_Click(object sender, RoutedEventArgs e)
    {
        if ((sender as FrameworkElement)?.DataContext is not ClipRow clip) return;
        ClipList.SelectedItem = clip;
        Toggle(clip);
    }

    void Toggle(ClipRow clip)
    {
        if (player.Current == clip) { player.Stop(); return; }
        try { player.Play(clip, session.Client.ClipPath(clip.Clip)); }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or InvalidOperationException or InvalidDataException)
        {
            session.Report("This clip could not be played. Refresh and try another clip.");
        }
    }

    static ScrollViewer? FindScrollViewer(DependencyObject root)
    {
        for (int i = 0; i < VisualTreeHelper.GetChildrenCount(root); i++)
        {
            var child = VisualTreeHelper.GetChild(root, i);
            if (child is ScrollViewer viewer) return viewer;
            if (FindScrollViewer(child) is { } found) return found;
        }
        return null;
    }
}

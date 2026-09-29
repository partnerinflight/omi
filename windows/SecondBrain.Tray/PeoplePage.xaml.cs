using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using SecondBrain.Status;

namespace SecondBrain.Tray;

public sealed record PersonRow(PersonSummary Summary)
{
    public string Name => Summary.Person.Name;
    public string Caption => $"{Summary.Confirmed} confirmed · {Summary.Matched} voice-matched · {Formats.Plural(Summary.Recordings, "recording")}";
}

public sealed record PersonSpeakerRow(string Id, string Title, string Caption);

public partial class PeoplePage : UserControl
{
    readonly ReviewSession session;
    readonly bool ready;
    bool filling;

    public event Action<string>? OpenSpeakerRequested;

    internal PeoplePage(ReviewSession session)
    {
        this.session = session;
        InitializeComponent();
        ready = true;
        session.CatalogChanged += Fill;
        session.MessageChanged += ShowMessage;
        ShowMessage();
        Fill();
    }

    PersonSummary? Selected => (PeopleList.SelectedItem as PersonRow)?.Summary;

    void Fill()
    {
        if (!ready || session.Catalog is not { } catalog) return;
        string? selectedId = Selected?.Person.Id;
        var rows = catalog.PersonSummaries().Select(s => new PersonRow(s)).ToList();
        filling = true;
        PeopleList.ItemsSource = rows;
        PeopleList.SelectedItem = rows.FirstOrDefault(r => r.Summary.Person.Id == selectedId) ?? rows.FirstOrDefault();
        filling = false;
        EmptyText.Visibility = rows.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
        ShowSelected(keepTypedName: Selected?.Person.Id == selectedId);
    }

    void ShowSelected(bool keepTypedName = false)
    {
        var summary = Selected;
        DetailPanel.Visibility = summary is null ? Visibility.Hidden : Visibility.Visible;
        if (summary is null) return;
        PersonTitle.Text = summary.Person.Name;
        PersonCaption.Text = new PersonRow(summary).Caption;
        if (!(keepTypedName && RenameBox.IsKeyboardFocusWithin)) RenameBox.Text = summary.Person.Name;
        var rows = summary.Speakers.Select(s => new PersonSpeakerRow(s.Id, $"{s.Display} · {s.Recorded}", Formats.State(s))).ToList();
        RowsList.ItemsSource = rows;
        NoRowsText.Visibility = rows.Count == 0 ? Visibility.Visible : Visibility.Collapsed;
    }

    void ShowMessage()
    {
        MessageText.Text = session.Message;
        RenameButton.IsEnabled = ForgetButton.IsEnabled = !session.Pending;
    }

    void PeopleList_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (!filling) ShowSelected();
    }

    void RenameBox_KeyDown(object sender, KeyEventArgs e)
    {
        if (e.Key != Key.Enter) return;
        Rename();
        e.Handled = true;
    }

    void Rename_Click(object sender, RoutedEventArgs e) => Rename();

    // The service keys rename/forget on person only; observation is sent as null.
    void Rename()
    {
        if (Selected is not { } summary) return;
        if (ReviewClient.ValidateName(RenameBox.Text) is { } error) { session.Report(error); return; }
        session.Send("rename", null, summary.Person.Id, RenameBox.Text.Trim());
    }

    void Forget_Click(object sender, RoutedEventArgs e)
    {
        if (Selected is not { } summary) return;
        var answer = MessageBox.Show(Window.GetWindow(this)!, $"Forget {summary.Person.Name}'s voice references and clear all their assignments?",
            "Forget person", MessageBoxButton.YesNo, MessageBoxImage.Warning);
        if (answer == MessageBoxResult.Yes) session.Send("forget", null, summary.Person.Id, "");
    }

    void Open_Click(object sender, RoutedEventArgs e)
    {
        if ((sender as FrameworkElement)?.DataContext is PersonSpeakerRow row) OpenSpeakerRequested?.Invoke(row.Id);
    }
}

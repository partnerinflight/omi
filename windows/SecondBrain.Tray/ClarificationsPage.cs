using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;
using SecondBrain.Status;

namespace SecondBrain.Tray;

sealed class ClarificationsPage : UserControl
{
    readonly ReviewClient client;
    readonly DispatcherTimer timer = new() { Interval = TimeSpan.FromSeconds(2) };
    readonly ListBox items = new() { DisplayMemberPath = "Text", MaxHeight = 180 };
    readonly TextBlock question = new() { TextWrapping = TextWrapping.Wrap, FontSize = 17, Margin = new Thickness(0,16,0,8) };
    readonly TextBlock location = new() { TextWrapping = TextWrapping.Wrap, Opacity = .7 };
    readonly TextBlock context = new() { TextWrapping = TextWrapping.Wrap };
    readonly TextBlock message = new() { TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0,12,0,0) };
    readonly TextBox answer = new() { TextWrapping = TextWrapping.Wrap, MinHeight = 85, MaxLength = 4000, Margin = new Thickness(0,8,0,8) };
    readonly Button save = new() { Content = "Save corrected note", Padding = new Thickness(12,6,12,6) };
    readonly Button dismiss = new() { Content = "Keep note as is", Padding = new Thickness(12,6,12,6), Margin = new Thickness(12,0,0,0) };
    string last = "";
    string? pending;
    bool live;

    public ClarificationsPage(ReviewClient client)
    {
        this.client = client;
        var panel = new StackPanel();
        panel.Children.Add(new TextBlock { Text = "Needs clarification", FontSize = 28, FontWeight = FontWeights.SemiBold });
        panel.Children.Add(new TextBlock { Text = "Add missing names or context so these notes make sense on their own.", TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0,8,0,20) });
        panel.Children.Add(items); panel.Children.Add(question); panel.Children.Add(location);
        panel.Children.Add(new Expander { Header = "Source excerpt", Content = context, Margin = new Thickness(0,8,0,8) });
        panel.Children.Add(new TextBlock { Text = "Corrected note (include the date and the missing context):", Margin = new Thickness(0,8,0,0) });
        panel.Children.Add(answer);
        var buttons = new StackPanel { Orientation = Orientation.Horizontal };
        buttons.Children.Add(save); buttons.Children.Add(dismiss); panel.Children.Add(buttons);
        panel.Children.Add(message);
        Content = new ScrollViewer { Content = panel, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
        items.SelectionChanged += (_, _) => Select();
        save.Click += (_, _) => Send("clarify");
        dismiss.Click += (_, _) => Send("clarify_dismiss");
        timer.Tick += (_, _) => Poll();
        IsVisibleChanged += (_, _) => { if (IsVisible) { Poll(); timer.Start(); } else timer.Stop(); };
    }

    void Select()
    {
        var item = items.SelectedItem as ClarificationItem;
        question.Text = item is null ? "" : string.Join("\n", item.Questions);
        location.Text = item?.Path ?? "";
        context.Text = item?.Context ?? "";
        answer.Text = item?.Text ?? "";
        save.IsEnabled = dismiss.IsEnabled = item is not null && pending is null && live;
    }

    void Send(string action)
    {
        if (items.SelectedItem is not ClarificationItem item || pending is not null) return;
        if (action == "clarify" && (string.IsNullOrWhiteSpace(answer.Text) || answer.Text.Trim() == item.Text))
        { message.Text = "Add the missing context before saving."; return; }
        try
        {
            pending = client.Send(action, item.Id, null, answer.Text);
            items.IsEnabled = save.IsEnabled = dismiss.IsEnabled = answer.IsEnabled = false;
            message.Text = "Saving…";
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        { message.Text = "Could not send the change. Check your review permissions."; }
    }

    void Poll()
    {
        try
        {
            var catalog = client.LoadClarifications();
            live = DateTimeOffset.UtcNow.ToUnixTimeSeconds() - catalog.Heartbeat <= 15;
            if (pending is not null && client.TryReadResponse(pending) is { } response)
            {
                pending = null;
                items.IsEnabled = answer.IsEnabled = true;
                message.Text = response.Ok ? "Saved. Your Obsidian note is up to date." : response.Error;
            }
            string data = JsonSerializer.Serialize(catalog.Items);
            if (pending is null && data != last)
            {
                string? selected = (items.SelectedItem as ClarificationItem)?.Id;
                string draft = answer.Text;
                last = data;
                items.ItemsSource = catalog.Items;
                items.SelectedItem = catalog.Items.FirstOrDefault(x => x.Id == selected) ?? catalog.Items.FirstOrDefault();
                if (selected is not null && (items.SelectedItem as ClarificationItem)?.Id == selected) answer.Text = draft;
                if (catalog.Items.Length == 0) message.Text = "No notes need clarification.";
            }
            save.IsEnabled = dismiss.IsEnabled = live && pending is null && items.SelectedItem is not null;
            if (!live) message.Text = pending is null ? "Service is offline. Your draft is preserved." : "Change queued; waiting for the service.";
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or JsonException)
        {
            save.IsEnabled = dismiss.IsEnabled = false;
            message.Text = "Clarification review is unavailable. The service may need an update or restart.";
        }
    }
}

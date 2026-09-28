using System.Media;
using System.Text.Json;

namespace SecondBrain.Tray;

sealed record Person(string id, string name);
sealed record Clip(double start, double end, string text, string quality, string file)
{
    public override string ToString() => $"{TimeSpan.FromSeconds(start):mm\\:ss}–{TimeSpan.FromSeconds(end):mm\\:ss}  ·  {text}";
}
sealed record Speaker(string id, string job, string display, string recorded, string? person, string? name,
    string state, string embedding_status, Clip[] clips);
sealed record Catalog(double heartbeat, Person[] people, Speaker[] speakers);

sealed class SpeakerReview : Form
{
    readonly string directory;
    readonly ListView speakers = new() { Dock = DockStyle.Fill, View = View.Details, FullRowSelect = true, MultiSelect = false, HideSelection = false };
    readonly ListBox clips = new() { Dock = DockStyle.Fill, HorizontalScrollbar = true };
    readonly ComboBox name = new() { Width = 290, DropDownStyle = ComboBoxStyle.DropDown, AutoCompleteMode = AutoCompleteMode.SuggestAppend, AutoCompleteSource = AutoCompleteSource.ListItems };
    readonly Label state = new() { AutoSize = true, MaximumSize = new(920, 0) };
    readonly Label detail = new() { AutoSize = true, MaximumSize = new(920, 0) };
    readonly TextBox search = new() { Width = 270, PlaceholderText = "Filter by name or recording date" };
    readonly CheckBox unknown = new() { Text = "Unidentified only", AutoSize = true };
    readonly System.Windows.Forms.Timer timer = new() { Interval = 2000 };
    Catalog? catalog;
    string lastData = "";
    string? pending;
    SoundPlayer? player;
    MemoryStream? playback;
    bool filling;

    public SpeakerReview(string path)
    {
        directory = path;
        Text = "Second Brain · Speakers";
        Size = new(1020, 730); MinimumSize = new(800, 600);
        Font = new Font("Segoe UI", 10); StartPosition = FormStartPosition.CenterScreen;
        var layout = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(18), ColumnCount = 1, RowCount = 7 };
        layout.RowStyles.Add(new(SizeType.AutoSize)); layout.RowStyles.Add(new(SizeType.AutoSize));
        layout.RowStyles.Add(new(SizeType.Percent, 55)); layout.RowStyles.Add(new(SizeType.AutoSize));
        layout.RowStyles.Add(new(SizeType.Percent, 45)); layout.RowStyles.Add(new(SizeType.AutoSize)); layout.RowStyles.Add(new(SizeType.AutoSize));
        layout.Controls.Add(new Label { AutoSize = true, Text = "Listen to a few clips, then assign a name. Confirmed voices are remembered for future recordings.", Padding = new(0, 0, 0, 12) });
        var filters = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Fill };
        filters.Controls.Add(search); filters.Controls.Add(unknown); layout.Controls.Add(filters);
        speakers.Columns.Add("Speaker / person", 230); speakers.Columns.Add("Recorded", 240); speakers.Columns.Add("Identity", 120); speakers.Columns.Add("Voice matching", 250);
        layout.Controls.Add(speakers); layout.Controls.Add(detail); layout.Controls.Add(clips);
        var actions = new FlowLayoutPanel { AutoSize = true, Dock = DockStyle.Fill, WrapContents = true };
        Button Button(string text, EventHandler action) { var b = new Button { Text = text, AutoSize = true }; b.Click += action; return b; }
        actions.Controls.Add(Button("▶ Play clip", (_, _) => Play()));
        actions.Controls.Add(Button("■ Stop", (_, _) => Stop()));
        actions.Controls.Add(name);
        actions.Controls.Add(Button("Assign name", (_, _) => Assign()));
        actions.Controls.Add(Button("Clear assignment", (_, _) => Send("clear")));
        actions.Controls.Add(Button("Rename person", (_, _) => Send("rename")));
        actions.Controls.Add(Button("Forget person", (_, _) => {
            if (Selected()?.person is not null && MessageBox.Show(this, "Forget this person's voice references and clear all their assignments?", "Forget person", MessageBoxButtons.YesNo) == DialogResult.Yes) Send("forget");
        }));
        layout.Controls.Add(actions); layout.Controls.Add(state); Controls.Add(layout);
        speakers.SelectedIndexChanged += (_, _) => { if (!filling) Selection(); };
        clips.DoubleClick += (_, _) => Play();
        search.TextChanged += (_, _) => Fill(); unknown.CheckedChanged += (_, _) => Fill();
        timer.Tick += (_, _) => RefreshCatalog();
        Shown += (_, _) => { RefreshCatalog(); timer.Start(); };
        FormClosed += (_, _) => { timer.Dispose(); Stop(); };
    }

    Speaker? Selected() => speakers.SelectedItems.Count == 0 ? null : speakers.SelectedItems[0].Tag as Speaker;
    static string Read(string path) { using var f = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete); using var r = new StreamReader(f); return r.ReadToEnd(); }

    void RefreshCatalog()
    {
        try {
            catalog = JsonSerializer.Deserialize<Catalog>(Read(Path.Combine(directory, "catalog.json"))) ?? throw new InvalidDataException();
            bool live = DateTimeOffset.UtcNow.ToUnixTimeSeconds() - catalog.heartbeat < 15;
            if (pending is not null) {
                string response = Path.Combine(directory, "responses", pending + ".json");
                if (File.Exists(response)) {
                    using var doc = JsonDocument.Parse(Read(response));
                    state.Text = doc.RootElement.GetProperty("ok").GetBoolean() ? "Saved. Voice references and speaker names updated. Existing Obsidian notes are preserved." : doc.RootElement.GetProperty("error").GetString();
                    pending = null;
                } else state.Text = live ? "Applying speaker change…" : "Change queued. It will apply when the service is available.";
            } else if (!live) state.Text = "Service is offline. Showing the last speaker list; changes will be queued.";
            else if (state.Text.Length == 0) state.Text = "Names apply here and to future notes. Voice matches are estimates; you can correct any assignment.";
            string data = JsonSerializer.Serialize(new { catalog.people, catalog.speakers });
            if (data != lastData) { lastData = data; Fill(); }
        } catch (Exception e) when (e is IOException or UnauthorizedAccessException or JsonException) {
            state.Text = "Speaker review is unavailable. Check the service and the ReviewUser configured by the installer.";
        }
    }

    void Fill()
    {
        if (catalog is null) return;
        string? selected = Selected()?.id;
        string typed = name.Text;
        filling = true; speakers.BeginUpdate(); speakers.Items.Clear();
        foreach (var speaker in catalog.speakers) {
            if (unknown.Checked && speaker.person is not null) continue;
            string title = speaker.name ?? speaker.display;
            if (!(title + " " + speaker.recorded).Contains(search.Text, StringComparison.CurrentCultureIgnoreCase)) continue;
            var row = new ListViewItem(new[] { title, speaker.recorded, speaker.state, speaker.embedding_status }) { Tag = speaker };
            speakers.Items.Add(row); if (speaker.id == selected) row.Selected = true;
        }
        name.Items.Clear(); name.Items.AddRange(catalog.people.Select(p => (object)p.name).ToArray());
        speakers.EndUpdate(); filling = false;
        if (Selected() is null && speakers.Items.Count > 0) speakers.Items[0].Selected = true;
        Selection();
        if (selected == Selected()?.id && typed.Length > 0) name.Text = typed;
    }

    void Selection()
    {
        Stop(); clips.Items.Clear();
        var s = Selected(); name.Text = s?.name ?? "";
        detail.Text = s is null ? "No speakers to review yet. New recordings appear after transcription." : $"{s.name ?? s.display} · {s.state} · {s.clips.Length} clips. Approximate/mixed clips are for listening only.";
        if (s is not null) { clips.Items.AddRange(s.clips.Cast<object>().ToArray()); if (clips.Items.Count > 0) clips.SelectedIndex = 0; }
    }

    void Play()
    {
        if (clips.SelectedItem is not Clip clip) return;
        try {
            if (Path.GetFileName(clip.file) != clip.file || !clip.file.EndsWith(".wav", StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException();
            Stop(); playback = new MemoryStream(File.ReadAllBytes(Path.Combine(directory, "clips", clip.file)));
            player = new SoundPlayer(playback); player.Load(); player.Play();
            detail.Text = $"Playing {clip.end - clip.start:F1}s · {clip.quality} · {clip.text}";
        } catch (Exception e) when (e is IOException or UnauthorizedAccessException or InvalidOperationException) { state.Text = "This clip could not be played. Refresh and try another clip."; }
    }
    void Stop() { player?.Stop(); player?.Dispose(); playback?.Dispose(); player = null; playback = null; }
    void Assign() => Send("assign");
    void Send(string action)
    {
        var s = Selected(); if (s is null || pending is not null) return;
        if ((action == "assign" || action == "rename") && string.IsNullOrWhiteSpace(name.Text)) { state.Text = "Type a name or choose an existing person."; return; }
        if ((action == "rename" || action == "forget") && s.person is null) { state.Text = "Select an already named speaker first."; return; }
        string? person = action == "assign" ? catalog?.people.FirstOrDefault(p => string.Equals(p.name, name.Text.Trim(), StringComparison.OrdinalIgnoreCase))?.id : s.person;
        string id = Guid.NewGuid().ToString();
        string target = Path.Combine(directory, "requests", id + ".json");
        try {
            string temporary = target + ".tmp";
            using (var file = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write)) {
                JsonSerializer.Serialize(file, new { id, action, observation = s.id, person, name = name.Text.Trim() }); file.Flush(true);
            }
            File.Move(temporary, target); pending = id; state.Text = "Speaker change queued…";
        } catch (Exception e) when (e is IOException or UnauthorizedAccessException) { state.Text = "Cannot save the change. Check your speaker-review permissions."; }
    }
}

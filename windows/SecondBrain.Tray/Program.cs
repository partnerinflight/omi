using System.Diagnostics;
using SecondBrain.Status;
namespace SecondBrain.Tray;
static class Program
{
    [STAThread] static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        using var mutex = new Mutex(true, "Local\\SecondBrain.Tray", out bool created);
        if (!created) return;
        string path = args.Length > 0 ? Path.GetFullPath(args[0]) : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "SecondBrain", "status", "status.json");
        Application.Run(new TrayContext(path));
    }
}
sealed class StatusCard : Form
{
    protected override bool ShowWithoutActivation => true;
    public StatusCard() { ShowInTaskbar = false; FormBorderStyle = FormBorderStyle.FixedToolWindow; TopMost = true; Text = "Second Brain status"; BackColor = Color.FromArgb(25, 27, 30); ForeColor = Color.WhiteSmoke; Size = new(490, 390); }
}
sealed class TrayContext : ApplicationContext
{
    readonly NotifyIcon icon;
    readonly System.Windows.Forms.Timer timer = new() { Interval = 1000 };
    readonly StatusCard card = new();
    readonly RichTextBox details = new() { Dock = DockStyle.Fill, ReadOnly = true, BorderStyle = BorderStyle.None, Font = new Font("Segoe UI", 10), BackColor = Color.FromArgb(25, 27, 30), ForeColor = Color.WhiteSmoke };
    readonly string path;
    DateTime lastHover;
    bool pinned;
    public TrayContext(string statusPath)
    {
        path = statusPath;
        card.Padding = new Padding(16); card.Controls.Add(details);
        card.FormClosing += (_, e) => { if (e.CloseReason == CloseReason.UserClosing) { e.Cancel = true; pinned = false; card.Hide(); } };
        icon = new NotifyIcon { Icon = SystemIcons.Information, Visible = true, Text = "Second Brain: loading status" };
        var menu = new ContextMenuStrip();
        menu.Items.Add("Show status", null, (_, _) => { pinned = true; ShowCard(); });
        menu.Items.Add("Windows Services", null, (_, _) => Process.Start(new ProcessStartInfo("services.msc") { UseShellExecute = true }));
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("Quit status app (service continues)", null, (_, _) => ExitThread());
        icon.ContextMenuStrip = menu;
        icon.MouseMove += (_, _) => { lastHover = DateTime.UtcNow; ShowCard(); };
        icon.DoubleClick += (_, _) => { pinned = true; ShowCard(); };
        timer.Tick += (_, _) => {
            Refresh();
            if (!pinned && card.Visible && DateTime.UtcNow - lastHover > TimeSpan.FromSeconds(5) && !card.Bounds.Contains(Cursor.Position)) card.Hide();
        };
        timer.Start(); Refresh();
    }
    void Refresh()
    {
        var status = Snapshot.Read(path, DateTimeOffset.UtcNow);
        icon.Text = status.Tooltip;
        icon.Icon = status.Healthy ? SystemIcons.Information : SystemIcons.Warning;
        details.Text = status.Details;
    }
    void ShowCard()
    {
        if (card.Visible) return;
        var area = Screen.FromPoint(Cursor.Position).WorkingArea;
        card.Location = new(Math.Clamp(Cursor.Position.X - card.Width, area.Left, Math.Max(area.Left, area.Right - card.Width)), Math.Clamp(Cursor.Position.Y - card.Height - 12, area.Top, Math.Max(area.Top, area.Bottom - card.Height)));
        card.Show();
    }
    protected override void ExitThreadCore() { timer.Stop(); timer.Dispose(); icon.Visible = false; icon.Dispose(); card.Dispose(); base.ExitThreadCore(); }
}

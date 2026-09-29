using System.Diagnostics;
using SecondBrain.Status;
using Drawing = System.Drawing;
using Forms = System.Windows.Forms;

namespace SecondBrain.Tray;

sealed class TrayIcon : IDisposable
{
    // Brain icon with a health dot: none when running, amber for attention, red when stopped.
    static readonly Drawing.Icon Running = Load("brain.ico"), Attention = Load("brain-attention.ico"), Stopped = Load("brain-stopped.ico");
    readonly Forms.NotifyIcon icon = new() { Icon = Running, Visible = true, Text = "Second Brain: loading status" };

    static Drawing.Icon Load(string name)
    {
        using var stream = System.Windows.Application.GetResourceStream(new Uri($"pack://application:,,,/Assets/{name}")).Stream;
        return new Drawing.Icon(stream, Forms.SystemInformation.SmallIconSize);
    }

    public event Action? OpenRequested;
    public event Action? SpeakersRequested;
    public event Action? QuitRequested;
    public event Action? Hovered;

    public TrayIcon()
    {
        var menu = new Forms.ContextMenuStrip();
        menu.Items.Add("Open Second Brain", null, (_, _) => OpenRequested?.Invoke());
        menu.Items.Add("Review speakers", null, (_, _) => SpeakersRequested?.Invoke());
        menu.Items.Add("Windows Services", null, (_, _) =>
        {
            // WinForms menu events bypass WPF's exception handler; mmc can be blocked by policy.
            try { Process.Start(new ProcessStartInfo("services.msc") { UseShellExecute = true }); }
            catch (System.ComponentModel.Win32Exception) { Forms.MessageBox.Show("Windows Services could not be opened on this PC.", "Second Brain"); }
        });
        menu.Items.Add(new Forms.ToolStripSeparator());
        menu.Items.Add("Quit status app (service continues)", null, (_, _) => QuitRequested?.Invoke());
        icon.ContextMenuStrip = menu;
        icon.MouseMove += (_, _) => Hovered?.Invoke();
        icon.MouseClick += (_, e) => { if (e.Button == Forms.MouseButtons.Left) OpenRequested?.Invoke(); };
    }

    public void Update(ServiceStatus status)
    {
        icon.Text = status.Tooltip;
        var wanted = status.Health switch { Health.Running => Running, Health.Attention => Attention, _ => Stopped };
        if (!ReferenceEquals(icon.Icon, wanted)) icon.Icon = wanted;
    }

    public void Dispose()
    {
        icon.Visible = false;
        icon.Dispose();
    }
}

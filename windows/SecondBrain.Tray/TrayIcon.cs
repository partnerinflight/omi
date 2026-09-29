using System.Diagnostics;
using SecondBrain.Status;
using Drawing = System.Drawing;
using Forms = System.Windows.Forms;

namespace SecondBrain.Tray;

sealed class TrayIcon : IDisposable
{
    readonly Forms.NotifyIcon icon = new() { Icon = Drawing.SystemIcons.Information, Visible = true, Text = "Second Brain: loading status" };

    public event Action? OpenRequested;
    public event Action? SpeakersRequested;
    public event Action? QuitRequested;
    public event Action? Hovered;

    public TrayIcon()
    {
        var menu = new Forms.ContextMenuStrip();
        menu.Items.Add("Open Second Brain", null, (_, _) => OpenRequested?.Invoke());
        menu.Items.Add("Review speakers", null, (_, _) => SpeakersRequested?.Invoke());
        menu.Items.Add("Windows Services", null, (_, _) => Process.Start(new ProcessStartInfo("services.msc") { UseShellExecute = true }));
        menu.Items.Add(new Forms.ToolStripSeparator());
        menu.Items.Add("Quit status app (service continues)", null, (_, _) => QuitRequested?.Invoke());
        icon.ContextMenuStrip = menu;
        icon.MouseMove += (_, _) => Hovered?.Invoke();
        icon.MouseClick += (_, e) => { if (e.Button == Forms.MouseButtons.Left) OpenRequested?.Invoke(); };
    }

    public void Update(ServiceStatus status)
    {
        icon.Text = status.Tooltip;
        icon.Icon = status.Health == Health.Running ? Drawing.SystemIcons.Information : Drawing.SystemIcons.Warning;
    }

    public void Dispose()
    {
        icon.Visible = false;
        icon.Dispose();
    }
}

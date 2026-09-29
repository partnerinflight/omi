using System.Windows;
using System.Windows.Threading;
using SecondBrain.Status;

namespace SecondBrain.Tray;

public partial class App : Application
{
    readonly DispatcherTimer timer = new() { Interval = TimeSpan.FromSeconds(1) };
    Mutex? mutex;
    TrayIcon? tray;
    HoverCard? card;
    MainWindow? window;
    ReviewSession? session;
    string statusPath = "";
    string reviewDirectory = "";
    DateTime lastHover;

    static readonly string ErrorLogPath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "SecondBrain", "tray-errors.log");

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        // Last-chance net: the tray runs unattended all day, so an unexpected error in a
        // handler or timer tick is logged and survived instead of silently closing the app.
        DispatcherUnhandledException += (_, args) =>
        {
            ErrorLog.Append(ErrorLogPath, args.Exception, DateTimeOffset.Now);
            args.Handled = true;
        };
        mutex = new Mutex(true, "Local\\SecondBrain.Tray", out bool created);
        if (!created) { Shutdown(); return; }

        string? path = e.Args.FirstOrDefault(a => !a.StartsWith("--", StringComparison.Ordinal));
        statusPath = path is not null
            ? Path.GetFullPath(path)
            : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "SecondBrain", "status", "status.json");
        reviewDirectory = Path.Combine(Path.GetDirectoryName(Path.GetDirectoryName(statusPath))!, "review");
        session = new ReviewSession(reviewDirectory);

        card = new HoverCard();
        tray = new TrayIcon();
        tray.OpenRequested += () => ShowWindow(null);
        tray.SpeakersRequested += () => ShowWindow("Speakers");
        tray.Hovered += () => { lastHover = DateTime.UtcNow; card?.ShowNearCursor(); };
        tray.QuitRequested += Quit;
        timer.Tick += (_, _) => Tick();
        Tick();
        timer.Start();

        string? show = e.Args.FirstOrDefault(a => a == "--show" || a.StartsWith("--show=", StringComparison.Ordinal));
        if (show is not null) ShowWindow(show.Length > "--show=".Length ? show["--show=".Length..] : null);
    }

    void Tick()
    {
        if (tray is null || card is null) return;
        var status = ServiceStatus.Read(statusPath, DateTimeOffset.UtcNow);
        tray.Update(status);
        card.Update(status);
        window?.Update(status);
        if (card.IsVisible && DateTime.UtcNow - lastHover > TimeSpan.FromSeconds(5) && !card.IsMouseOver) card.Hide();
    }

    void ShowWindow(string? page)
    {
        window ??= new MainWindow(session!);
        window.Update(ServiceStatus.Read(statusPath, DateTimeOffset.UtcNow));
        if (page is not null) window.Navigate(page);
        window.Show();
        if (window.WindowState == WindowState.Minimized) window.WindowState = WindowState.Normal;
        window.Activate();
        card?.Hide();
    }

    void Quit()
    {
        timer.Stop();
        tray?.Dispose();
        tray = null;
        window?.CloseForReal();
        card?.Close();
        mutex?.ReleaseMutex();
        Shutdown();
    }

    protected override void OnExit(ExitEventArgs e)
    {
        tray?.Dispose();
        mutex?.Dispose();
        base.OnExit(e);
    }
}

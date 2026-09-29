using System.Text.Json;
using System.Windows.Threading;
using SecondBrain.Status;

namespace SecondBrain.Tray;

// Shared speaker-review state for the Speakers and People pages: polls the catalog and
// tracks the single in-flight request until the service acknowledges it.
sealed class ReviewSession
{
    const string Hint = "Names apply here and to future notes. Voice matches are estimates; you can correct any assignment.";
    readonly DispatcherTimer timer = new() { Interval = TimeSpan.FromSeconds(2) };
    string lastData = "";
    string? pendingId;
    string? pendingAction;
    string? pendingObservation;
    string? result;

    public ReviewSession(string directory)
    {
        Client = new ReviewClient(directory);
        timer.Tick += (_, _) => Poll();
    }

    public ReviewClient Client { get; }
    public SpeakerCatalog? Catalog { get; private set; }
    public string Message { get; private set; } = "";
    public bool Pending => pendingId is not null;

    public event Action? CatalogChanged;
    public event Action? MessageChanged;
    public event Action<string, string?>? Applied;

    public void Start()
    {
        if (timer.IsEnabled) return;
        Poll();
        timer.Start();
    }

    public void Stop() => timer.Stop();

    public void Poll()
    {
        try
        {
            var catalog = Client.LoadCatalog();
            bool live = catalog.IsLive(DateTimeOffset.UtcNow);
            if (pendingId is not null && Client.TryReadResponse(pendingId) is { } response)
            {
                var (action, observation) = (pendingAction!, pendingObservation);
                pendingId = null;
                result = response.Ok ? "Saved. Voice references and speaker names updated. Existing Obsidian notes are preserved." : response.Error;
                SetCatalog(catalog);
                SetMessage(result!);
                if (response.Ok) Applied?.Invoke(action, observation);
                return;
            }
            SetCatalog(catalog);
            SetMessage(pendingId is not null
                ? live ? "Applying speaker change…" : "Change queued. It will apply when the service is available."
                : !live ? "Service is offline. Showing the last speaker list; changes will be queued."
                : result ?? Hint);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or JsonException)
        {
            // Keep the last good catalog visible.
            SetMessage("Speaker review is unavailable. Check the service and the ReviewUser configured by the installer.");
        }
    }

    public bool Send(string action, string? observation, string? person, string name)
    {
        if (pendingId is not null) return false;
        try
        {
            pendingId = Client.Send(action, observation, person, name);
            (pendingAction, pendingObservation, result) = (action, observation, null);
            SetMessage("Speaker change queued…");
            return true;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            Report("Cannot save the change. Check your speaker-review permissions.");
            return false;
        }
    }

    public void Report(string message)
    {
        result = message;
        SetMessage(message);
    }

    void SetCatalog(SpeakerCatalog catalog)
    {
        Catalog = catalog;
        string data = JsonSerializer.Serialize(new { catalog.People, catalog.Speakers });
        if (data == lastData) return;
        lastData = data;
        CatalogChanged?.Invoke();
    }

    void SetMessage(string message)
    {
        Message = message;
        MessageChanged?.Invoke();
    }
}

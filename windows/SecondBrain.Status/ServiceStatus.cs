using System.Text.Json;

namespace SecondBrain.Status;

public enum Health { Running, Attention, Stopped, Unavailable }

public sealed record ReceiverInfo(bool Listening, int Port, int ActiveUploads, int SessionsOk, int SessionsFailed, string? Error);

public sealed record StatusEvent(DateTimeOffset Time, string Kind, string Detail);

public sealed record JobInfo(string Id, string State, string Stage, int Attempts, string? Error, DateTimeOffset Updated)
{
    public string ShortId => Id.Length > 8 ? Id[..8] : Id;
}

public sealed record ServiceStatus(
    Health Health, string Reason, string? Stage, TimeSpan? InStage,
    int Pending, int Processing, int Complete, int Failed,
    ReceiverInfo Receiver, string? DiscoveryError, int UnidentifiedSpeakers, int People,
    IReadOnlyList<StatusEvent> Events, IReadOnlyList<JobInfo> Jobs)
{
    public const string UnavailableReason = "No readable status from the service. Check Windows Services and the installation path.";
    public const string StoppedReason = "The service is stopped or its heartbeat is stale. Recording ingestion is not confirmed active.";
    static readonly ReceiverInfo NoReceiver = new(false, 0, 0, 0, 0, null);

    public string Activity => Stage ?? (Pending > 0 ? "Waiting / retrying" : "Idle");

    public string Title => Health switch
    {
        Health.Unavailable => "Service unavailable",
        Health.Stopped => "Service stopped",
        _ => Receiver.ActiveUploads > 0 ? $"Receiving · {Activity}" : Activity,
    };

    public string Tooltip
    {
        get
        {
            if (Health == Health.Unavailable) return "Second Brain: service unavailable";
            if (Health == Health.Stopped) return "Second Brain: stopped or unreachable";
            string last = Events.Count == 0 ? "No completed operations yet" : $"Last: {Events[0].Kind} at {Events[0].Time.ToLocalTime():HH:mm}";
            string tip = $"Second Brain: {Title}\nQueue {Pending} · Done {Complete} · Failed {Failed}\n{last}";
            // NotifyIcon tooltips are limited to 127 characters.
            return tip.Length > 127 ? tip[..126] + "…" : tip;
        }
    }

    public static ServiceStatus Read(string path, DateTimeOffset now)
    {
        try { return Parse(SharedFile.ReadAllText(path), now); }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or JsonException or InvalidOperationException
            or KeyNotFoundException or FormatException or ArgumentException)
        {
            return Empty(Health.Unavailable, UnavailableReason);
        }
    }

    public static ServiceStatus Parse(string json, DateTimeOffset now)
    {
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        double heartbeat = root.GetProperty("heartbeat").GetDouble();
        if (now.ToUnixTimeSeconds() - heartbeat > 15 || root.GetProperty("service").GetString() != "running")
            return Empty(Health.Stopped, StoppedReason);

        int Count(string key) => Section(root, "counts") is { } counts ? Int(counts, key) : 0;
        var receiver = Section(root, "receiver") is { } rx
            ? new ReceiverInfo(Bool(rx, "listening"), Int(rx, "port"), Int(rx, "active_uploads"), Int(rx, "sessions_ok"), Int(rx, "sessions_failed"), Text(rx, "error"))
            : NoReceiver;

        string? stage = null;
        TimeSpan? inStage = null;
        if (Section(root, "current") is { } current)
        {
            stage = Text(current, "stage") ?? "processing";
            if (current.TryGetProperty("updated", out var updated) && updated.ValueKind == JsonValueKind.Number)
            {
                var span = now - Time(updated);
                inStage = span < TimeSpan.Zero ? TimeSpan.Zero : span;
            }
        }

        var events = Items(root, "events")
            .Select(e => new StatusEvent(Time(e.GetProperty("time")), Text(e, "kind") ?? "", Text(e, "detail") ?? ""))
            .ToList();
        var jobs = Items(root, "recent")
            .Select(j => new JobInfo(Text(j, "id") ?? "", Text(j, "state") ?? "", Text(j, "stage") ?? "", Int(j, "attempts"), Text(j, "error"), Time(j.GetProperty("updated"))))
            .ToList();
        string? discovery = Text(root, "discovery_error");
        int unidentified = 0, people = 0;
        if (Section(root, "speakers") is { } speakers)
        {
            unidentified = Int(speakers, "unidentified");
            people = Int(speakers, "people");
        }

        int failed = Count("failed");
        var problems = new List<string>();
        if (failed > 0) problems.Add(failed == 1 ? "1 recording failed processing" : $"{failed} recordings failed processing");
        if (!receiver.Listening) problems.Add(string.IsNullOrEmpty(receiver.Error) ? "Receiver is not listening" : $"Receiver is not listening: {receiver.Error}");
        if (discovery is not null) problems.Add("Recording discovery needs attention; check the service log");

        return new(
            problems.Count == 0 ? Health.Running : Health.Attention,
            problems.Count == 0 ? "Receiving and processing normally" : string.Join(" · ", problems),
            stage, inStage, Count("pending"), Count("processing"), Count("complete"), failed,
            receiver, discovery, unidentified, people, events, jobs);
    }

    static ServiceStatus Empty(Health health, string reason) =>
        new(health, reason, null, null, 0, 0, 0, 0, NoReceiver, null, 0, 0, [], []);

    static JsonElement? Section(JsonElement e, string key) =>
        e.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.Object ? v : null;

    static IEnumerable<JsonElement> Items(JsonElement e, string key) =>
        e.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.Array ? v.EnumerateArray() : Enumerable.Empty<JsonElement>();

    static int Int(JsonElement e, string key) =>
        e.TryGetProperty(key, out var v) && v.ValueKind != JsonValueKind.Null ? v.GetInt32() : 0;

    static bool Bool(JsonElement e, string key) =>
        e.TryGetProperty(key, out var v) && v.ValueKind == JsonValueKind.True;

    static string? Text(JsonElement e, string key) =>
        e.TryGetProperty(key, out var v) && v.ValueKind != JsonValueKind.Null ? v.GetString() : null;

    static DateTimeOffset Time(JsonElement value) =>
        DateTimeOffset.FromUnixTimeMilliseconds((long)(value.GetDouble() * 1000));
}

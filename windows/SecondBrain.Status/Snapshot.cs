using System.Text.Json;
namespace SecondBrain.Status;
public record Snapshot(string Title, string Tooltip, string Details, bool Healthy)
{
    public static Snapshot Read(string path, DateTimeOffset now)
    {
        try { using var file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete); using var reader = new StreamReader(file); return Parse(reader.ReadToEnd(), now); }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or JsonException or InvalidOperationException or KeyNotFoundException or FormatException)
        { return new("Service unavailable", "Second Brain: service unavailable", "No readable status from the service. Check Windows Services and the installation path.", false); }
    }
    public static Snapshot Parse(string json, DateTimeOffset now)
    {
        using var doc = JsonDocument.Parse(json);
        var r = doc.RootElement;
        double heartbeat = r.GetProperty("heartbeat").GetDouble();
        if (now.ToUnixTimeSeconds() - heartbeat > 15 || r.GetProperty("service").GetString() != "running")
            return new("Service stopped", "Second Brain: stopped or unreachable", "The service is stopped or its heartbeat is stale. Recording ingestion is not confirmed active.", false);
        var counts = r.GetProperty("counts");
        int Count(string key) => counts.TryGetProperty(key, out var v) ? v.GetInt32() : 0;
        var receiver = r.GetProperty("receiver");
        int uploads = receiver.GetProperty("active_uploads").GetInt32();
        var current = r.GetProperty("current");
        string activity = current.ValueKind == JsonValueKind.Object ? current.GetProperty("stage").GetString() ?? "Processing" : Count("pending") > 0 ? "Waiting / retrying" : "Idle";
        bool healthy = Count("failed") == 0 && receiver.GetProperty("listening").GetBoolean() && r.GetProperty("discovery_error").ValueKind == JsonValueKind.Null;
        string title = uploads > 0 ? $"Receiving · {activity}" : activity;
        var events = r.GetProperty("events").EnumerateArray().ToArray();
        string last = events.Length == 0 ? "No completed operations yet" : $"Last: {events[0].GetProperty("kind").GetString()} at {DateTimeOffset.FromUnixTimeSeconds((long)events[0].GetProperty("time").GetDouble()).ToLocalTime():HH:mm}";
        string tip = $"Second Brain: {title}\nQueue {Count("pending")} · Done {Count("complete")} · Failed {Count("failed")}\n{last}";
        if (tip.Length > 127) tip = tip[..126] + "…";
        var lines = new List<string> { "SECOND BRAIN", title, "", $"Receiver: {(receiver.GetProperty("listening").GetBoolean() ? "listening" : "offline")} on port {receiver.GetProperty("port")}",
            $"Active uploads: {uploads}", $"Upload sessions: {receiver.GetProperty("sessions_ok")} succeeded / {receiver.GetProperty("sessions_failed")} failed",
            $"Queue: {Count("pending")} pending / {Count("processing")} processing / {Count("failed")} failed", $"Completed recordings: {Count("complete")}", "", "RECENT OPERATIONS" };
        foreach (var ev in events.Take(6)) {
            var at = DateTimeOffset.FromUnixTimeSeconds((long)ev.GetProperty("time").GetDouble()).ToLocalTime();
            lines.Add($"{at:HH:mm:ss} · {ev.GetProperty("kind").GetString()} · {ev.GetProperty("detail").GetString()}");
        }
        if (r.TryGetProperty("speakers", out var speakers)) {
            lines.Add("");
            lines.Add($"Speakers: {speakers.GetProperty("unidentified")} unidentified · {speakers.GetProperty("people")} saved people");
            lines.Add("Use Speakers — listen and name from the tray menu.");
        }
        if (r.GetProperty("discovery_error").ValueKind != JsonValueKind.Null) lines.Add("Recording discovery needs attention; check the service log.");
        return new(title, tip, string.Join(Environment.NewLine, lines), healthy);
    }
}

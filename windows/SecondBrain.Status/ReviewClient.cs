using System.Text.Json;

namespace SecondBrain.Status;

public sealed record ReviewResponse(bool Ok, string? Error);

// File protocol shared with src/second_brain/speakers.py: the review user may only drop requests;
// the service validates them and owns all authoritative state.
public sealed class ReviewClient(string directory)
{
    public string Directory { get; } = directory;

    public SpeakerCatalog LoadCatalog() => SpeakerCatalog.Parse(SharedFile.ReadAllText(Path.Combine(Directory, "catalog.json")));

    public string Send(string action, string? observation, string? person, string name)
    {
        string id = Guid.NewGuid().ToString();
        string target = Path.Combine(Directory, "requests", id + ".json");
        string temporary = target + ".tmp";
        try
        {
            using (var file = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write))
            {
                JsonSerializer.Serialize(file, new { id, action, observation, person, name = name.Trim() });
                file.Flush(true);
            }
            // The service only picks up *.json, so it never sees a half-written request.
            File.Move(temporary, target);
        }
        finally
        {
            try { File.Delete(temporary); } catch (IOException) { } catch (UnauthorizedAccessException) { }
        }
        return id;
    }

    public ReviewResponse? TryReadResponse(string id)
    {
        string path = Path.Combine(Directory, "responses", id + ".json");
        if (!File.Exists(path)) return null;
        using var doc = JsonDocument.Parse(SharedFile.ReadAllText(path));
        var root = doc.RootElement;
        bool ok = root.TryGetProperty("ok", out var value) && value.ValueKind == JsonValueKind.True;
        string? error = root.TryGetProperty("error", out var e) && e.ValueKind == JsonValueKind.String ? e.GetString() : null;
        return new ReviewResponse(ok, ok ? null : error ?? "The service rejected the change.");
    }

    public string ClipPath(Clip clip)
    {
        if (string.IsNullOrEmpty(clip.File) || Path.GetFileName(clip.File) != clip.File || !clip.File.EndsWith(".wav", StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Invalid clip file name");
        return Path.Combine(Directory, "clips", clip.File);
    }

    // Mirrors SpeakerStore.name() for immediate feedback; the service remains authoritative.
    public static string? ValidateName(string? name)
    {
        string trimmed = (name ?? "").Trim();
        if (trimmed.Length == 0) return "Type a name or choose an existing person.";
        if (trimmed.Length > 80 || trimmed.Any(c => c < ' ') || trimmed.IndexOfAny(['[', ']', '<', '>', '\\', '|']) >= 0)
            return "Use a name of 1–80 characters without control characters or markup brackets.";
        return null;
    }
}

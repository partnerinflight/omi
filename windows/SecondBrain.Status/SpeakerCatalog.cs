using System.Text.Json;

namespace SecondBrain.Status;

public sealed record Person(string Id, string Name);

public sealed record Clip(double Start, double End, string Text, string Quality, string File)
{
    public TimeSpan Length => TimeSpan.FromSeconds(Math.Max(0, End - Start));
    public string Range => $"{TimeSpan.FromSeconds(Start):mm\\:ss}–{TimeSpan.FromSeconds(End):mm\\:ss}";
}

public sealed record Speaker(string Id, string Job, string Display, string Recorded, string? Person, string? Name,
    string State, string EmbeddingStatus, double? Score, Clip[] Clips, int? Importance = null)
{
    public string Title => Name ?? Display;
    public bool Unidentified => State == "unidentified";
}

// Importance is the memory gate's score for the recording's published conversation (0–100).
public sealed record Recording(string Job, string Recorded, IReadOnlyList<Speaker> Speakers, int SpeakerCount, int UnidentifiedCount,
    int? Importance);

public sealed record PersonSummary(Person Person, int Confirmed, int Matched, int Recordings, IReadOnlyList<Speaker> Speakers);

public sealed record SpeakerCatalog(double Heartbeat, Person[] People, Speaker[] Speakers)
{
    static readonly JsonSerializerOptions Options = new() { PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };

    // Valid JSON of the wrong shape is reported as unreadable here, so views never see null lists.
    public static SpeakerCatalog Parse(string json)
    {
        var catalog = JsonSerializer.Deserialize<SpeakerCatalog>(json, Options) ?? throw new JsonException("Empty speaker catalog");
        if (catalog.People is null || catalog.Speakers is null || catalog.People.Any(p => p is null)
            || catalog.Speakers.Any(s => s is null || s.Clips is null || s.Clips.Any(c => c is null)))
            throw new JsonException("Speaker catalog is missing required lists");
        return catalog;
    }

    public bool IsLive(DateTimeOffset now) => now.ToUnixTimeSeconds() - Heartbeat < 15;

    public Speaker? Find(string id) => Speakers.FirstOrDefault(s => s.Id == id);

    // Recordings in catalog order (the service writes most important first, then newest); counts cover the whole recording.
    public IReadOnlyList<Recording> Recordings(bool unidentifiedOnly = false, string search = "")
    {
        var result = new List<Recording>();
        foreach (var group in Speakers.GroupBy(s => s.Job))
        {
            var rows = group.Where(s => (!unidentifiedOnly || s.Unidentified) && Matches(s, search)).ToList();
            if (rows.Count > 0)
                result.Add(new Recording(group.Key, group.First().Recorded, rows, group.Count(), group.Count(s => s.Unidentified),
                    group.Max(s => s.Importance)));
        }
        return result;
    }

    public IReadOnlyList<PersonSummary> PersonSummaries() => People.Select(person =>
    {
        var rows = Speakers.Where(s => s.Person == person.Id).ToList();
        return new PersonSummary(person, rows.Count(s => s.State == "confirmed"), rows.Count(s => s.State == "matched"),
            rows.Select(s => s.Job).Distinct().Count(), rows);
    }).ToList();

    // The next unidentified speaker after afterId in display order, wrapping; never afterId itself.
    public Speaker? NextUnidentified(string? afterId, string search = "")
    {
        var order = Recordings(false, search).SelectMany(r => r.Speakers).ToList();
        int start = afterId is null ? -1 : order.FindIndex(s => s.Id == afterId);
        for (int i = 1; i <= order.Count; i++)
        {
            var candidate = order[(start + i) % order.Count];
            if (candidate.Unidentified && candidate.Id != afterId) return candidate;
        }
        return null;
    }

    static bool Matches(Speaker speaker, string search) =>
        string.IsNullOrWhiteSpace(search) ||
        $"{speaker.Title} {speaker.Recorded}".Contains(search.Trim(), StringComparison.CurrentCultureIgnoreCase);
}

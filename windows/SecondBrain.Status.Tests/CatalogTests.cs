namespace SecondBrain.Status.Tests;

static class CatalogTests
{
    static string Ids(IEnumerable<Speaker> speakers) => string.Join(",", speakers.Select(s => s.Id));

    public static void Run()
    {
        var catalog = SpeakerCatalog.Parse(Fixtures.CatalogJson);

        var all = catalog.Recordings();
        Check.Equal("job-new,job-old", string.Join(",", all.Select(r => r.Job)), "recordings newest first");
        Check.Equal("o1,o2,o3", Ids(all[0].Speakers), "speakers keep catalog order within a recording");
        Check.Equal("2026-09-28 14:05", all[0].Recorded, "recording date");
        Check.Equal(3, all[0].SpeakerCount, "speaker count");
        Check.Equal(1, all[0].UnidentifiedCount, "unidentified count");

        var unknown = catalog.Recordings(unidentifiedOnly: true);
        Check.Equal("o2", Ids(unknown[0].Speakers), "unidentified filter, newest recording");
        Check.Equal("o4", Ids(unknown[1].Speakers), "unidentified filter, older recording");
        Check.Equal(3, unknown[0].SpeakerCount, "counts cover the whole recording, not the filter");

        var alice = catalog.Recordings(search: "alice");
        Check.Equal(1, alice.Count, "name search groups");
        Check.Equal("o1,o3", Ids(alice[0].Speakers), "name search is case-insensitive");
        Check.Equal("job-old", catalog.Recordings(search: " 09-27 ").Single().Job, "date search, trimmed");
        Check.Equal(0, catalog.Recordings(search: "nobody").Count, "no match");

        var people = catalog.PersonSummaries().ToDictionary(p => p.Person.Name);
        Check.Equal((1, 1, 1), (people["Alice"].Confirmed, people["Alice"].Matched, people["Alice"].Recordings), "Alice summary");
        Check.Equal((1, 0, 1), (people["Bob"].Confirmed, people["Bob"].Matched, people["Bob"].Recordings), "Bob summary");
        Check.Equal((0, 0, 0), (people["Carol"].Confirmed, people["Carol"].Matched, people["Carol"].Recordings), "person without rows is listed");
        Check.Equal("o1,o3", Ids(people["Alice"].Speakers), "person rows");

        Check.Equal("o2", catalog.NextUnidentified(null)?.Id, "first unidentified");
        Check.Equal("o2", catalog.NextUnidentified("o1")?.Id, "next after a named row");
        Check.Equal("o4", catalog.NextUnidentified("o2")?.Id, "next skips to the following recording");
        Check.Equal("o2", catalog.NextUnidentified("o4")?.Id, "wraps to the start");
        Check.Equal("o2", catalog.NextUnidentified("missing")?.Id, "unknown id starts from the top");
        Check.Equal("o4", catalog.NextUnidentified("o2", "09-27")?.Id, "respects the search");
        var lastOne = catalog with { Speakers = catalog.Speakers.Where(s => s.Id != "o4").ToArray() };
        Check.Equal<string?>(null, lastOne.NextUnidentified("o2")?.Id, "no other unidentified speaker");

        Check.Equal(0.91, catalog.Find("o3")?.Score, "score");
        Check.Equal("Alice", catalog.Find("o1")?.Title, "title prefers name");
        Check.Equal("Speaker 2", catalog.Find("o2")?.Title, "title falls back to display");
        Check.Equal("reference", catalog.Find("o1")?.EmbeddingStatus, "snake_case field");
        var clip = catalog.Find("o1")!.Clips[0];
        Check.Equal("00:01–00:07", clip.Range, "clip range");
        Check.Equal(TimeSpan.FromSeconds(6.5), clip.Length, "clip length");

        Check.That(catalog.IsLive(DateTimeOffset.FromUnixTimeSeconds(1010)), "fresh catalog is live");
        Check.That(!catalog.IsLive(DateTimeOffset.FromUnixTimeSeconds(1016)), "stale catalog is not live");
        Check.Throws<System.Text.Json.JsonException>(() => SpeakerCatalog.Parse(Fixtures.CatalogJson[..200]), "truncated catalog");
        // Valid JSON with missing lists must be rejected as unreadable, not crash the UI later.
        Check.Throws<System.Text.Json.JsonException>(() => SpeakerCatalog.Parse("""{"heartbeat": 1, "people": null, "speakers": []}"""), "null people");
        Check.Throws<System.Text.Json.JsonException>(() => SpeakerCatalog.Parse("""{"heartbeat": 1, "people": []}"""), "missing speakers");
        Check.Throws<System.Text.Json.JsonException>(() => SpeakerCatalog.Parse(
            """{"heartbeat": 1, "people": [], "speakers": [{"id": "o1", "job": "j", "display": "Speaker 1", "recorded": "r", "state": "unidentified", "embedding_status": "x", "clips": null}]}"""),
            "null clips");
        Check.Throws<System.Text.Json.JsonException>(() => SpeakerCatalog.Parse("[]"), "array root");
    }
}

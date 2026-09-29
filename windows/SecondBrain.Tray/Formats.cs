using SecondBrain.Status;

namespace SecondBrain.Tray;

static class Formats
{
    public static string Plural(int count, string word) => $"{count} {word}{(count == 1 ? "" : "s")}";

    public static string Duration(TimeSpan t) =>
        t.TotalMinutes < 1 ? $"{(int)t.TotalSeconds}s"
        : t.TotalHours < 1 ? $"{(int)t.TotalMinutes} min"
        : $"{(int)t.TotalHours} h {t.Minutes} min";

    public static string Time(DateTimeOffset time, DateTimeOffset now)
    {
        var local = time.ToLocalTime();
        return local.Date == now.ToLocalTime().Date ? local.ToString("HH:mm:ss") : local.ToString("MMM d, HH:mm");
    }

    // Match scores are cosine similarities, not probabilities (docs/speakers.md).
    public static string State(Speaker speaker) => speaker.State switch
    {
        "confirmed" => "Confirmed",
        "matched" => speaker.Score is { } score ? $"Voice match ({score:0.00})" : "Voice match",
        _ => "Unidentified",
    };
}

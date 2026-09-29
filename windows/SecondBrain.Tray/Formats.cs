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
}

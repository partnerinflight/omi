namespace SecondBrain.Status;

// Best-effort, size-capped error log for the per-user tray. Logging must never throw:
// it runs inside the last-chance exception handler.
public static class ErrorLog
{
    public static void Append(string path, Exception error, DateTimeOffset now, long maxBytes = 1_000_000)
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            var existing = new FileInfo(path);
            if (existing.Exists && existing.Length > maxBytes) existing.Delete();
            File.AppendAllText(path, $"{now.ToLocalTime():yyyy-MM-dd HH:mm:ss zzz} {error}{Environment.NewLine}{Environment.NewLine}");
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or NotSupportedException or ArgumentException) { }
    }
}

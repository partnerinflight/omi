namespace SecondBrain.Status;

static class SharedFile
{
    // The service rewrites these files while the tray reads them; never lock them against it.
    public static string ReadAllText(string path)
    {
        using var file = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
        using var reader = new StreamReader(file);
        return reader.ReadToEnd();
    }
}

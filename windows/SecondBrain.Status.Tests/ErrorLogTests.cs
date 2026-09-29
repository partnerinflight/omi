namespace SecondBrain.Status.Tests;

static class ErrorLogTests
{
    public static void Run()
    {
        string dir = Path.Combine(Path.GetTempPath(), "sb-errorlog-" + Guid.NewGuid().ToString("N"));
        string path = Path.Combine(dir, "tray-errors.log");
        var now = DateTimeOffset.FromUnixTimeSeconds(1_790_635_757);
        try
        {
            ErrorLog.Append(path, new InvalidOperationException("first failure"), now);
            ErrorLog.Append(path, new ArgumentNullException("speakers"), now.AddMinutes(1));
            string text = File.ReadAllText(path);
            Check.That(text.Contains("InvalidOperationException") && text.Contains("first failure"), "first entry written, creating the directory");
            Check.That(text.Contains("ArgumentNullException") && text.IndexOf("ArgumentNullException") > text.IndexOf("first failure"), "entries appended in order");

            // The file is bounded by the cap plus one entry: the write after it exceeds the cap starts over.
            ErrorLog.Append(path, new Exception(new string('x', 5000)), now, maxBytes: 2000);
            ErrorLog.Append(path, new TimeoutException("after the cap"), now, maxBytes: 2000);
            string capped = File.ReadAllText(path);
            Check.That(!capped.Contains("first failure") && !capped.Contains("xxxx") && capped.Contains("after the cap"), "log restarts once it exceeds the cap");

            // Logging must never become a second crash: a path that cannot be written is ignored.
            ErrorLog.Append(Path.Combine(path, "not-a-directory", "x.log"), new Exception("ignored"), now);
        }
        finally { if (Directory.Exists(dir)) Directory.Delete(dir, recursive: true); }
    }
}

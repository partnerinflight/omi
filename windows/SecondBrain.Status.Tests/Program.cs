using System.Text.Json;
using SecondBrain.Status;
var now = DateTimeOffset.UtcNow;
var snapshot = new { heartbeat = now.ToUnixTimeSeconds(), service = "running", counts = new { pending = 2, complete = 4, failed = 0 },
    current = new { stage = "transcribing" }, receiver = new { active_uploads = 1, listening = true, port = 7331, sessions_ok = 2, sessions_failed = 0 },
    events = new[] { new { time = now.ToUnixTimeSeconds(), kind = "complete", detail = "1 note published" } }, discovery_error = (string?)null,
    speakers = new { unidentified = 3, people = 2 } };
var json = JsonSerializer.Serialize(snapshot);
var live = Snapshot.Parse(json, now);
if (!live.Healthy || !live.Title.Contains("Receiving") || !live.Details.Contains("transcribing") || !live.Details.Contains("1 note published") || live.Tooltip.Length > 127) throw new Exception("Live status regression");
if (Snapshot.Parse(json, now.AddSeconds(20)).Healthy) throw new Exception("Stale status claims healthy");
if (Snapshot.Read("nonexistent-status-file", now).Healthy) throw new Exception("Missing service claims healthy");
if (!live.Details.Contains("3 unidentified") || !live.Details.Contains("2 saved people")) throw new Exception("Speaker review summary missing");
Console.WriteLine("Status rendering tests passed");

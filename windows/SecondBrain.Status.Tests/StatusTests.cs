using System.Text.Json.Nodes;

namespace SecondBrain.Status.Tests;

static class StatusTests
{
    static readonly DateTimeOffset Now = DateTimeOffset.FromUnixTimeSeconds(1_790_635_757);

    static JsonObject Live() => new()
    {
        ["version"] = 1,
        ["service"] = "running",
        ["heartbeat"] = Now.ToUnixTimeSeconds(),
        ["counts"] = new JsonObject { ["pending"] = 2, ["processing"] = 1, ["complete"] = 4, ["failed"] = 0 },
        ["current"] = new JsonObject { ["id"] = "14fe9d78d90b359d", ["stage"] = "transcribing", ["attempts"] = 1, ["updated"] = Now.ToUnixTimeSeconds() - 30 },
        ["recent"] = new JsonArray(new JsonObject
        {
            ["id"] = "14fe9d78d90b359d530a1dc5", ["state"] = "processing", ["stage"] = "transcribing",
            ["attempts"] = 1, ["error"] = null, ["updated"] = Now.ToUnixTimeSeconds(),
        }),
        ["events"] = new JsonArray(new JsonObject
        {
            ["time"] = Now.ToUnixTimeSeconds(), ["kind"] = "complete", ["job"] = "abc", ["detail"] = "1 note published",
        }),
        ["receiver"] = new JsonObject
        {
            ["listening"] = true, ["port"] = 7331, ["active_uploads"] = 1, ["sessions_ok"] = 2, ["sessions_failed"] = 0, ["error"] = null,
        },
        ["discovery_error"] = null,
        ["speakers"] = new JsonObject { ["unidentified"] = 3, ["people"] = 2 },
    };

    static ServiceStatus Parse(JsonObject json) => ServiceStatus.Parse(json.ToJsonString(), Now);

    static ServiceStatus ReadText(string text)
    {
        string path = Path.GetTempFileName();
        try
        {
            File.WriteAllText(path, text);
            return ServiceStatus.Read(path, Now);
        }
        finally { File.Delete(path); }
    }

    public static void Run()
    {
        var live = Parse(Live());
        Check.Equal(Health.Running, live.Health, "live health");
        Check.Equal("Receiving and processing normally", live.Reason, "live reason");
        Check.Equal("Receiving · transcribing", live.Title, "live title");
        Check.Equal("transcribing", live.Stage, "stage");
        Check.Equal<TimeSpan?>(TimeSpan.FromSeconds(30), live.InStage, "time in stage");
        Check.Equal(2, live.Pending, "pending");
        Check.Equal(1, live.Processing, "processing");
        Check.Equal(4, live.Complete, "complete");
        Check.Equal(7331, live.Receiver.Port, "receiver port");
        Check.Equal(2, live.Receiver.SessionsOk, "sessions ok");
        Check.Equal(3, live.UnidentifiedSpeakers, "unidentified speakers");
        Check.Equal(2, live.People, "people");
        Check.Equal("1 note published", live.Events[0].Detail, "event detail");
        Check.Equal("14fe9d78", live.Jobs[0].ShortId, "short job id");
        Check.That(live.Tooltip.Length <= 127 && live.Tooltip.Contains("Queue 2 · Done 4 · Failed 0"), "tooltip");

        Check.Equal(Health.Running, ServiceStatus.Parse(Live().ToJsonString(), Now.AddSeconds(15)).Health, "heartbeat exactly 15 s old is still fresh");
        var stale = ServiceStatus.Parse(Live().ToJsonString(), Now.AddSeconds(20));
        Check.Equal(Health.Stopped, stale.Health, "stale heartbeat");
        Check.Equal("Service stopped", stale.Title, "stale title");
        Check.Equal(ServiceStatus.StoppedReason, stale.Reason, "stale reason");

        var stoppedJson = Live();
        stoppedJson["service"] = "stopped";
        Check.Equal(Health.Stopped, Parse(stoppedJson).Health, "service reported stopped");

        var missing = ServiceStatus.Read("nonexistent-status-file", Now);
        Check.Equal(Health.Unavailable, missing.Health, "missing file");
        Check.Equal(ServiceStatus.UnavailableReason, missing.Reason, "missing file reason");
        Check.Equal("Second Brain: service unavailable", missing.Tooltip, "missing file tooltip");

        var failedJson = Live();
        failedJson["counts"]!["failed"] = 2;
        var failed = Parse(failedJson);
        Check.Equal(Health.Attention, failed.Health, "failed jobs need attention");
        Check.That(failed.Reason.Contains("2 recordings failed processing"), "failed reason");
        Check.That(failed.Tooltip.Contains("Failed 2"), "failed tooltip");

        var deafJson = Live();
        deafJson["receiver"]!["listening"] = false;
        deafJson["receiver"]!["error"] = "port in use";
        Check.That(Parse(deafJson).Reason.Contains("Receiver is not listening: port in use"), "receiver offline reason");

        var discoveryJson = Live();
        discoveryJson["discovery_error"] = "scan failed";
        var discovery = Parse(discoveryJson);
        Check.Equal(Health.Attention, discovery.Health, "discovery error needs attention");
        Check.That(discovery.Reason.Contains("Recording discovery needs attention"), "discovery reason");

        var minimal = Parse(new JsonObject
        {
            ["service"] = "running", ["heartbeat"] = Now.ToUnixTimeSeconds(), ["receiver"] = new JsonObject { ["listening"] = true },
        });
        Check.Equal(Health.Running, minimal.Health, "minimal status is healthy");
        Check.Equal("Idle", minimal.Title, "minimal title");
        Check.Equal(0, minimal.Events.Count, "minimal events");
        Check.Equal(0, minimal.Jobs.Count, "minimal jobs");
        Check.Equal<string?>(null, minimal.Stage, "minimal stage");

        var waitingJson = Live();
        waitingJson.Remove("current");
        waitingJson["receiver"]!["active_uploads"] = 0;
        Check.Equal("Waiting / retrying", Parse(waitingJson).Title, "pending work without a current job");

        var startingJson = Live();
        startingJson["current"]!["stage"] = "starting";
        Check.Equal("Receiving · starting", Parse(startingJson).Title, "unknown stage shown verbatim");

        var longJson = Live();
        longJson["events"]![0]!["kind"] = new string('x', 300);
        var longTip = Parse(longJson).Tooltip;
        Check.That(longTip.Length <= 127 && longTip.EndsWith('…'), "long tooltip truncated");

        var wrongType = Live();
        wrongType["receiver"]!["port"] = "7331";
        Check.Equal(Health.Unavailable, ReadText(wrongType.ToJsonString()).Health, "string where a number belongs");
        var absurdTime = Live();
        absurdTime["events"]![0]!["time"] = 1e20;
        Check.Equal(Health.Unavailable, ReadText(absurdTime.ToJsonString()).Health, "absurd timestamp");
        Check.Equal(Health.Unavailable, ReadText("{\"service\": \"running\", \"heart").Health, "truncated file");
    }
}

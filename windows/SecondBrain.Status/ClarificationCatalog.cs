using System.Text.Json;

namespace SecondBrain.Status;

public sealed record ClarificationItem(string Id, string Path, string Text, string[] Questions, string Context);
public sealed record ClarificationCatalog(double Heartbeat, ClarificationItem[] Items)
{
    public static ClarificationCatalog Parse(string json) =>
        JsonSerializer.Deserialize<ClarificationCatalog>(json, new JsonSerializerOptions { PropertyNameCaseInsensitive = true })
        is { Items: not null } catalog ? catalog : throw new JsonException("Invalid clarification catalog");
}

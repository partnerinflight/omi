using System.Text.Json;

namespace SecondBrain.Status.Tests;

static class ReviewClientTests
{
    public static void Run()
    {
        string root = Path.Combine(Path.GetTempPath(), "sb-review-" + Guid.NewGuid().ToString("N"));
        try
        {
            foreach (var dir in new[] { "requests", "responses", "clips" }) Directory.CreateDirectory(Path.Combine(root, dir));
            File.WriteAllText(Path.Combine(root, "catalog.json"), Fixtures.CatalogJson);
            var client = new ReviewClient(root);

            Check.Equal(5, client.LoadCatalog().Speakers.Length, "catalog loads from the review directory");

            string id = client.Send("assign", "o2", null, "  Dana ");
            string request = Path.Combine(root, "requests", id + ".json");
            Check.That(File.Exists(request), "request written");
            Check.Equal(0, Directory.GetFiles(Path.Combine(root, "requests"), "*.tmp").Length, "no temporary file left");
            using (var doc = JsonDocument.Parse(File.ReadAllText(request)))
            {
                var r = doc.RootElement;
                // Keys read by src/second_brain/speakers.py command().
                Check.Equal("action,id,name,observation,person", string.Join(",", r.EnumerateObject().Select(p => p.Name).Order()), "request keys");
                Check.Equal(id, r.GetProperty("id").GetString(), "id matches the file name");
                Check.That(Guid.TryParse(id, out _) && id == id.ToLowerInvariant(), "id is a lowercase uuid");
                Check.Equal("Dana", r.GetProperty("name").GetString(), "name trimmed");
                Check.Equal(JsonValueKind.Null, r.GetProperty("person").ValueKind, "new person has no id");
            }
            string rename = client.Send("rename", null, "p-alice", "Alicia");
            using (var doc = JsonDocument.Parse(File.ReadAllText(Path.Combine(root, "requests", rename + ".json"))))
                Check.Equal(JsonValueKind.Null, doc.RootElement.GetProperty("observation").ValueKind, "rename sends no observation");

            Check.Equal<ReviewResponse?>(null, client.TryReadResponse(id), "no response yet");
            File.WriteAllText(Path.Combine(root, "responses", id + ".json"), $"{{\"id\": \"{id}\", \"ok\": true}}");
            Check.Equal<ReviewResponse?>(new ReviewResponse(true, null), client.TryReadResponse(id), "ok response");
            File.WriteAllText(Path.Combine(root, "responses", rename + ".json"), $"{{\"id\": \"{rename}\", \"ok\": false, \"error\": \"Person no longer exists\"}}");
            Check.Equal<ReviewResponse?>(new ReviewResponse(false, "Person no longer exists"), client.TryReadResponse(rename), "error response");

            var good = new Clip(0, 1, "", "clean", "o1-0.wav");
            Check.Equal(Path.Combine(root, "clips", "o1-0.wav"), client.ClipPath(good), "clip path");
            foreach (var bad in new[] { "..\\secret.wav", "sub/o1.wav", "C:o1.wav", "o1.mp3", "", ".." })
                Check.Throws<InvalidDataException>(() => client.ClipPath(good with { File = bad }), $"unsafe clip name '{bad}'");

            Check.Throws<IOException>(() => new ReviewClient(Path.Combine(root, "missing")).Send("clear", "o2", null, ""), "unwritable review directory");
        }
        finally { Directory.Delete(root, recursive: true); }

        Check.Equal("Type a name or choose an existing person.", ReviewClient.ValidateName("   "), "blank name");
        Check.Equal("Type a name or choose an existing person.", ReviewClient.ValidateName(null), "missing name");
        foreach (var ok in new[] { "Alice", " Bob ", "Mary-Jane O'Neil", new string('a', 80) })
            Check.Equal<string?>(null, ReviewClient.ValidateName(ok), $"valid name '{ok}'");
        foreach (var bad in new[] { new string('a', 81), "A[b]", "a<b>", "a|b", "back\\slash", "tab\tname" })
            Check.Equal("Use a name of 1–80 characters without control characters or markup brackets.", ReviewClient.ValidateName(bad), $"invalid name '{bad}'");
    }
}

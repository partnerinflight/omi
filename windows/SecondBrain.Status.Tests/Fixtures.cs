namespace SecondBrain.Status.Tests;

static class Fixtures
{
    // Shape written by src/second_brain/speakers.py catalog(); extra keys (label, manual, version) must be ignored.
    public const string CatalogJson = """
    {"version": 1, "heartbeat": 1000,
     "people": [{"id": "p-alice", "name": "Alice"}, {"id": "p-bob", "name": "Bob"}, {"id": "p-carol", "name": "Carol"}],
     "speakers": [
      {"id": "o1", "job": "job-new", "label": "S1", "display": "Speaker 1", "recorded": "2026-09-28 14:05",
       "clips": [{"start": 1.0, "end": 7.5, "text": "so the demo", "quality": "clean", "file": "o1-0.wav"}],
       "embedding_status": "reference", "person": "p-alice", "manual": 1, "score": null, "name": "Alice", "state": "confirmed"},
      {"id": "o2", "job": "job-new", "label": "S2", "display": "Speaker 2", "recorded": "2026-09-28 14:05", "clips": [],
       "embedding_status": "compared", "person": null, "manual": 0, "score": null, "name": null, "state": "unidentified"},
      {"id": "o3", "job": "job-new", "label": "S3", "display": "Speaker 3", "recorded": "2026-09-28 14:05", "clips": [],
       "embedding_status": "compared", "person": "p-alice", "manual": 0, "score": 0.91, "name": "Alice", "state": "matched"},
      {"id": "o4", "job": "job-old", "label": "S1", "display": "Speaker 1", "recorded": "2026-09-27 18:22", "clips": [],
       "embedding_status": "compared", "person": null, "manual": 0, "score": null, "name": null, "state": "unidentified"},
      {"id": "o5", "job": "job-old", "label": "S2", "display": "Speaker 2", "recorded": "2026-09-27 18:22", "clips": [],
       "embedding_status": "reference", "person": "p-bob", "manual": 1, "score": null, "name": "Bob", "state": "confirmed"}
     ]}
    """;
}

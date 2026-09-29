import json
import math
import hashlib
import shutil
import sys
from pathlib import Path
import tempfile
import unittest
import uuid
import wave
from second_brain.config import Config
from second_brain.io import write_json
from second_brain.speakers import Speakers, match, unit
from second_brain.speaker_audio import candidates, prepare
from second_brain.adaptive.pipeline import parse_vibe_segments


class MatchingTests(unittest.TestCase):
    def test_ambiguous_short_different_model_and_disagreeing_clips_stay_unknown(self):
        a, b = unit([1, 0]), unit([0, 1])
        refs = [("alice", "m", a), ("bob", "m", b)]
        self.assertEqual(match([a, a], "m", refs)[0], "alice")
        for vectors, model in [([a], "m"), ([a, b], "m"), ([a, a], "new-model"), ([unit([1, 1])] * 2, "m")]:
            self.assertIsNone(match(vectors, model, refs)[0])
        self.assertIsNone(match([a, a], "m", refs + [("other", "m", a)])[0])
        with self.assertRaises(ValueError):
            unit([float("nan"), 1])

    def test_multiple_clips_exclude_mixed_and_approximate_turns_from_enrollment(self):
        segments = [dict(start=i * 4, end=i * 4 + 3, speaker="c:S1", text=f"turn {i}") for i in range(7)]
        segments += [
            dict(start=0, end=3, speaker="c:S2", text="overlap"),
            dict(start=40, end=44, speaker="w:S1", text="approx", approximate=True),
        ]
        groups = candidates({"windows": [dict(id="w", final_segments=segments)]})
        self.assertEqual(len(groups[0]["clips"]), 5)
        self.assertTrue(all(s["eligible"] for s in groups[0]["clips"]))
        self.assertFalse(groups[1]["clips"][0]["eligible"])
        self.assertFalse(groups[2]["clips"][0]["eligible"])

    def test_default_rule_accepts_calibrated_omi_level_similarity(self):
        # scripts/speaker_calibration.py on confirmed Omi rows (2026-09-29): same-person samples
        # scored median 0.46 / max 0.66, different people median 0.07 / max 0.52.
        alice, bob = unit([1, 0, 0]), unit([0, 1, 0])
        refs = [("alice", "m", alice), ("bob", "m", bob)]
        near = unit([0.55, 0.10, math.sqrt(1 - 0.55**2 - 0.10**2)])
        self.assertEqual(match([near, near], "m", refs)[0], "alice")
        self.assertIsNone(match([near], "m", refs)[0], "one sample is still not enough")
        between = unit([0.56, 0.53, math.sqrt(1 - 0.56**2 - 0.53**2)])
        self.assertIsNone(match([between, between], "m", refs)[0], "close to two people is still unknown")
        fields = Config.__dataclass_fields__
        self.assertEqual((fields["speaker_match_threshold"].default, fields["speaker_match_margin"].default), (0.50, 0.05))

    def test_review_skips_short_and_wordless_clips_and_speakers_left_without_any(self):
        segments = [
            dict(start=0, end=0.8, speaker="c:S1", text="yes"),
            dict(start=1, end=6, speaker="c:S2", text="..."),
            dict(start=7, end=12, speaker="c:S3", text="real words here"),
            dict(start=12.5, end=13.2, speaker="c:S3", text="ok"),
            dict(start=14, end=19, speaker="c:S4", text=" … "),
            dict(start=20, end=33, speaker="c:S5", text="a long turn with a short tail"),
        ]
        groups = {g["label"]: g["clips"] for g in candidates({"windows": [dict(id="w", final_segments=segments)]})}
        self.assertEqual(sorted(groups), ["c:S3", "c:S5"])
        self.assertEqual([(c["start"], c["end"]) for c in groups["c:S3"]], [(7.0, 12.0)])
        self.assertEqual([(c["start"], c["end"]) for c in groups["c:S5"]], [(20.0, 32.0)], "1 s tail chunk dropped")

    def test_vibe_times_are_bounded_and_marked_approximate(self):
        ref = dict(context_start=10, segments=[dict(approx_start=0, approx_end=10, speaker=0, text="words")])
        segment = parse_vibe_segments(ref, 14, 18, "w")[0]
        self.assertEqual((segment["start"], segment["end"]), (14, 18))
        self.assertTrue(segment["approximate"])

    def test_long_turn_supplies_several_nonoverlapping_clips(self):
        groups = candidates(
            {"windows": [dict(id="w", final_segments=[dict(start=0, end=30, speaker="c:S1", text="long turn")])]}
        )
        self.assertEqual([(c["start"], c["end"]) for c in groups[0]["clips"]], [(0, 12), (12, 24), (24, 30)])
        self.assertTrue(all(c["eligible"] for c in groups[0]["clips"]))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg required")
    def test_encoder_failure_preserves_playable_manual_review(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "source.wav"
            with wave.open(str(audio), "wb") as f:
                f.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                f.writeframes(b"\0" * 96000)
            manifest = dict(
                fallbacks=[], windows=[dict(id="w", final_segments=[dict(start=0, end=3, speaker="c:S1", text="test")])]
            )
            prepare(
                manifest, audio, root, dict(speaker_python=sys.executable, speaker_model=str(root / "missing-model"))
            )
            self.assertEqual(manifest["speaker_embedding_status"], "unavailable")
            self.assertEqual(len(manifest["fallbacks"]), 1)
            with wave.open(manifest["speaker_observations"][0]["clips"][0]["path"]) as clip:
                self.assertEqual(clip.getframerate(), 16000)
                self.assertEqual(clip.getnframes(), 48000)


class SpeakerStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = Speakers(self.root / "data", self.root / "review")

    def ingest(self, job_id, vectors=None, label="c:S1", model="model", text="hello", seconds=3):
        directory = self.root / "data/jobs" / job_id
        directory.mkdir(parents=True, exist_ok=True)
        clips = []
        for i, vector in enumerate(vectors or [[1, 0], [1, 0]]):
            audio = directory / f"{i}.wav"
            with wave.open(str(audio), "wb") as f:
                f.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                f.writeframes(b"\0" * 96000)
            clips.append(
                dict(
                    path=str(audio),
                    start=i * 4,
                    end=i * 4 + seconds,
                    text=text,
                    quality="clean turn",
                    eligible=True,
                    embedding=vector,
                )
            )
        job = dict(id=job_id, metadata=json.dumps(dict(first_utc="2026-09-28T10:00:00Z")))
        manifest = dict(
            windows=[dict(final_segments=[dict(speaker=label, start=0, end=3, text="hello")])],
            speaker_embedding_status="ready",
            speaker_observations=[dict(label=label, clips=clips, model=model)],
        )
        self.store.ingest(job, manifest)
        self.store.catalog()
        row = next((r for r in self.catalog()["speakers"] if r["job"] == job_id), None)
        return row, manifest

    def test_review_hides_useless_rows_but_keeps_named_ones_and_counts_agree(self):
        wordless, _ = self.ingest("a", text="...")
        # A different voice, so naming row "a" below does not auto-match it.
        short, _ = self.ingest("b", vectors=[[0, 1], [0, 1]], seconds=1)
        useful, _ = self.ingest("c", text="we should ship the demo")
        self.assertIsNone(wordless, "a row whose clips have no words is not offered for naming")
        self.assertIsNone(short, "a row whose clips are all under 2 s is not offered for naming")
        self.assertEqual([r["job"] for r in self.catalog()["speakers"]], ["c"])
        self.assertEqual(self.store.summary()["unidentified"], 1, "tray count matches the visible list")
        # A row someone already named stays visible even if its clips are not useful.
        hidden = hashlib.sha256(b"a:c:S1").hexdigest()
        self.assertTrue(self.command("assign", observation=hidden, name="Eugene")["ok"])
        self.store.catalog()
        self.assertEqual(sorted(r["job"] for r in self.catalog()["speakers"]), ["a", "c"])
        self.assertEqual(self.catalog()["speakers"][[r["job"] for r in self.catalog()["speakers"]].index("a")]["clips"], [])

    def catalog(self):
        return json.loads((self.root / "review/catalog.json").read_text())

    def command(self, action, **kwargs):
        return self.store.command(dict(id=str(uuid.uuid4()), action=action, **kwargs))

    def test_enrollment_matches_future_files_persists_and_correction_revokes_matches(self):
        alice, first = self.ingest("a")
        self.assertEqual(alice["display"], "Speaker 1")
        self.assertEqual(len(alice["clips"]), 2)
        self.assertTrue(self.command("assign", observation=alice["id"], name="Alice")["ok"])
        next_alice, second = self.ingest("b", label="c99:S8")
        self.assertEqual(next_alice["name"], "Alice")
        self.assertEqual(next_alice["state"], "matched")
        self.assertIn("Alice (c99:S8)", self.store.render("b", second)["windows"][0]["final_transcript"])
        self.store = Speakers(self.root / "data", self.root / "review")
        self.assertTrue(self.command("rename", person=next_alice["person"], name="Alicia")["ok"])
        self.store.catalog()
        self.assertEqual({r["name"] for r in self.catalog()["speakers"]}, {"Alicia"})
        self.assertTrue(self.command("clear", observation=alice["id"])["ok"])
        self.store.catalog()
        # The automatic match was never enrolled as a new reference.
        self.assertTrue(all(r["person"] is None for r in self.catalog()["speakers"]))

    def test_identity_not_tied_to_number_model_and_people_remain_separate(self):
        first, _ = self.ingest("a")
        self.command("assign", observation=first["id"], name="Alice")
        other, _ = self.ingest("b", vectors=[[0, 1], [0, 1]])
        self.assertIsNone(other["person"])
        other_model, _ = self.ingest("c", model="different")
        self.assertIsNone(other_model["person"])

    def test_mailbox_replay_validation_and_private_catalog(self):
        row, manifest = self.ingest("a")
        key = str(uuid.uuid4())
        request = dict(id=key, action="assign", observation=row["id"], name="Eugene")
        path = self.root / "review/requests" / f"{key}.json"
        write_json(path, request)
        self.store.tick()
        self.assertTrue(json.loads((self.root / "review/responses" / path.name).read_text())["ok"])
        write_json(path, request)
        self.store.tick()
        self.assertEqual(len(self.catalog()["people"]), 1)
        self.assertNotIn("vectors", json.dumps(self.catalog()))
        self.assertNotIn("embedding_model", json.dumps(self.catalog()))
        self.assertFalse(self.command("assign", observation={}, name="Broken")["ok"])
        self.assertFalse(self.command("assign", observation=row["id"], name="bad\nname")["ok"])
        self.store.ingest(dict(id="a", metadata="{}"), manifest)
        self.store.catalog()
        self.assertEqual(self.catalog()["speakers"][0]["name"], "Eugene")
        self.assertTrue(self.command("forget", person=self.catalog()["people"][0]["id"])["ok"])
        self.store.catalog()
        self.assertEqual(self.catalog()["people"], [])

    def test_clip_path_cannot_escape_private_job(self):
        job = dict(id="a", metadata="{}")
        with self.assertRaises(ValueError):
            self.store.ingest(
                job, dict(speaker_observations=[dict(label="s", clips=[dict(path=str(self.root / "outside.wav"))])])
            )

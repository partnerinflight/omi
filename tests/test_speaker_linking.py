"""MOSS chunk-local speaker labels become recording-wide speakers via Nemotron turns."""

from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def seg(start, end, speaker, text="words"):
    return {"start": start, "end": end, "speaker": speaker, "text": text}


class ParseRttmTests(unittest.TestCase):
    def test_reads_speaker_lines_and_skips_everything_else(self):
        from second_brain.adaptive.linking import parse_rttm
        text = ("SPEAKER rec 1 1.500 2.000 <NA> <NA> speaker_0 <NA> <NA>\n"
                "# a comment\n\n"
                "SPEAKER rec 1 4.000 0.000 <NA> <NA> speaker_1 <NA> <NA>\n"   # zero length
                "SPEAKER rec.Mix-Headset 1 5.000 1.250 <NA> <NA> speaker_1 <NA> <NA>\n")
        self.assertEqual(parse_rttm(text), [(1.5, 3.5, "speaker_0"), (5.0, 6.25, "speaker_1")])


class LinkSpeakersTests(unittest.TestCase):
    def link(self, segments, turns, **kw):
        from second_brain.adaptive.linking import link_speakers
        return link_speakers(segments, turns, **kw)

    def test_the_same_voice_in_different_chunks_becomes_one_speaker(self):
        segments = [seg(0, 4, "c0000:S1"), seg(4, 8, "c0000:S2"),
                    seg(240, 244, "c0001:S1"), seg(244, 248, "c0001:S2")]
        # MOSS numbers speakers per chunk by appearance; here chunk 1 happens to swap them.
        turns = [(0, 4, "a"), (4, 8, "b"), (240, 244, "b"), (244, 248, "a")]
        mapping, report = self.link(segments, turns)
        self.assertEqual(mapping, {"c0000:S1": "spk1", "c0000:S2": "spk2",
                                   "c0001:S1": "spk2", "c0001:S2": "spk1"})
        self.assertEqual(report, {"labels": 4, "linked": 4, "kept_apart": 0, "unlinked": 0, "speakers": 2})

    def test_two_voices_moss_separated_in_one_chunk_never_merge(self):
        # Nemotron (capped at 8 speakers) puts both onto "a"; MOSS heard two people.
        segments = [seg(0, 6, "c0000:S1"), seg(6, 8, "c0000:S2")]
        turns = [(0, 8, "a")]
        mapping, report = self.link(segments, turns)
        self.assertNotEqual(mapping["c0000:S1"], mapping["c0000:S2"])
        self.assertEqual(mapping["c0000:S1"], "spk1")           # the stronger claim keeps the link
        self.assertEqual(report["kept_apart"], 1)

    def test_a_label_with_weak_evidence_keeps_its_own_identity(self):
        segments = [seg(0, 10, "c0000:S1")]
        turns = [(0, 2, "a")]                                    # 20% of the label's speech
        mapping, report = self.link(segments, turns)
        self.assertEqual(report, {"labels": 1, "linked": 0, "kept_apart": 0, "unlinked": 1, "speakers": 1})
        self.assertEqual(mapping, {"c0000:S1": "spk1"})
        mapping, report = self.link(segments, turns, min_share=0.1)
        self.assertEqual(report["linked"], 1)

    def test_owner_and_unknown_labels_are_left_alone(self):
        segments = [seg(0, 2, "owner"), seg(2, 4, "c0000:S?"), seg(4, 6, "c0000:S1")]
        mapping, _ = self.link(segments, [(0, 6, "a")])
        self.assertEqual(mapping, {"c0000:S1": "spk1"})

    def test_global_names_follow_first_appearance_and_are_deterministic(self):
        segments = [seg(10, 12, "c0000:S2"), seg(0, 2, "c0000:S1"), seg(300, 302, "c0001:S1")]
        turns = [(0, 2, "x"), (10, 12, "y"), (300, 302, "z")]
        first, _ = self.link(segments, turns)
        again, _ = self.link(list(reversed(segments)), list(reversed(turns)))
        self.assertEqual(first, {"c0000:S1": "spk1", "c0000:S2": "spk2", "c0001:S1": "spk3"})
        self.assertEqual(first, again)

    def test_no_turns_means_every_label_stays_its_own_speaker(self):
        segments = [seg(0, 2, "c0000:S1"), seg(240, 242, "c0001:S1")]
        mapping, report = self.link(segments, [])
        self.assertEqual(mapping, {"c0000:S1": "spk1", "c0001:S1": "spk2"})
        self.assertEqual(report["unlinked"], 2)


def sine_wav(path: Path, seconds: float, channels: int = 1) -> Path:
    spec = "|".join(["0.3*sin(2*PI*700*t)"] * channels)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
                    "-i", f"aevalsrc='{spec}':s=16000:d={seconds}", "-c:a", "pcm_s16le", str(path)], check=True)
    return path


def pipeline_config(root: Path, **extra) -> Path:
    cfg = json.loads((ROOT / "config/pipeline.example.json").read_text())
    cfg.update(moss_command=[sys.executable, str(ROOT / "tests/fixtures/fake_moss.py")],
               scan_vault_for_novelty=False, long_silence_seconds=999, memory_gate_min_words=1,
               work_root=str(root / "work"), **extra)
    path = root / "pipeline.json"
    path.write_text(json.dumps(cfg))
    return path


def run_pipeline(root: Path, audio: Path, out: Path, config: Path, *extra) -> dict:
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]))
    proc = subprocess.run([sys.executable, "-m", "second_brain.adaptive.pipeline", "--audio", str(audio),
                           "--config", str(config), "--output-dir", str(out), "--no-hermes", "--skip-vibe7", *extra],
                          capture_output=True, text=True, env=env)
    if proc.returncode:
        raise AssertionError(proc.stdout[-3000:] + proc.stderr[-3000:])
    return json.loads((out / "manifest.json").read_text())


FAKE = [sys.executable, str(ROOT / "tests/fixtures/fake_nemo_speech.py")]


class PipelineLinkingTests(unittest.TestCase):
    """Three 2 s MOSS chunks (coarse_max_seconds=2): fake MOSS labels each half S1/S2 per chunk,
    so without linking there are six labels; the fake diarizer says it is two people."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.audio = sine_wav(cls.root / "omi.wav", 6)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def segments(self, manifest):
        return [s for w in manifest["windows"] for s in w["moss_segments"]]

    def test_chunk_labels_become_two_recording_wide_speakers(self):
        config = pipeline_config(self.root, coarse_max_seconds=2,
                                 speaker_linking_command=FAKE + ["--block", "2"],
                                 speaker_linking_model=str(self.root / "model.gguf"))
        manifest = run_pipeline(self.root, self.audio, self.root / "linked", config)
        labels = {s["speaker"] for s in self.segments(manifest)}
        self.assertEqual(labels, {"spk1", "spk2"})
        self.assertEqual({s["chunk_speaker"] for s in self.segments(manifest)},
                         {f"c000{i}:S{j}" for i in range(3) for j in (1, 2)})
        self.assertEqual(manifest["speaker_linking"],
                         {"labels": 6, "linked": 6, "kept_apart": 0, "unlinked": 0, "speakers": 2})
        received = json.loads((self.root / "linked/linking.rttm.input.json").read_text())
        self.assertEqual((received["channels"], received["rate"]), (1, 16000))
        self.assertAlmostEqual(received["duration"], 6, delta=0.1)
        # The archive carries the linked labels too, with the original kept alongside.
        archive = json.loads((self.root / "linked/moss_all_segments.json").read_text())
        self.assertEqual({s["speaker"] for s in archive}, {"spk1", "spk2"})

    def test_linking_is_off_unless_configured(self):
        manifest = run_pipeline(self.root, self.audio, self.root / "plain",
                                pipeline_config(self.root, coarse_max_seconds=2))
        self.assertNotIn("speaker_linking", manifest)
        self.assertEqual({s["speaker"] for s in self.segments(manifest)},
                         {f"c000{i}:S{j}" for i in range(3) for j in (1, 2)})

    def test_a_failing_diarizer_keeps_chunk_labels_and_records_a_fallback(self):
        config = pipeline_config(self.root, coarse_max_seconds=2,
                                 speaker_linking_command=[sys.executable, "-c", "import sys; sys.exit(3)"],
                                 speaker_linking_model=str(self.root / "model.gguf"))
        manifest = run_pipeline(self.root, self.audio, self.root / "failed", config)
        self.assertNotIn("speaker_linking", manifest)
        self.assertIn({"from": "speaker linking", "to": "chunk-local labels", "reason": "RuntimeError"},
                      manifest["fallbacks"])
        self.assertEqual(len({s["speaker"] for s in self.segments(manifest)}), 6)

    def test_a_rerun_into_the_same_directory_still_links(self):
        # Recovery reuses the attempt directory; the real CLI refuses to overwrite its --output.
        config = pipeline_config(self.root, coarse_max_seconds=2,
                                 speaker_linking_command=FAKE + ["--block", "2"],
                                 speaker_linking_model=str(self.root / "model.gguf"))
        run_pipeline(self.root, self.audio, self.root / "rerun", config)
        manifest = run_pipeline(self.root, self.audio, self.root / "rerun", config)
        self.assertIn("speaker_linking", manifest)
        self.assertEqual({s["speaker"] for s in self.segments(manifest)}, {"spk1", "spk2"})
        self.assertFalse([f for f in manifest["fallbacks"] if f["from"] == "speaker linking"])

    def test_a_recording_longer_than_the_cap_keeps_chunk_labels(self):
        config = pipeline_config(self.root, coarse_max_seconds=2, speaker_linking_max_seconds=5,
                                 speaker_linking_command=FAKE + ["--block", "2"],
                                 speaker_linking_model=str(self.root / "model.gguf"))
        manifest = run_pipeline(self.root, self.audio, self.root / "capped", config)
        self.assertNotIn("speaker_linking", manifest)
        self.assertIn({"from": "speaker linking", "to": "chunk-local labels",
                       "reason": "recording longer than speaker_linking_max_seconds"}, manifest["fallbacks"])
        self.assertFalse((self.root / "capped/linking.rttm").exists())
        self.assertEqual(len({s["speaker"] for s in self.segments(manifest)}), 6)


class LinkRecordingSpeakersTests(unittest.TestCase):
    def test_nothing_linkable_never_runs_the_diarizer(self):
        from second_brain.adaptive.pipeline import link_recording_speakers
        segments = [seg(0, 2, "owner"), seg(2, 4, "owner")]
        cfg = {"speaker_linking_command": [sys.executable, "-c", "import sys; sys.exit(9)"],
               "speaker_linking_model": "model.gguf"}
        manifest = {"fallbacks": []}
        with tempfile.TemporaryDirectory() as tmp:
            result = link_recording_speakers("missing.wav", segments, cfg, Path(tmp), manifest, 4.0, "R")
        self.assertEqual([s["speaker"] for s in result], ["owner", "owner"])
        self.assertEqual(manifest, {"fallbacks": []})

    def test_an_unexpected_error_keeps_chunk_labels(self):
        from second_brain.adaptive.pipeline import link_recording_speakers
        writes_empty_rttm = "import sys; a = sys.argv; open(a[a.index('--output') + 1], 'w').close()"
        cfg = {"speaker_linking_command": [sys.executable, "-c", writes_empty_rttm],
               "speaker_linking_model": "model.gguf"}
        manifest = {"fallbacks": []}
        with tempfile.TemporaryDirectory() as tmp, \
                unittest.mock.patch("second_brain.adaptive.linking.link_speakers", side_effect=KeyError("bug")):
            audio = sine_wav(Path(tmp) / "a.wav", 2)
            result = link_recording_speakers(str(audio), [seg(0, 2, "c0000:S1")], cfg, Path(tmp), manifest, 2.0)
        self.assertEqual(result[0]["speaker"], "c0000:S1")
        self.assertEqual(manifest["fallbacks"],
                         [{"from": "speaker linking", "to": "chunk-local labels", "reason": "KeyError"}])

class MeetingLinkingTests(unittest.TestCase):
    def test_remote_channel_is_linked_and_the_owner_is_untouched(self):
        from tests.test_meetings import make_capture
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            capture = make_capture(root / "capture.caf")          # L owner tone, R remote tone, 10 s
            cfg = json.loads((ROOT / "config/pipeline.example.json").read_text())
            cfg.update(moss_command=[sys.executable, str(ROOT / "tests/fixtures/fake_moss_tones.py")],
                       scan_vault_for_novelty=False, long_silence_seconds=999, memory_gate_min_words=1,
                       work_root=str(root / "work"), speaker_linking_command=FAKE,
                       speaker_linking_model=str(root / "model.gguf"))
            config = root / "pipeline.json"
            config.write_text(json.dumps(cfg))
            manifest = run_pipeline(root, capture, root / "run", config, "--meeting")
            segments = [s for w in manifest["windows"] for s in w["moss_segments"]]
            self.assertEqual({s["speaker"] for s in segments if s.get("channel") == "L"}, {"owner"})
            self.assertEqual({s["speaker"] for s in segments if s.get("channel") == "R"}, {"spk1"})
            received = json.loads((root / "run/linking.rttm.input.json").read_text())
            self.assertEqual(received["channels"], 1)


class DownstreamLabelTests(unittest.TestCase):
    def test_one_review_observation_per_linked_person(self):
        from second_brain.speaker_audio import candidates
        manifest = {"windows": [{"id": "w0000", "moss_segments": [
            {"start": 0.0, "end": 4.0, "speaker": "spk1", "chunk_speaker": "c0000:S1", "text": "one two three"},
            {"start": 240.0, "end": 244.0, "speaker": "spk1", "chunk_speaker": "c0001:S2", "text": "four five six"},
            {"start": 244.0, "end": 248.0, "speaker": "spk2", "chunk_speaker": "c0001:S1", "text": "seven eight"},
        ]}]}
        observations = {o["label"]: o for o in candidates(manifest)}
        self.assertEqual(set(observations), {"spk1", "spk2"})
        self.assertEqual(len(observations["spk1"]["clips"]), 2)    # clips from both chunks

if __name__ == "__main__":
    unittest.main()

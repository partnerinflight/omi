"""MOSS chunk-local speaker labels become recording-wide speakers via Nemotron turns."""

from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()

"""Meeting captures from the Mac app become one vault note each (spec §3, Plan 2a)."""

from __future__ import annotations
import asyncio
import dataclasses
import json
import os
import subprocess
import sys
import tempfile
import unittest
import wave
from array import array
from pathlib import Path

from second_brain.config import Config

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))


def write_config(root: Path, **extra):
    for name in ("data", "incoming", "vault"):
        (root / name).mkdir(exist_ok=True)
    raw = {
        "data_dir": str(root / "data"),
        "incoming_dir": str(root / "incoming"),
        "secret_file": str(root / "secret.hex"),
        "pipeline_config": str(root / "pipeline.json"),
        "vault_path": str(root / "vault"),
        "status_file": str(root / "status.json"),
        **extra,
    }
    path = root / "service.json"
    path.write_text(json.dumps(raw))
    return path


class MeetingConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_meetings_are_off_by_default(self):
        cfg = Config.load(write_config(self.root))
        self.assertFalse(cfg.meetings_enabled)
        self.assertEqual(cfg.meetings_vault_folder, "Omi/Meetings")
        self.assertEqual(cfg.owner_name, "Me")

    def test_meeting_keys_load(self):
        cfg = Config.load(write_config(self.root, meetings_enabled=True, meetings_vault_folder="Meetings",
                                       owner_name="Eugene"))
        self.assertTrue(cfg.meetings_enabled)
        self.assertEqual((cfg.meetings_vault_folder, cfg.owner_name), ("Meetings", "Eugene"))

    def test_unsafe_meeting_folder_is_rejected(self):
        for folder in ("../x", "/abs", "a/./b", "C:x", ""):
            with self.assertRaisesRegex(ValueError, "meetings_vault_folder must be a safe relative folder"):
                Config.load(write_config(self.root, meetings_vault_folder=folder))

    def test_owner_name_is_a_plain_display_name(self):
        for name in ("", " ", "x" * 81, "Bad [[link]]", "tab\tname"):
            with self.assertRaisesRegex(ValueError, "owner_name"):
                Config.load(write_config(self.root, owner_name=name))


L_EXPR = "if(lt(t,5),0.5*sin(2*PI*440*t),0.05*sin(2*PI*880*t))"
R_EXPR = "if(lt(t,5),0,0.5*sin(2*PI*880*t))"


def make_capture(path: Path, seconds: int = 10) -> Path:
    """Stereo Opus: owner tone on L, remote tone on R, remote bleed into L after 5 s."""
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"aevalsrc='{L_EXPR}|{R_EXPR}':s=48000:d={seconds}",
         "-c:a", "libopus", "-b:a", "96k", "-f", "ogg", str(path)],
        check=True,
    )
    return path


class ChannelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.capture = make_capture(Path(cls.tmp.name) / "capture.caf")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_frames_cover_the_capture_in_50_ms_steps(self):
        from second_brain.adaptive.channels import stereo_frames
        left, right = stereo_frames(self.capture)
        self.assertEqual(len(left), len(right))
        self.assertAlmostEqual(len(left), 200, delta=2)

    def test_mic_dominates_only_where_the_owner_speaks(self):
        from second_brain.adaptive.channels import mic_dominates, span_level_db, stereo_frames
        left, right = stereo_frames(self.capture)
        self.assertTrue(mic_dominates(left, right, 0.5, 4.5))
        self.assertFalse(mic_dominates(left, right, 5.5, 9.5))  # bleed is ~20 dB below the remote channel
        self.assertAlmostEqual(span_level_db(right, 5.5, 9.5) - span_level_db(left, 5.5, 9.5), 20, delta=3)

    def test_silence_on_both_channels_is_not_the_owner(self):
        from second_brain.adaptive.channels import mic_dominates
        self.assertFalse(mic_dominates([0.0] * 10, [0.0] * 10, 0, 0.5))
        self.assertEqual(mic_dominates([4.0] * 10, [0.0] * 10, 0, 0.5), True)

    def test_downmix_selects_a_channel_or_mixes(self):
        from second_brain.adaptive.channels import downmix
        self.assertEqual(downmix(None), ["-ac", "1"])
        self.assertEqual(downmix("L"), ["-af", "pan=mono|c0=c0"])
        self.assertEqual(downmix("R"), ["-af", "pan=mono|c0=c1"])

    def test_utc_from_ms(self):
        from second_brain.io import utc_from_ms
        self.assertEqual(utc_from_ms(1759761000000), "2025-10-06T14:30:00Z")

    def test_stereo_frames_leaves_no_pipes_open(self):
        import gc
        import warnings
        from second_brain.adaptive.channels import stereo_frames
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            stereo_frames(self.capture)
            gc.collect()
        self.assertEqual([w for w in caught if issubclass(w.category, ResourceWarning)], [])

    def test_undecodable_capture_raises_a_clear_error(self):
        from second_brain.adaptive.channels import stereo_frames
        bad = Path(self.tmp.name) / "bad.caf"
        bad.write_bytes(b"not audio")
        with self.assertRaisesRegex(RuntimeError, "ffmpeg could not decode the capture"):
            stereo_frames(bad)


def dominant_hz(path: Path) -> float:
    with wave.open(str(path)) as w:
        frames = w.readframes(w.getnframes())
        rate, n = w.getframerate(), w.getnframes()
    samples = array("h")
    samples.frombytes(frames)
    crossings = sum(1 for a, b in zip(samples, samples[1:]) if (a < 0) != (b < 0))
    return crossings / 2 / (n / rate)


class ChannelExtractionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.capture = make_capture(cls.root / "capture.caf")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_extract_wav_selects_the_requested_channel(self):
        from second_brain.adaptive.pipeline import extract_wav
        left = extract_wav(str(self.capture), 0, 5, 16000, self.root / "l.wav", "L")
        right = extract_wav(str(self.capture), 5, 10, 16000, self.root / "r.wav", "R")
        self.assertAlmostEqual(dominant_hz(left), 440, delta=30)
        self.assertAlmostEqual(dominant_hz(right), 880, delta=30)
        with wave.open(str(left)) as w:
            self.assertEqual((w.getnchannels(), w.getframerate()), (1, 16000))

    def test_silence_detection_can_listen_to_one_channel(self):
        from second_brain.adaptive.pipeline import detect_long_silences
        silences = detect_long_silences(str(self.capture), -42.0, 2.0, "R")
        self.assertEqual(len(silences), 1)
        self.assertAlmostEqual(silences[0][1], 5.0, delta=0.3)
        self.assertEqual(detect_long_silences(str(self.capture), -42.0, 2.0, "L"), [])

    def test_channel_count(self):
        from second_brain.adaptive.pipeline import ffprobe_channels
        self.assertEqual(ffprobe_channels(str(self.capture)), 2)


from fake_moss_tones import OWNER_TEXT, REMOTE_TEXT  # noqa: E402  (fixtures dir is on sys.path, see top)


def meeting_pipeline_config(root: Path) -> Path:
    cfg = json.loads((ROOT / "config/pipeline.example.json").read_text())
    cfg.update(moss_command=[sys.executable, str(ROOT / "tests/fixtures/fake_moss_tones.py")],
               scan_vault_for_novelty=False, long_silence_seconds=999, memory_gate_min_words=1,
               work_root=str(root / "work"))
    path = root / "pipeline.json"
    path.write_text(json.dumps(cfg))
    return path


class MeetingPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        capture = make_capture(root / "capture.caf")
        out = root / "run"
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), os.environ.get("PYTHONPATH", "")]))
        proc = subprocess.run(
            [sys.executable, "-m", "second_brain.adaptive.pipeline", "--audio", str(capture),
             "--config", str(meeting_pipeline_config(root)), "--output-dir", str(out), "--meeting",
             "--no-hermes"],
            capture_output=True, text=True, env=env,
        )
        if proc.returncode:
            raise AssertionError(proc.stdout[-3000:] + proc.stderr[-3000:])
        cls.manifest = json.loads((out / "manifest.json").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def segments(self):
        return [s for w in self.manifest["windows"] for s in w["final_segments"]]

    def test_owner_speech_comes_from_the_mic_channel(self):
        owner = [s for s in self.segments() if s.get("channel") == "L"]
        self.assertEqual([(s["speaker"], s["text"]) for s in owner], [("owner", OWNER_TEXT)])
        self.assertAlmostEqual(owner[0]["end"], 5.0, delta=0.2)

    def test_remote_speech_is_diarized_on_the_remote_channel(self):
        remote = [s for s in self.segments() if s.get("channel") == "R"]
        self.assertEqual([(s["speaker"], s["text"]) for s in remote], [("r0000:S1", REMOTE_TEXT)])

    def test_remote_bleed_into_the_mic_is_dropped_and_recorded(self):
        meeting = self.manifest["meeting"]
        self.assertEqual(meeting["owner_segments"], 1)
        self.assertEqual(meeting["remote_segments"], 1)
        self.assertEqual([s["text"] for s in meeting["bleed_dropped"]], [REMOTE_TEXT])
        self.assertEqual(sum(s["text"] == REMOTE_TEXT for s in self.segments()), 1)

    def test_meeting_windows_never_use_mono_refinement(self):
        self.assertTrue(self.manifest["windows"])
        self.assertEqual({w["asr_tier"] for w in self.manifest["windows"]}, {"moss"})
        self.assertEqual({c["channel"] for c in self.manifest["coarse_chunks"]}, {"L", "R"})

    def test_owner_is_not_offered_for_speaker_review(self):
        labels = {o["label"] for o in self.manifest["speaker_observations"]}
        self.assertEqual(labels, {"r0000:S1"})
        self.assertEqual(self.manifest["speaker_observations"][0]["channel"], "R")


class MeetingSpeakerTests(unittest.TestCase):
    def setUp(self):
        from second_brain.speakers import Speakers
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.speakers = Speakers(root / "data", root / "review")
        self.manifest = {"windows": [{
            "id": "w0000", "memory_keep": False, "scores": {"importance": 20},
            "final_segments": [
                {"start": 0.0, "end": 5.0, "speaker": "owner", "channel": "L", "text": OWNER_TEXT},
                {"start": 5.0, "end": 10.0, "speaker": "r0000:S1", "channel": "R", "text": REMOTE_TEXT},
            ]}]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_mic_segments_render_as_the_owner(self):
        window = self.speakers.render("job", self.manifest, owner_name="Eugene")["windows"][0]
        self.assertIn(f"[0.00-5.00] Eugene (mic): {OWNER_TEXT}", window["final_transcript"])
        self.assertIn(f"r0000:S1 (r0000:S1): {REMOTE_TEXT}", window["final_transcript"])
        self.assertEqual(window["speaker_identities"]["owner"], {"name": "Eugene", "source": "meeting microphone"})

    def test_omi_rendering_is_unchanged_without_an_owner(self):
        window = self.speakers.render("job", self.manifest)["windows"][0]
        self.assertIn("owner (owner):", window["final_transcript"])

    def test_meeting_speakers_are_reviewable_even_if_the_gate_dropped_the_window(self):
        clip = Path(self.tmp.name) / "data" / "jobs" / "job" / "c.wav"
        clip.parent.mkdir(parents=True)
        clip.write_bytes(b"RIFF")
        manifest = dict(self.manifest, speaker_observations=[{"label": "r0000:S1", "channel": "R", "clips": [
            {"start": 5.0, "end": 10.0, "text": REMOTE_TEXT, "quality": "clean turn", "path": str(clip)}]}])
        job = {"id": "job", "metadata": json.dumps({"source": "meeting", "first_utc": "2026-10-06T14:30:00Z"})}
        self.speakers.ingest(job, manifest)
        with self.speakers.connect() as db:
            self.assertEqual(db.execute("SELECT published FROM observations").fetchone()[0], 1)

    def test_omi_speakers_still_follow_the_memory_gate(self):
        clip = Path(self.tmp.name) / "data" / "jobs" / "job" / "c.wav"
        clip.parent.mkdir(parents=True)
        clip.write_bytes(b"RIFF")
        manifest = dict(self.manifest, speaker_observations=[{"label": "r0000:S1", "channel": "R", "clips": [
            {"start": 5.0, "end": 10.0, "text": REMOTE_TEXT, "quality": "clean turn", "path": str(clip)}]}])
        job = {"id": "job", "metadata": json.dumps({"first_utc": "2026-10-06T14:30:00Z"})}
        self.speakers.ingest(job, manifest)
        with self.speakers.connect() as db:
            self.assertEqual(db.execute("SELECT published FROM observations").fetchone()[0], 0)


def meeting_job(audio=str(Path(tempfile.gettempdir()) / "meetings" / "abc.caf")):
    return {"id": "f" * 64, "audio": audio, "sha256": "0" * 64, "metadata": json.dumps({
        "source": "meeting", "capture_id": "ab" * 16, "app": "us.zoom.xos",
        "start_ms": 1759761000000, "end_ms": 1759764600000, "first_utc": "2025-10-06T14:30:00Z"})}


def meeting_manifest():
    return {"windows": [
        {"id": "w0000", "start": 0.0, "end": 65.0, "memory_keep": False,
         "memory_gate": {"contains": {"decision": True, "task": True, "date_or_event": False}},
         "final_transcript": "[0.00-5.00] Eugene (mic): We decided.",
         "speaker_identities": {"owner": {"name": "Eugene", "source": "meeting microphone"}}},
        {"id": "w0001", "start": 3700.0, "end": 3720.0, "memory_keep": False,
         "memory_gate": {"contains": {}},
         "final_transcript": "[3700.00-3720.00] Ana (r0001:S1): Small talk.",
         "speaker_identities": {"r0001:S1": {"id": "p1", "name": "Ana", "source": "voice match"}}},
    ]}


class MeetingNoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self.tmp.name) / "vault"
        self.vault.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def publish(self, manifest=None):
        from second_brain.vault import publish_meeting
        return publish_meeting(self.vault, "Omi/Meetings", meeting_job(), manifest or meeting_manifest())

    def test_one_note_with_highlights_first_and_every_window(self):
        written = self.publish()
        self.assertEqual(written, [f"Omi/Meetings/{'f' * 64}.md"])
        text = (self.vault / written[0]).read_text()
        self.assertIn('type: "omi-meeting"', text)
        self.assertIn('app: "us.zoom.xos"', text)
        self.assertIn('started_at: "2025-10-06T14:30:00Z"', text)
        self.assertIn('ended_at: "2025-10-06T15:30:00Z"', text)
        self.assertIn('participants: ["Ana", "Eugene"]', text)
        self.assertLess(text.index("## Highlights"), text.index("## Transcript"))
        self.assertIn("- 0:00–1:05 · decision, task", text)
        # The memory gate dropped both windows; meetings publish them anyway.
        self.assertIn("We decided.", text)
        self.assertIn("### 1:01:40", text)
        self.assertIn("Small talk.", text)

    def test_republishing_is_idempotent_and_edits_are_preserved(self):
        from second_brain.vault import NoteConflict
        path = self.vault / self.publish()[0]
        self.assertEqual(self.publish(), [str(path.relative_to(self.vault))])
        path.write_text(path.read_text() + "\nmy notes\n")
        with self.assertRaises(NoteConflict):
            self.publish()
        self.assertTrue(path.read_text().endswith("my notes\n"))

    def test_a_meeting_without_speech_still_gets_a_note(self):
        text = (self.vault / self.publish({"windows": []})[0]).read_text()
        self.assertIn("No decisions, tasks or dates detected.", text)
        self.assertIn("No speech was transcribed.", text)


class MeetingDiscoveryTests(unittest.TestCase):
    def setUp(self):
        from second_brain.runtime import Runtime
        from tests.helpers import configuration
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = dataclasses.replace(configuration(Path(self.tmp.name)), meetings_enabled=True)
        self.runtime = Runtime(self.cfg)
        meetings = self.cfg.incoming_dir / "meetings"
        (meetings / ".ready").mkdir(parents=True)
        self.audio = meetings / ("ab" * 16 + ".caf")
        self.audio.write_bytes(b"capture")
        (meetings / ".ready" / ("ab" * 16 + ".json")).write_text(json.dumps({"audio": str(self.audio), "metadata": {
            "source": "meeting", "capture_id": "ab" * 16, "app": "us.zoom.xos",
            "start_ms": 1759761000000, "end_ms": 1759764600000}}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_enabled_meetings_are_queued_once_with_a_start_time(self):
        self.runtime.discover()
        self.runtime.discover()
        job = self.runtime.queue.claim()
        self.assertEqual(Path(job["audio"]), self.audio.resolve())
        metadata = json.loads(job["metadata"])
        self.assertEqual(metadata["first_utc"], "2025-10-06T14:30:00Z")
        self.assertIsNone(self.runtime.queue.claim())

    def test_disabled_meetings_are_not_queued(self):
        self.runtime.cfg = dataclasses.replace(self.cfg, meetings_enabled=False)
        self.runtime.discover()
        self.assertIsNone(self.runtime.queue.claim())

    def test_a_receipt_pointing_outside_incoming_is_refused(self):
        receipt = next((self.cfg.incoming_dir / "meetings" / ".ready").glob("*.json"))
        value = json.loads(receipt.read_text())
        value["audio"] = str(Path(self.tmp.name) / "elsewhere.caf")
        receipt.write_text(json.dumps(value))
        self.runtime.discover()
        self.assertIsNone(self.runtime.queue.claim())
        self.assertIn("discovery failed", self.runtime.last_scan_error)


if __name__ == "__main__":
    unittest.main()

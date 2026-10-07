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


if __name__ == "__main__":
    unittest.main()

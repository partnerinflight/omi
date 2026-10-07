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


if __name__ == "__main__":
    unittest.main()

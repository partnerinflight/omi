"""Lecture mode: Omi recordings tagged "lecture" become one transcript note and one summary note
(docs/superpowers/specs/2026-10-21-lecture-mode-design.md §3)."""

from __future__ import annotations
import asyncio
import dataclasses
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from second_brain.io import write_json
from tests.helpers import configuration

ROOT = Path(__file__).resolve().parents[1]


class KindTests(unittest.TestCase):
    def test_receipts_map_to_kinds(self):
        from second_brain.kinds import job_kind
        self.assertEqual(job_kind({}), "omi")                       # receivers before lecture mode
        self.assertEqual(job_kind({"mode": "normal"}), "omi")
        self.assertEqual(job_kind({"mode": "lecture"}), "lecture")
        self.assertEqual(job_kind({"mode": "podcast"}), "omi")      # an unknown mode is normal
        self.assertEqual(job_kind({"source": "meeting"}), "meeting")
        self.assertEqual(job_kind({"source": "meeting", "mode": "lecture"}), "meeting")

    def test_a_stored_kind_wins_and_an_invalid_one_is_ignored(self):
        from second_brain.kinds import job_kind
        self.assertEqual(job_kind({"kind": "lecture"}), "lecture")
        self.assertEqual(job_kind({"kind": "podcast", "mode": "lecture"}), "lecture")

    def test_with_kind_stores_the_derived_kind_and_never_trusts_a_sidecar_kind(self):
        from second_brain.kinds import with_kind
        sidecar = {"device": "d", "kind": "meeting", "mode": "lecture"}
        self.assertEqual(with_kind(sidecar), {"device": "d", "kind": "lecture", "mode": "lecture"})
        self.assertEqual(sidecar["kind"], "meeting", "input is not mutated")


if __name__ == "__main__":
    unittest.main()

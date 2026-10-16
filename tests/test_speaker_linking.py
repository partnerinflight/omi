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


if __name__ == "__main__":
    unittest.main()

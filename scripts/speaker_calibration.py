"""Calibrate speaker matching against the voices you have already confirmed.

Read-only. Prints anonymised numbers only (people become P1, P2, ...; no names, transcripts
or file paths). Run as an administrator with the service's interpreter, for example:

  & 'C:\\Program Files\\SecondBrain\\python\\Scripts\\python.exe' -I scripts\\speaker_calibration.py

Leave-one-out: each confirmed row is treated as unknown and matched against every other
confirmed row. Candidate rules are compared with the service's current rule:

  every-clip  the previous service rule: every sample must clear the threshold and margin
  averaged    speakers.match: the row's mean fingerprint vs each person's mean fingerprint
  averaged+q  the same, using only samples >= 3 s and louder than -45 dBFS on both sides
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import sqlite3
import statistics
import struct
import wave
from pathlib import Path

from second_brain.speakers import match

MIN_SECONDS, MIN_DBFS = 3.0, -45.0


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


def clip_dbfs(path: Path):
    try:
        with wave.open(str(path)) as w:
            frames = w.readframes(w.getnframes())
    except (OSError, wave.Error, EOFError):
        return None
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    if not samples:
        return None
    rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    return 20 * math.log10(max(rms, 1) / 32768)


def samples_with_quality(row, review: Path):
    """Pair each stored voice vector with its clip. Vectors are stored in order for clips the
    service marked 'clean turn'; if the counts disagree the pairing is unknown (quality None)."""
    clean = [c for c in json.loads(row["clips"]) if c.get("quality") == "clean turn"]
    if len(clean) != len(row["vectors"]):
        return [(v, None, None) for v in row["vectors"]]
    return [(v, c["end"] - c["start"], clip_dbfs(review / "clips" / c["file"])) for v, c in zip(row["vectors"], clean)]


def good(sample):
    _, seconds, dbfs = sample
    return seconds is not None and dbfs is not None and seconds >= MIN_SECONDS and dbfs >= MIN_DBFS


def every_clip(row_samples, others, threshold, margin):
    """The rule before 2026-09-29: at least two samples, each clearing threshold and margin
    against its best-matching reference sample, all agreeing on one person."""
    if len(row_samples) < 2:
        return None
    winners = set()
    for vector, _, _ in row_samples:
        best = {}
        for o in others:
            for s in o["samples"]:
                best[o["person"]] = max(best.get(o["person"], -1), dot(vector, s[0]))
        ranking = sorted(best.values(), reverse=True)
        if not ranking or ranking[0] < threshold or ranking[0] - (ranking[1] if len(ranking) > 1 else -1) < margin:
            return None
        winners.add(max(best, key=best.get))
    return winners.pop() if len(winners) == 1 else None


def averaged(row_samples, others, threshold, margin, quality=False):
    use = [s for s in row_samples if good(s)] if quality else row_samples
    refs = [(o["person"], "m", s[0]) for o in others for s in o["samples"] if not quality or good(s)]
    return match([s[0] for s in use], "m", refs, threshold, margin)[0]


def spread(values):
    if not values:
        return "n=0"
    q = statistics.quantiles(values, n=10) if len(values) >= 2 else [values[0]] * 9
    return f"n={len(values):4d}  min {min(values):.2f}  p10 {q[0]:.2f}  median {statistics.median(values):.2f}  p90 {q[8]:.2f}  max {max(values):.2f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path(r"C:\ProgramData\SecondBrain\data\speakers.sqlite3"))
    ap.add_argument("--review", type=Path, default=Path(r"C:\ProgramData\SecondBrain\review"))
    args = ap.parse_args()
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = [dict(r) | {"vectors": json.loads(r["vectors"])} for r in db.execute(
        "SELECT id, job, person, manual, model, vectors, clips FROM observations")]
    for r in rows:
        r["samples"] = samples_with_quality(r, args.review)
    confirmed = [r for r in rows if r["manual"] and r["person"] and r["vectors"]]
    open_rows = [r for r in rows if not r["manual"] and r["vectors"]]
    alias = {p: f"P{i + 1}" for i, (p, _) in enumerate(
        sorted(((p, sum(r["person"] == p for r in confirmed)) for p in {r["person"] for r in confirmed}), key=lambda x: -x[1]))}

    print(f"Confirmed rows with voice samples: {len(confirmed)} across {len(alias)} people")
    for p, a in alias.items():
        mine = [s for r in confirmed if r["person"] == p for s in r["samples"]]
        known = [s for s in mine if s[1] is not None]
        print(f"  {a}: {sum(r['person'] == p for r in confirmed)} rows, {len(mine)} samples, "
              f"{sum(good(s) for s in mine)} pass >= {MIN_SECONDS:.0f} s and >= {MIN_DBFS:.0f} dBFS"
              + (f" ({len(mine) - len(known)} unpaired)" if len(known) != len(mine) else ""))
    print(f"Auto-matchable rows with samples: {len(open_rows)}; with 1 sample: {sum(len(r['vectors']) == 1 for r in open_rows)}")

    for label, keep in (("all samples", lambda s: True), ("quality samples only", good)):
        same, different = [], []
        for a, b in itertools.combinations(confirmed, 2):
            scores = [dot(u[0], v[0]) for u in a["samples"] if keep(u) for v in b["samples"] if keep(v)]
            (same if a["person"] == b["person"] else different).extend(scores)
        print(f"\nSample-to-sample similarity, {label}")
        print("  same person      " + spread(same))
        print("  different people " + spread(different))

    rules = {
        "every-clip": lambda s, o, t, m: every_clip(s, o, t, m),
        "averaged": lambda s, o, t, m: averaged(s, o, t, m),
        "averaged+q": lambda s, o, t, m: averaged(s, o, t, m, quality=True),
    }
    print("\nLeave-one-out on confirmed rows (correct / wrong / no match); open rows the rule would assign")
    print("  rule        threshold margin |  correct wrong none | open assigned")
    for name, decide in rules.items():
        for margin in (0.12, 0.10, 0.05):
            for threshold in (0.60, 0.55, 0.50, 0.45, 0.40, 0.35, 0.30):
                correct = wrong = none = 0
                for row in confirmed:
                    others = [o for o in confirmed if o["id"] != row["id"]]
                    got = decide(row["samples"], others, threshold, margin)
                    correct += got == row["person"]
                    wrong += got is not None and got != row["person"]
                    none += got is None
                assigned = sum(decide(r["samples"], confirmed, threshold, margin) is not None for r in open_rows)
                print(f"  {name:11} {threshold:9.2f} {margin:6.2f} | {correct:8d} {wrong:5d} {none:4d} | {assigned:4d}")
    print("\nService rule today: averaged, threshold 0.40, margin 0.12 (service config speaker_match_*).")


if __name__ == "__main__":
    main()

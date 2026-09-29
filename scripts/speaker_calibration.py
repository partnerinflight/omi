"""Calibrate speaker-matching thresholds against the voices you have already confirmed.

Read-only. Prints anonymised numbers only (people become P1, P2, ...; no names, transcripts
or file paths). Run as an administrator with the service's interpreter, for example:

  & 'C:\\Program Files\\SecondBrain\\python\\Scripts\\python.exe' -I scripts\\speaker_calibration.py

Leave-one-out: each confirmed row is treated as unknown and matched against every other
confirmed row, using the same rule as the service (speakers.match) at several settings.
"""
from __future__ import annotations

import argparse
import itertools
import json
import sqlite3
import statistics
from pathlib import Path

from second_brain.speakers import match


def rule(vectors, model, references, threshold, margin, min_clips):
    """speakers.match with the 2-clip minimum made adjustable (min_clips=2 is the service rule)."""
    if len(vectors) < min_clips:
        return None
    if len(vectors) >= 2:
        return match(vectors, model, references, threshold, margin)[0]
    return match(vectors * 2, model, references, threshold, margin)[0]  # one clip, judged by the same test


def spread(values):
    if not values:
        return "n=0"
    q = statistics.quantiles(values, n=10) if len(values) >= 2 else [values[0]] * 9
    return f"n={len(values):4d}  min {min(values):.2f}  p10 {q[0]:.2f}  median {statistics.median(values):.2f}  p90 {q[8]:.2f}  max {max(values):.2f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path(r"C:\ProgramData\SecondBrain\data\speakers.sqlite3"))
    args = ap.parse_args()
    db = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    rows = [dict(r) | {"vectors": json.loads(r["vectors"])} for r in db.execute(
        "SELECT id, job, person, manual, model, vectors FROM observations")]
    confirmed = [r for r in rows if r["manual"] and r["person"] and r["vectors"]]
    open_rows = [r for r in rows if not r["manual"] and r["vectors"]]
    alias = {p: f"P{i + 1}" for i, (p, _) in enumerate(
        sorted(((p, sum(r["person"] == p for r in confirmed)) for p in {r["person"] for r in confirmed}), key=lambda x: -x[1]))}

    print(f"Confirmed rows with voice samples: {len(confirmed)} across {len(alias)} people "
          + ", ".join(f"{a}={sum(r['person'] == p for r in confirmed)} rows/{sum(len(r['vectors']) for r in confirmed if r['person'] == p)} samples"
                      for p, a in alias.items()))
    print(f"Auto-matchable rows (not confirmed or cleared) with samples: {len(open_rows)}; "
          f"of those with only 1 sample: {sum(len(r['vectors']) == 1 for r in open_rows)}")

    same, different = [], []
    for a, b in itertools.combinations(confirmed, 2):
        if a["model"] != b["model"]:
            continue
        scores = [sum(x * y for x, y in zip(u, v)) for u in a["vectors"] for v in b["vectors"] if len(u) == len(v)]
        (same if a["person"] == b["person"] else different).extend(scores)
    print("\nSample-to-sample similarity between confirmed rows")
    print("  same person      " + spread(same))
    print("  different people " + spread(different))

    print("\nLeave-one-out on confirmed rows (correct / wrong / no match) and open rows that would be assigned")
    print("  threshold margin min_samples |  correct wrong none | open rows assigned")
    for min_clips in (2, 1):
        for margin in (0.10, 0.05):
            for threshold in (0.80, 0.75, 0.70, 0.65, 0.60, 0.55, 0.50):
                correct = wrong = none = 0
                for row in confirmed:
                    refs = [(o["person"], o["model"], v) for o in confirmed if o["id"] != row["id"] for v in o["vectors"]]
                    got = rule(row["vectors"], row["model"], refs, threshold, margin, min_clips)
                    correct += got == row["person"]
                    wrong += got is not None and got != row["person"]
                    none += got is None
                refs = [(o["person"], o["model"], v) for o in confirmed for v in o["vectors"]]
                assigned = sum(rule(r["vectors"], r["model"], refs, threshold, margin, min_clips) is not None for r in open_rows)
                print(f"  {threshold:9.2f} {margin:6.2f} {min_clips:11d} | {correct:8d} {wrong:5d} {none:4d} | {assigned:4d}")
    print("\nCurrent service rule: threshold 0.80, margin 0.10, min_samples 2.")


if __name__ == "__main__":
    main()

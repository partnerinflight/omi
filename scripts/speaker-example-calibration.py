"""Read-only comparison of average profiles and confirmed-example fallback.

Hold out whole recordings so nearby turns cannot validate each other. Outputs
aggregate counts only; no names, transcripts, audio, or fingerprints.
"""
import argparse
import collections
import json
from pathlib import Path
import sqlite3

from second_brain.speakers import match, mean_unit

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--db", type=Path, default=Path(r"C:\ProgramData\SecondBrain\data\speakers.sqlite3"))
args = parser.parse_args()
with sqlite3.connect(f"file:{args.db}?mode=ro", uri=True) as db:
    db.row_factory = sqlite3.Row
    rows = [dict(r) for r in db.execute("SELECT id,job,person,manual,model,vectors FROM observations")]
for row in rows:
    row["vectors"] = json.loads(row["vectors"])
confirmed = [r for r in rows if r["manual"] and r["person"] and r["vectors"]]
results = {}
for rule in ("average", "confirmed_examples"):
    counts = collections.Counter()
    per_person = collections.defaultdict(collections.Counter)
    for row in confirmed:
        other = [r for r in confirmed if r["job"] != row["job"]]
        references = [(r["person"], r["model"], v) for r in other for v in r["vectors"]]
        examples = [(r["person"], r["model"], mean_unit(r["vectors"])) for r in other
                    if mean_unit(r["vectors"])] if rule == "confirmed_examples" else []
        got, _ = match(row["vectors"], row["model"], references, exemplars=examples)
        outcome = "correct" if got == row["person"] else "unmatched" if got is None else "wrong"
        counts[outcome] += 1
        per_person[row["person"]][outcome] += 1
    results[rule] = dict(counts)
    # Stable anonymous aliases, largest confirmed profile first.
    results[rule]["people"] = {f"P{i + 1}": dict(per_person[p]) for i, (p, _) in enumerate(
        sorted(collections.Counter(r["person"] for r in confirmed).items(), key=lambda x: (-x[1], x[0])))}
print(json.dumps(results, indent=2))

"""Clean managed formatting and remove reviewed event IDs with private vault backups."""
import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from second_brain.io import atomic_write
from second_brain.router_cleanup import clean_note

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--vault", type=Path, required=True)
parser.add_argument("--remove", type=Path, help="JSON mapping decision event IDs to review reasons")
args = parser.parse_args()
reasons = json.loads(args.remove.read_text(encoding="utf-8")) if args.remove else {}
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
backup = args.vault / "System/Router/Cleanup" / stamp
changes = []
for folder in ("People", "Projects", "Topics", "Decisions", "Ideas", "Daily"):
    for path in sorted((args.vault / folder).glob("*.md")):
        original = path.read_bytes()
        updated, removed = clean_note(original.decode("utf-8"), set(reasons) if folder == "Decisions" else set())
        if folder in ("Daily", "Decisions") and re.fullmatch(r"\d{4}-\d{2}-\d{2}", path.stem):
            updated = re.sub(r"\A# " + re.escape(path.stem) + r"\r?\n(?:\r?\n)?", "", updated)
        if updated.encode("utf-8") == original:
            continue
        rel = path.relative_to(args.vault)
        atomic_write(backup / rel, original)
        # Refuse to overwrite a concurrent edit after taking the snapshot.
        if path.read_bytes() != original:
            raise RuntimeError(f"Note changed during cleanup: {rel}")
        atomic_write(path, updated.encode("utf-8"))
        changes.append(dict(path=str(rel), removed={eid: reasons[eid] for eid in removed}))
atomic_write(backup / "review.json", json.dumps(changes, indent=2).encode("utf-8"))
print(json.dumps(dict(changed_notes=len(changes), removed_decisions=sum(len(c['removed']) for c in changes),
                      backup=str(backup))))

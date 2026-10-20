"""Register an existing router entry for human clarification without changing its text."""
import argparse
import json
import re
import sqlite3
from pathlib import Path
from second_brain.clarifications import Clarifications
from second_brain.router_markers import normalize, marker as event_marker

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--config', type=Path, required=True)
p.add_argument('--note', required=True)
p.add_argument('--event', required=True)
p.add_argument('--source', required=True)
p.add_argument('--question', required=True)
a = p.parse_args()
if not re.fullmatch('[a-f0-9]{16}', a.event):
    p.error('Invalid event identifier')
cfg = json.loads(a.config.read_text(encoding='utf-8-sig'))
vault, data = Path(cfg['vault_path']), Path(cfg['data_dir'])
note = (vault / a.note).resolve()
note.relative_to(vault.resolve())
marker = ' ' + event_marker(a.event)
lines = [normalize(line) for line in note.read_text(encoding='utf-8').splitlines() if line.startswith('- ') and normalize(line).endswith(marker)]
if len(lines) != 1:
    p.error('Expected exactly one matching router entry')
db = sqlite3.connect(f'file:{(data / "router.sqlite3").as_posix()}?mode=ro', uri=True)
try:
    source = db.execute('SELECT text FROM routes WHERE id=?', (a.source,)).fetchone()
finally:
    db.close()
if source is None:
    p.error('Source is unavailable')
review = Clarifications(data, data.parent/'review', vault)
review.add(a.event, note, lines[0][2:-len(marker)], [a.question], source[0])
# The service owns the review catalog; the interactive account has read-only access.
print(f'Pending clarifications: {review.count()}')

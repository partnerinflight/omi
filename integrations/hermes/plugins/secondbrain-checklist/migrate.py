"""Milestone 5: inventory ToDos/Tasks.md and import it into the Second Brain server.

Dry run by default: it only reads, under the same lock as Telegram callbacks, and reports
what would be imported and what cannot be. --apply sends the import; the server maps each
legacy id to one fixed todo id, so repeating or resuming an import changes nothing.
See docs/mobile/implementation-plan.md (milestone 5) for the cutover order.
"""
from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path

from .checklist import StateStore
from .server_todos import USER_AGENT
from .todos import MARKER, ROW, TodoFile

STATES = {"open": "open", "snoozed": "open", "done": "completed", "dismissed": "dismissed"}


def inventory(vault) -> dict:
    """The import payload plus a report. Raises (and imports nothing) on duplicate ids,
    a Syncthing conflict or malformed rows, exactly as the plugin refuses to edit then."""
    root = Path(vault).expanduser().resolve(strict=True)
    with StateStore(root).locked():
        return build(root)


def build(root: Path) -> dict:
    """Caller holds the reminder state lock."""
    store, todos = StateStore(root), TodoFile(root)
    state = store.read()
    text = todos.read()
    raw = todos.path.read_bytes() if text is not None else None
    if text is None:
        raise ValueError("ToDos/Tasks.md is missing; nothing to migrate")
    rows = todos.rows(text)
    lines = text.splitlines(keepends=True)
    by_line = {row["line"]: eid for eid, row in rows.items()}
    notes, nested, unmarked, prose = {}, [], 0, 0
    fenced, owner = False, None
    for n, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        indented = line.startswith((" ", "\t")) and line.strip()
        if n in by_line and not fenced:
            if indented and owner:
                nested.append(by_line[n])  # imported as its own todo; the nesting is not kept
            else:
                owner = by_line[n]
            continue
        if indented and owner and not MARKER.search(line):
            notes.setdefault(owner, []).append(line.rstrip("\r\n"))
            continue
        if line.strip():
            owner = None
            if ROW.match(line) and not fenced:
                unmarked += 1  # the plugin's sync adds ids; run it before migrating
            elif not line.startswith(("#", "---")):
                prose += 1
    imported = set(state.get("todos_imported", []))
    payload = [{
        "legacy_id": eid, "title": row["text"], "state": STATES[row["status"]],
        "snoozed_until": row["until"], "notes": "\n".join(notes[eid]) if eid in notes else None,
    } for eid, row in rows.items()]
    # Imported once and since removed by hand: a tombstone, so a later intake sync never recreates it.
    for eid in sorted(imported - set(rows)):
        item = state["items"].get(eid, {})
        payload.append({"legacy_id": eid, "title": item.get("text") or "(removed)", "state": "dismissed",
                        "deleted": True})
    stale = sum(1 for eid, row in rows.items()
                if eid in state["items"] and state["items"][eid].get("status") != row["status"])
    counts = {}
    for row in rows.values():
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "source_hash": hashlib.sha256(raw).hexdigest(),
        "state_hash": hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest(),
        "report": {
            "tasks": len(rows), "by_status": counts, "with_notes": len(notes), "tombstones": len(imported - set(rows)),
            "nested_imported_flat": nested, "unmarked_rows_not_imported": unmarked,
            "prose_lines_not_imported": prose, "state_cache_differs": stale,
        },
        "todos": payload,
    }


def apply(vault, server: str, token: str, migration_id: str, opener=urllib.request.urlopen) -> dict:
    """Hold the writer lock across reading and sending: Telegram buttons wait instead of
    changing Tasks.md between the inventory and the import (the final-delta freeze)."""
    root = Path(vault).expanduser().resolve(strict=True)
    with StateStore(root).locked():
        found = build(root)
        body = json.dumps({"migration_id": migration_id, "source_hash": found["source_hash"],
                           "todos": found["todos"]}).encode()
        request = urllib.request.Request(server.rstrip("/") + "/v1/migration/todos", data=body, method="POST",
                                         headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                                  "User-Agent": USER_AGENT})
        with opener(request, timeout=60) as response:
            result = json.loads(response.read())
    return {"report": found["report"], "source_hash": found["source_hash"], "result": result}

"""Editable Obsidian todos. Markdown is authoritative; JSON is only a cache.

All callers hold the reminder state's interprocess lock. The compare-before-write
check detects local editor/sync changes; Syncthing conflicts fail closed. This is
not a distributed lock: conflicting remote edits still need human reconciliation.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
import uuid
from datetime import date
from pathlib import Path

MARKER = re.compile(r"(?:%%\s*router:([^%\s]+)\s*%%|<!--\s*router:([^>\s]+)\s*-->)")
ROW = re.compile(r"^(\s*[-*+]\s+\[)([ xX-])(\]\s+)(.*?)(\r?\n)?$")
SNOOZE = re.compile(r"\s*%% snoozed-until:(\d{4}-\d{2}-\d{2}) %%")
HEADER = "# ToDos\n\nCheck items here or use Done in Telegram. Due dates are included only when known.\nOriginal routed notes remain in [[Projects/_Tasks]] and [[Decisions]].\n\n## Tasks\n\n"


class TodoError(RuntimeError):
    pass


class TodoFile:
    def __init__(self, vault: Path):
        self.vault = vault.resolve()
        self.path = self.vault / "ToDos" / "Tasks.md"

    def check(self):
        for path in (self.path.parent, self.path):
            try:
                path.resolve().relative_to(self.vault)
            except (ValueError, OSError) as exc:
                raise TodoError("ToDos path escapes the vault") from exc
        if self.path.parent.exists() and any(self.path.parent.glob("Tasks.sync-conflict-*.md")):
            raise TodoError("Resolve the ToDos Syncthing conflict before changing tasks")

    def read(self):
        self.check()
        try:
            return self.path.read_bytes().decode("utf-8") if self.path.exists() else None
        except (OSError, UnicodeError) as exc:
            raise TodoError("ToDos is unreadable; refusing to replace it") from exc

    def write(self, before, after):
        self.check()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=".Tasks.", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(after.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            if self.read() != before:
                raise TodoError("ToDos changed while saving; reload before retrying")
            os.replace(temp, self.path)
            if os.name != "nt":
                directory = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            if self.read() != after:
                raise TodoError("Could not confirm saved ToDos")
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def rows(self, text=None):
        text = self.read() if text is None else text
        result = {}
        fenced = False
        for number, line in enumerate((text or "").splitlines(keepends=True)):
            if line.lstrip().startswith(("```", "~~~")):
                fenced = not fenced
                continue
            if fenced:
                continue
            match = ROW.match(line)
            ids = list(MARKER.finditer(line))
            if not match or not ids:
                continue
            if len(ids) != 1:
                raise TodoError("A ToDo row has multiple identifiers")
            eid = ids[0].group(1) or ids[0].group(2)
            if eid in result:
                raise TodoError("Duplicate ToDo identifier; reconcile the notes first")
            snooze = SNOOZE.search(line)
            if snooze:
                try:
                    date.fromisoformat(snooze[1])
                except ValueError as exc:
                    raise TodoError("A ToDo has an invalid snooze date") from exc
            status = "done" if match[2].lower() == "x" else "dismissed" if match[2] == "-" else "snoozed" if snooze else "open"
            body = SNOOZE.sub("", MARKER.sub("", match[4])).strip()
            if not body:
                raise TodoError("A ToDo has no task text")
            result[eid] = dict(line=number, text=body, status=status,
                               until=snooze[1] if status == "snoozed" else None,
                               signature=hashlib.sha256(line.encode()).hexdigest())
        return result

    def reconcile(self, state):
        if self.read() is None:
            return state
        for eid, row in self.rows().items():
            item = dict(state["items"].get(eid, {}))
            old_signature = item.get("todo_signature")
            revision = item.get("revision", 0)
            if old_signature and old_signature != row["signature"]:
                revision += 1
            item.update(status=row["status"], until=row["until"], text=row["text"],
                        source="ToDos/Tasks.md", revision=revision,
                        todo_signature=row["signature"])
            state["items"][eid] = item
        return state

    def promote(self, tasks, state):
        """Append explicitly selected intake tasks; never replace an existing row."""
        before = self.read()
        text = before if before is not None else HEADER
        rows = self.rows(text)
        seen = state.setdefault("todos_imported", [])
        if not isinstance(seen, list) or any(not isinstance(x, str) for x in seen):
            raise TodoError("Invalid ToDos import history")
        # Explicit tasks typed by the owner become stable tasks at the next sync.
        lines = text.splitlines(keepends=True)
        fenced = False
        for n, line in enumerate(lines):
            if line.lstrip().startswith(("```", "~~~")):
                fenced = not fenced
            if not fenced and ROW.match(line) and not MARKER.search(line):
                ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                lines[n] = line.rstrip("\r\n") + f" %% router:{uuid.uuid4().hex} %%" + ending
        text = "".join(lines)
        added = 0
        for task in tasks:
            eid = task.router_id
            if eid in rows or eid in seen:
                continue
            item = state["items"].get(eid, {})
            status = item.get("status", "open")
            check = "x" if status == "done" else "-" if status == "dismissed" else " "
            snooze = f" %% snoozed-until:{item['until']} %%" if status == "snoozed" else ""
            # Intake is one line. Preserve its explicit owner/due metadata verbatim.
            if "\n" in task.text or "\r" in task.text:
                raise TodoError("Task intake must be a single Markdown line")
            text = text.rstrip("\r\n") + f"\n- [{check}] {task.text}{snooze} %% router:{eid} %%\n"
            added += 1
        final = self.rows(text)
        if text != before:
            self.write(before, text)
        state["todos_imported"] = sorted(set(seen) | set(final))
        self.reconcile(state)
        return added

    def change(self, eid, status, until=None, *, expected_signature=None):
        before = self.read()
        if before is None:
            return None  # Legacy operation before explicit migration.
        row = self.rows(before).get(eid)
        if row is None:
            raise TodoError("Task is absent from ToDos; refusing to recreate a removed task")
        if expected_signature and row["signature"] != expected_signature:
            raise TodoError("Task changed while updating; reload before retrying")
        lines = before.splitlines(keepends=True)
        original = lines[row["line"]]
        match = ROW.match(original)
        check = "x" if status == "done" else "-" if status == "dismissed" else " "
        changed = original[:match.start(2)] + check + original[match.end(2):]
        changed = SNOOZE.sub("", changed)
        if status == "snoozed":
            if not until:
                raise TodoError("Missing snooze date")
            marker = MARKER.search(changed)
            changed = changed[:marker.start()] + f"%% snoozed-until:{until} %% " + changed[marker.start():]
        lines[row["line"]] = changed
        after = "".join(lines)
        if before != after:
            self.write(before, after)
        return self.rows(after)[eid]["signature"]

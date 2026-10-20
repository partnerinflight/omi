"""After cutover (milestone 5) the Second Brain server owns todos. This backend gives the
checklist the same interface as TodoFile, so Telegram delivery, buttons and text actions
are unchanged; reads and state changes go to the API instead of ToDos/Tasks.md.

Rows are keyed by the legacy router id (or the server id for todos created in the app), so
existing selections keep working. Each change carries the revision it was made against;
the server rejects stale ones, which surfaces as the plugin's usual "changed" error.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from .todos import TodoError, TodoFile

LEGACY_NS = uuid.UUID("5b0c8d1e-3f7a-4d2b-9c61-0e4a7b2f9d13")  # server/sb_server/app.py
SOURCE = "server"  # differs from "ToDos/Tasks.md", so buttons sent before cutover are refused
STATES = {"done": "completed", "dismissed": "dismissed", "open": "open", "snoozed": "open"}


class ServerTodos(TodoFile):
    SOURCE = SOURCE

    def __init__(self, vault: Path, url: str, token: str, opener=urllib.request.urlopen):
        super().__init__(vault)
        self.url, self.token, self.opener = url.rstrip("/"), token, opener

    def api(self, method: str, path: str, body=None):
        request = urllib.request.Request(self.url + path, method=method,
                                         data=json.dumps(body).encode() if body is not None else None,
                                         headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with self.opener(request, timeout=20) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as exc:
            exc.close()
            raise TodoError(f"Second Brain server returned {exc.code}") from exc
        except (OSError, ValueError) as exc:
            raise TodoError("Second Brain server is unreachable; nothing was changed") from exc

    def check(self):
        pass  # no file to guard

    def read(self):
        """A snapshot of every live todo; never None, so the checklist treats migration as done."""
        return json.dumps(self.api("GET", "/v1/bootstrap")["todos"])

    def rows(self, text=None):
        result = {}
        for t in json.loads(self.read() if text is None else text):
            status = {"completed": "done", "dismissed": "dismissed"}.get(t["state"]) or (
                "snoozed" if t.get("snoozed_until") else "open")
            result[t.get("legacy_id") or t["id"]] = dict(
                id=t["id"], text=t["title"], status=status, until=t.get("snoozed_until") if status == "snoozed" else None,
                revision=t["revision"], signature=f"{t['id']}:{t['revision']}")
        return result

    def write(self, before, after):
        raise TodoError("ToDos/Tasks.md is managed by the Second Brain app after cutover")

    def operation(self, entity_id, base_revision, action, payload):
        op = {"operation_id": str(uuid.uuid4()), "entity_type": "todo", "entity_id": str(entity_id),
              "base_revision": base_revision, "action": action, "payload": payload}
        result = self.api("POST", "/v1/operations", {"operations": [op]})["results"][0]
        return result

    def change(self, eid, status, until=None, *, expected_signature=None):
        row = self.rows().get(eid)
        if row is None:
            raise TodoError("Task is absent from the Second Brain app; refusing to recreate a removed task")
        if expected_signature and row["signature"] != expected_signature:
            raise TodoError("Task changed while updating; reload before retrying")
        payload = {"state": STATES[status], "snoozed_until": until if status == "snoozed" else None}
        result = self.operation(row["id"], row["revision"], "set_state", payload)
        if result["outcome"] != "applied":
            raise TodoError("Task changed in the app; reload before retrying" if result["outcome"] == "conflict"
                            else f"The app refused this change ({result['error']})")
        return f"{row['id']}:{result['entity']['revision']}"

    def promote(self, tasks, state):
        """Selected intake tasks become app todos with fixed ids: repeating a sync, or a task
        that was imported and then removed, never creates a second copy."""
        rows = self.rows()
        seen = set(state.setdefault("todos_imported", []))
        added = 0
        for task in tasks:
            if task.router_id in rows or task.router_id in seen:
                continue
            if "\n" in task.text or "\r" in task.text:
                raise TodoError("Task intake must be a single Markdown line")
            result = self.operation(uuid.uuid5(LEGACY_NS, task.router_id), 0, "create",
                                    {"title": task.text, "legacy_id": task.router_id})
            if result["outcome"] == "applied":
                added += 1
            elif result["error"] != "already_exists":
                raise TodoError(f"The app refused this task ({result['error']})")
            seen.add(task.router_id)
        state["todos_imported"] = sorted(seen)
        self.reconcile(state)
        return added

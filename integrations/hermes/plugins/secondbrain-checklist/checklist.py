"""Validated source indexing, locked state transitions, and at-most-once delivery."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from .transport import AmbiguousSendError
from .todos import TodoFile, TodoError

_MARKER = re.compile(
    r"(?:%%\s*router:([^%\s]+)\s*%%|<!--\s*router:([^>\s]+)\s*-->)"
)
_BULLET = re.compile(r"^\s*[-*+]\s+(?:\[[ xX]\]\s*)?(.*?)\s*$")
_LEDGER = "secondbrain_checklist"
_VALID_STATUSES = {"done", "dismissed", "snoozed", "open"}
_MAX_MESSAGE_CHARS = 3500
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class ChecklistError(TodoError):
    pass


@dataclass(frozen=True)
class Task:
    router_id: str
    text: str
    source: str


@dataclass(frozen=True)
class CallbackResult:
    ok: bool
    message: str
    markup: Any = None


def _resolved_inside(vault: Path, path: Path, label: str) -> Path:
    """Resolve existing symlinks and reject any path escaping the vault."""
    try:
        resolved = path.resolve(strict=False)
        resolved.relative_to(vault)
    except (OSError, ValueError) as exc:
        raise ChecklistError(f"{label} escapes the configured vault") from exc
    return resolved


def _thread_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


class StateStore:
    def __init__(self, vault: Path):
        self.vault = vault
        self.path = vault / "System" / "Reminders" / "state.json"
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def _check_paths(self) -> None:
        _resolved_inside(self.vault, self.path.parent, "Reminder state directory")
        _resolved_inside(self.vault, self.path, "Reminder state file")
        _resolved_inside(self.vault, self.lock_path, "Reminder lock file")

    @contextlib.contextmanager
    def locked(self):
        self._check_paths()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._check_paths()
        with _thread_lock(self.lock_path):
            with self.lock_path.open("a+b") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def read(self) -> dict[str, Any]:
        self._check_paths()
        if not self.path.exists():
            return {"version": 1, "items": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ChecklistError("Reminder state is unreadable; refusing to modify it") from exc
        if not isinstance(value, dict) or not isinstance(value.get("items", {}), dict):
            raise ChecklistError("Reminder state has an invalid shape; refusing to modify it")
        value.setdefault("items", {})
        for router_id, item in value["items"].items():
            if not isinstance(router_id, str) or not isinstance(item, dict):
                raise ChecklistError("Reminder state contains a malformed item record")
            status = item.get("status")
            until = item.get("until")
            revision = item.get("revision", 0)
            if status not in _VALID_STATUSES or not isinstance(revision, int) or revision < 0:
                raise ChecklistError("Reminder state contains an invalid status or revision")
            if status == "snoozed":
                try:
                    date.fromisoformat(until)
                except (TypeError, ValueError) as exc:
                    raise ChecklistError("Reminder state contains an invalid snooze date") from exc
            elif until is not None:
                raise ChecklistError("Reminder state contains an unexpected snooze date")
        return value

    def write(self, value: dict[str, Any]) -> None:
        self._check_paths()
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        fd, tmp_name = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                tmp.write(payload)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, self.path)
            dir_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_name)
            raise


def index_tasks(vault: Path, *, intake: bool = False) -> dict[str, list[Task]]:
    try:
        vault = vault.resolve(strict=True)
    except OSError as exc:
        raise ChecklistError("Configured vault is inaccessible") from exc
    todo = TodoFile(vault)
    if not intake and todo.read() is not None:
        return {eid: [Task(eid, row["text"], "ToDos/Tasks.md")] for eid, row in todo.rows().items()}
    decisions_dir = vault / "Decisions"
    tasks_path = vault / "Projects" / "_Tasks.md"
    _resolved_inside(vault, decisions_dir, "Decisions directory")
    _resolved_inside(vault, tasks_path, "Project task source")
    try:
        decisions = sorted(decisions_dir.glob("*.md"))
    except OSError as exc:
        raise ChecklistError("Decision sources are inaccessible") from exc
    paths = decisions + [tasks_path]
    if not vault.is_dir() or not any(path.is_file() for path in paths):
        raise ChecklistError("Configured vault or reminder sources are missing")
    result: dict[str, list[Task]] = {}
    for path in paths:
        if not path.is_file():
            continue
        _resolved_inside(vault, path, "Reminder source")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise ChecklistError("A reminder source is inaccessible") from exc
        source = path.relative_to(vault).as_posix()
        for line in lines:
            matches = list(_MARKER.finditer(line))
            if not matches:
                continue
            bullet = _BULLET.match(_MARKER.sub("", line))
            if not bullet:
                continue
            text = bullet.group(1).strip()
            if not text:
                continue
            for match in matches:
                router_id = match.group(1) or match.group(2)
                task = Task(router_id=router_id, text=text, source=source)
                result.setdefault(router_id, []).append(task)
    return result


def resolve_tasks(vault: Path, router_ids: Iterable[str], *, intake: bool = False) -> list[Task]:
    ids = list(router_ids)
    if any(not isinstance(item, str) or not item for item in ids):
        raise ChecklistError("Every router ID must be a non-empty string")
    if len(set(ids)) != len(ids):
        raise ChecklistError("Duplicate selected router IDs are not allowed")
    indexed = index_tasks(vault, intake=intake)
    selected: list[Task] = []
    for router_id in ids:
        matches = indexed.get(router_id, [])
        if not matches:
            raise ChecklistError(f"Unknown router ID: {router_id}")
        if len(matches) != 1:
            raise ChecklistError(f"Ambiguous router ID: {router_id}")
        selected.append(matches[0])
    return selected


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _status_for(state: dict[str, Any], router_id: str) -> str:
    item = state["items"].get(router_id)
    return item.get("status", "open") if isinstance(item, dict) else "open"


def _revision_for(state: dict[str, Any], router_id: str) -> int:
    item = state["items"].get(router_id)
    return int(item.get("revision", 0)) if isinstance(item, dict) else 0


def _button_rows(delivery: dict[str, Any], state: dict[str, Any], *, only_slot: int | None = None) -> list[list[dict[str, str]]]:
    rows = []
    token = delivery["token"]
    for slot, router_id in enumerate(delivery["router_ids"]):
        if only_slot is not None and slot != only_slot:
            continue
        done = _status_for(state, router_id) == "done"
        action, label = ("o", f"☑ Done {slot + 1} · Reopen") if done else ("d", f"☐ Done {slot + 1}")
        if only_slot is not None:
            label = "☑ Done · Reopen" if done else "☐ Done"
        data = f"sbr:{token}:{action}:{slot}:{_revision_for(state, router_id)}"
        if len(data.encode("utf-8")) >= 64:
            raise ChecklistError("Callback data exceeds Telegram's limit")
        rows.append([{"text": label, "callback_data": data}])
    return rows


class ChecklistService:
    def __init__(
        self,
        *,
        vault: str,
        chat_id: int,
        owner_user_id: int,
        timezone_name: str,
        sender: Any,
        markup_factory: Callable[[list[list[dict[str, str]]]], Any] = lambda rows: rows,
    ):
        self.vault = Path(vault).expanduser().resolve()
        self.chat_id = int(chat_id)
        self.owner_user_id = int(owner_user_id)
        self.timezone = ZoneInfo(timezone_name)
        self.sender = sender
        self.markup_factory = markup_factory
        self.store = StateStore(self.vault)
        self.todos = TodoFile(self.vault)

    def _read_state(self):
        state = self.store.read()
        if "todos_imported" in state and self.todos.read() is None:
            raise ChecklistError("ToDos/Tasks.md is missing; restore it before updating tasks")
        return self.todos.reconcile(state)

    def sync(self, router_ids: list[str]) -> dict[str, Any]:
        if not self.vault.is_dir():
            raise ChecklistError("Configured vault is inaccessible")
        with self.store.locked():
            state = self._read_state()
            tasks = resolve_tasks(self.vault, router_ids, intake=True) if router_ids else []
            added = self.todos.promote(tasks, state)
            self.store.write(state)
            return {"success": True, "added": added, "path": "ToDos/Tasks.md",
                    "tasks": [{"router_id": eid, **{k: row[k] for k in ("text", "status", "until")}}
                              for eid, row in self.todos.rows().items()]}

    def _ledger(self, state: dict[str, Any]) -> dict[str, Any]:
        ledger = state.setdefault(_LEDGER, {"deliveries": {}})
        if not isinstance(ledger, dict) or not isinstance(ledger.get("deliveries"), dict):
            raise ChecklistError("Checklist delivery ledger is invalid; refusing to continue")
        return ledger

    def change(self, *, action: str, router_id: str, until: str | None = None) -> dict[str, Any]:
        task = resolve_tasks(self.vault, [router_id])[0]
        statuses = {"done": "done", "dismiss": "dismissed", "snooze": "snoozed", "reopen": "open"}
        if action not in statuses:
            raise ChecklistError("Unsupported reminder action")
        snooze_until = None
        if action == "snooze":
            try:
                parsed = date.fromisoformat(until or "")
            except ValueError as exc:
                raise ChecklistError("Snooze requires YYYY-MM-DD") from exc
            if parsed <= datetime.now(self.timezone).date():
                raise ChecklistError("Snooze date must be in the future")
            snooze_until = parsed.isoformat()
        with self.store.locked():
            # Revalidate while holding the writer lock; routed intake stays read-only.
            task = resolve_tasks(self.vault, [router_id])[0]
            state = self._read_state()
            previous = _status_for(state, router_id)
            item = dict(state["items"].get(router_id, {}))
            item.update(
                status=statuses[action], until=snooze_until, text=task.text,
                source=task.source, updated_at=_now(), revision=_revision_for(state, router_id) + 1,
            )
            signature = self.todos.change(router_id, statuses[action], snooze_until, expected_signature=item.get("todo_signature"))
            if signature:
                item["todo_signature"] = signature
            state["items"][router_id] = item
            self.store.write(state)
            confirmed = self.store.read()["items"].get(router_id, {}).get("status") == statuses[action]
        if not confirmed:
            raise ChecklistError("Saved reminder state could not be confirmed")
        return {"success": True, "router_id": router_id, "status": statuses[action], "previous": previous}

    async def display(self, *, router_ids: list[str], request_id: str) -> dict[str, Any]:
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 160:
            raise ChecklistError("A non-empty caller request_id of at most 160 characters is required")
        selected = resolve_tasks(self.vault, router_ids)
        if not selected:
            return {"success": True, "sent": False, "confirmed": False, "status": "empty", "reply_instruction": "For a direct user question, say no open tasks are due today. Never use [SILENT] for an interactive question."}
        token = secrets.token_urlsafe(12)
        with self.store.locked():
            state = self._read_state()
            deliveries = self._ledger(state)["deliveries"]
            existing = deliveries.get(request_id)
            if isinstance(existing, dict):
                return {
                    "success": existing.get("status") == "confirmed",
                    "sent": False,
                    "confirmed": existing.get("status") == "confirmed",
                    "status": existing.get("status", "unknown"),
                    "duplicate": True,
                    "message_ids": existing.get("message_ids", {}),
                    "reply_instruction": "For a direct user question give a normal acknowledgement if confirmed, otherwise explain the incomplete delivery. Never use [SILENT] for an interactive question or retry this reservation.",
                }
            selected = resolve_tasks(self.vault, router_ids)
            today = datetime.now(self.timezone).date()
            tasks = []
            for task in selected:
                item = state["items"].get(task.router_id, {})
                status = item.get("status", "open") if isinstance(item, dict) else "open"
                if status in {"done", "dismissed"}:
                    continue
                if status == "snoozed" and date.fromisoformat(item["until"]) > today:
                    continue
                tasks.append(task)
            tasks = tasks[:3]
            if not tasks:
                return {"success": True, "sent": False, "confirmed": False, "status": "empty", "reply_instruction": "For a direct user question, say no open tasks are due today. Never use [SILENT] for an interactive question."}
            self.sender.preflight()
            delivery = {
                "status": "reserved",
                "token": token,
                "chat_id": self.chat_id,
                "owner_user_id": self.owner_user_id,
                "router_ids": [task.router_id for task in tasks],
                "snapshots": [{"text": task.text, "source": task.source} for task in tasks],
                "created_at": _now(),
                "message_ids": {},
            }
            deliveries[request_id] = delivery
            self.store.write(state)
        # Reserve the whole selection first. Persist each receipt separately so a
        # partial send never causes already-sent cards to be sent again on retry.
        for slot, task in enumerate(tasks):
            with self.store.locked():
                state = self._read_state()
                current = self._ledger(state)["deliveries"][request_id]
                markup = self.markup_factory(_button_rows(current, state, only_slot=slot))
            try:
                text = f"{slot + 1}. {task.text}"
                if len(text) > _MAX_MESSAGE_CHARS:
                    text = text[:_MAX_MESSAGE_CHARS - 1] + "…"
                receipt = await self.sender.send(chat_id=self.chat_id, text=text, reply_markup=markup)
                if int(receipt.chat_id) != self.chat_id:
                    raise AmbiguousSendError("Telegram receipt did not match the configured chat")
            except Exception:
                with self.store.locked():
                    state = self._read_state()
                    current = self._ledger(state)["deliveries"].get(request_id)
                    if isinstance(current, dict) and current.get("token") == token:
                        current["status"] = "ambiguous"
                        current["updated_at"] = _now()
                        self.store.write(state)
                return {"success": False, "sent": False, "confirmed": False,
                        "status": "ambiguous", "confirmed_count": slot,
                        "reply_instruction": "Tell the user delivery was incomplete or uncertain; do not resend automatically or output a silence marker."}
            with self.store.locked():
                state = self._read_state()
                current = self._ledger(state)["deliveries"].get(request_id)
                if not isinstance(current, dict) or current.get("token") != token:
                    raise ChecklistError("Delivery reservation changed before confirmation")
                current["message_ids"][str(slot)] = int(receipt.message_id)
                current.update(status="confirmed" if slot == len(tasks) - 1 else "partial", updated_at=_now())
                self.store.write(state)
        return {
            "success": True, "sent": True, "confirmed": True, "status": "confirmed",
            "message_ids": list(current["message_ids"].values()),
            "reply_instruction": "For a direct user question, reply: Each task is posted above with its own Done button. Never use [SILENT] for an interactive question. Only a scheduled morning job may return [SILENT].",
        }

    def callback(
        self, *, data: str, user_id: int, chat_id: int, message_id: int
    ) -> CallbackResult:
        match = re.fullmatch(r"sbr:([A-Za-z0-9_-]{16}):([do]):([0-9]{1,3}):([0-9]{1,10})", data or "")
        if not match:
            return CallbackResult(False, "Invalid or stale checklist button")
        token, action_code, slot_text, expected_revision_text = match.groups()
        if int(user_id) != self.owner_user_id or int(chat_id) != self.chat_id:
            return CallbackResult(False, "This checklist button is not authorized")
        with self.store.locked():
            state = self._read_state()
            deliveries = self._ledger(state)["deliveries"]
            delivery = next(
                (item for item in deliveries.values() if isinstance(item, dict) and secrets.compare_digest(str(item.get("token", "")), token)),
                None,
            )
            if (
                not delivery
                or delivery.get("status") not in {"confirmed", "partial", "ambiguous", "reserved"}
                or int(delivery.get("chat_id", 0)) != self.chat_id
                or int(delivery.get("owner_user_id", 0)) != self.owner_user_id
            ):
                return CallbackResult(False, "Invalid or stale checklist button")
            slot = int(slot_text)
            ids = delivery.get("router_ids", [])
            if slot >= len(ids):
                return CallbackResult(False, "Invalid or stale checklist button")
            message_ids = delivery.get("message_ids")
            if message_ids is not None:
                bound_message = message_ids.get(str(slot)) if isinstance(message_ids, dict) else None
            else:
                bound_message = delivery.get("message_id") if delivery.get("status") == "confirmed" else None
            if bound_message is None or int(bound_message) != int(message_id):
                return CallbackResult(False, "Invalid or stale checklist button")
            router_id = ids[slot]
            task = resolve_tasks(self.vault, [router_id])[0]
            snapshots = delivery.get("snapshots", [])
            if slot >= len(snapshots) or snapshots[slot] != {"text": task.text, "source": task.source}:
                return CallbackResult(False, "Task source changed; review it before updating")
            desired = "done" if action_code == "d" else "open"
            previous = _status_for(state, router_id)
            current_revision = _revision_for(state, router_id)
            if previous != desired and int(expected_revision_text) != current_revision:
                return CallbackResult(False, "This button is stale; use the current task state")
            if previous != desired:
                item = dict(state["items"].get(router_id, {}))
                item.update(
                    status=desired, until=None, text=task.text, source=task.source,
                    updated_at=_now(), revision=current_revision + 1,
                )
                signature = self.todos.change(router_id, desired, expected_signature=item.get("todo_signature"))
                if signature:
                    item["todo_signature"] = signature
                state["items"][router_id] = item
                self.store.write(state)
                state = self._read_state()
            markup = self.markup_factory(_button_rows(delivery, state, only_slot=slot if message_ids is not None else None))
        label = "Done" if desired == "done" else "Reopened"
        suffix = " (already recorded)" if previous == desired else ""
        return CallbackResult(True, label + suffix, markup)

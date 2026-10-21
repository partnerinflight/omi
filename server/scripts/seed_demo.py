"""Fill a local sync server with synthetic data so the phone app has something to show.

    SB_DATABASE_URL=postgresql://... PYTHONPATH=. uv run python scripts/seed_demo.py [--url http://127.0.0.1:8000] [--follow]

Plays the Windows worker (a producer named "Demo PC (synthetic)"): conversations, extracted
items (which become Inbox memories), people, speakers with spoken clips, clarifications and a
status report. Adds todos through a demo device. With --follow it keeps reporting every
minute and applies the phone's speaker commands the way Windows would.

Every name, sentence and clip here is invented. Never point this at a real server: it
writes to whatever database SB_DATABASE_URL names. Re-running is idempotent (same ids and
hashes; tokens are kept in ~/.cache/second-brain-demo/).
"""
import argparse
import hashlib
import json
import secrets
import shutil
import subprocess
import tempfile
import time
import urllib.request
import uuid
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sb_server import auth
from sb_server.db import connect

STATE = Path.home() / ".cache" / "second-brain-demo" / "tokens.json"
WORKER = "Demo PC (synthetic)"
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def ago(**kw) -> str:
    return (NOW - timedelta(**kw)).isoformat()


# --- Synthetic content -------------------------------------------------------------------

SOURCES = {
    "demo-job-1": {"kind": "omi", "recorded_at": ago(hours=3), "windows": [
        {"id": "w1", "start": 12.0, "end": 96.0, "excerpt": "So we agreed the kitchen quote from Alder & Pine is too high, "
         "let's get a second one before the end of the month.", "conversation_type": "planning"},
        {"id": "w2", "start": 140.0, "end": 210.0, "excerpt": "Priya mentioned she's moving to the Lisbon office in March "
         "and wants to keep the Thursday calls.", "conversation_type": "catch_up"}]},
    "demo-job-2": {"kind": "meeting", "recorded_at": ago(days=1, hours=2), "windows": [
        {"id": "w1", "start": 30.0, "end": 180.0, "excerpt": "Decision: we ship the onboarding redesign behind a flag on the "
         "fourteenth and measure activation for two weeks.", "conversation_type": "meeting"},
        {"id": "w2", "start": 300.0, "end": 420.0, "excerpt": "What if the weekly digest grouped notes by person instead of "
         "by day? That's how I actually remember things.", "conversation_type": "meeting"}]},
    "demo-job-3": {"kind": "omi", "recorded_at": ago(days=3), "windows": [
        {"id": "w1", "start": 5.0, "end": 64.0, "excerpt": "Marcus is allergic to shellfish, so not the seafood place for "
         "his birthday dinner.", "conversation_type": "social"}]},
}

ITEMS = [
    ("demo-item-1", "decision", "Get a second kitchen quote before the end of the month.", "demo-job-1-w1", {"confidence": 0.86}),
    ("demo-item-2", "fact", "Priya is moving to the Lisbon office in March.", "demo-job-1-w2",
     {"entity_type": "person", "entity_name": "Priya Raman", "confidence": 0.91}),
    ("demo-item-3", "decision", "Ship the onboarding redesign behind a flag on the 14th and measure activation for two weeks.",
     "demo-job-2-w1", {"confidence": 0.94}),
    ("demo-item-4", "idea", "Group the weekly digest by person instead of by day.", "demo-job-2-w2", {"confidence": 0.72}),
    ("demo-item-5", "fact", "Marcus is allergic to shellfish.", "demo-job-3-w1",
     {"entity_type": "person", "entity_name": "Marcus Webb", "confidence": 0.97}),
    ("demo-item-6", "fact", "Priya wants to keep the Thursday calls after moving.", "demo-job-1-w2",
     {"entity_type": "person", "entity_name": "Priya Raman", "confidence": 0.64}),
]

PEOPLE = {"demo-person-priya": "Priya Raman", "demo-person-marcus": "Marcus Webb", "demo-person-jo": "Jo Okafor"}

OBSERVATIONS = {
    # An inferred voice match the phone can confirm or reject.
    "demo-obs-1": {"source_id": "demo-job-1", "label": "c0000:S01", "display": "Speaker 2", "recorded_at": ago(hours=3),
                   "person_id": "demo-person-priya", "provenance": "inferred", "score": 0.81, "published": 1,
                   "clips": [0, 1], "say": ["I'm moving to the Lisbon office in March.", "Let's keep the Thursday calls."]},
    # Unknown voices to name.
    "demo-obs-2": {"source_id": "demo-job-2", "label": "c0001:S00", "display": "Speaker 1", "recorded_at": ago(days=1, hours=2),
                   "person_id": None, "provenance": None, "score": None, "published": 1,
                   "clips": [0], "say": ["We ship behind a flag on the fourteenth."]},
    "demo-obs-3": {"source_id": "demo-job-3", "label": "c0000:S02", "display": "Speaker 3", "recorded_at": ago(days=3),
                   "person_id": None, "provenance": None, "score": None, "published": 1,
                   "clips": [0, 1], "say": ["Not the seafood place, please.", "Shellfish and I do not get along."]},
}

CLARIFICATIONS = {
    "demo-clar-1": {"note": "Decisions/demo.md", "text": "Ship the onboarding redesign behind a flag on the 14th.",
                    "questions": ["Which month is “the 14th”?"], "context": SOURCES["demo-job-2"]["windows"][0]["excerpt"],
                    "state": "pending", "answer": None},
}

TODOS = ["Get a second quote for the kitchen", "Book Marcus's birthday dinner (no seafood)",
         "Send Priya the Lisbon relocation checklist", "Draft the onboarding experiment readout"]


# --- Tokens ------------------------------------------------------------------------------

def tokens() -> dict:
    saved = json.loads(STATE.read_text()) if STATE.exists() else {}
    with connect() as conn:
        def live(table, column, value):
            return value and conn.execute(f"SELECT 1 FROM {table} WHERE {column} = %s", (auth.digest(value),)).fetchone()
        if not live("producers", "token_hash", saved.get("producer")):
            saved["producer"] = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO producers (id, name, token_hash) VALUES (%s, %s, %s)",
                         (uuid.uuid4(), WORKER, auth.digest(saved["producer"])))
        if not live("tokens", "hash", saved.get("device")):  # a demo device that adds the todos
            saved["device"], device_id = secrets.token_urlsafe(32), uuid.uuid4()
            conn.execute("INSERT INTO devices (id, name) VALUES (%s, %s)", (device_id, "Demo seeder (synthetic)"))
            conn.execute("INSERT INTO tokens (hash, device_id, kind, expires_at) VALUES (%s, %s, 'access', now() + interval '1 year')",
                         (auth.digest(saved["device"]), device_id))
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(saved))
    STATE.chmod(0o600)
    return saved


def call(url: str, token: str, method: str, path: str, value=None, body: bytes | None = None, headers: dict | None = None):
    if value is not None:
        body, headers = json.dumps(value).encode(), {"Content-Type": "application/json"}
    req = urllib.request.Request(url + path, data=body, method=method,
                                 headers={"Authorization": f"Bearer {token}", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


# --- Records -----------------------------------------------------------------------------

def record(kind: str, rid: str, data: dict) -> dict:
    return {"kind": kind, "id": rid, "hash": hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:32], "data": data}


def records(people: dict, observations: dict) -> list[dict]:
    out = [record("source", k, {**v, "source_sha256": "0" * 64, "window_count": len(v["windows"])}) for k, v in SOURCES.items()]
    for rid, kind, text, source_id, extra in ITEMS:
        job = source_id.rsplit("-", 1)[0]
        out.append(record("item", rid, {"kind": kind, "text": text, "source_id": source_id, "source_hash": "0" * 64,
                                        "note": f"Demo/{kind}.md", "observed_at": SOURCES[job]["recorded_at"], **extra}))
    out += [record("person", k, {"name": v}) for k, v in people.items()]
    out += [record("observation", k, {key: v[key] for key in v if key != "say"} | {"clip_count": len(v["clips"])})
            for k, v in observations.items()]
    out += [record("clarification", k, v) for k, v in CLARIFICATIONS.items()]
    return out


def status(pending: int = 1) -> dict:
    return {"jobs": {"pending": pending, "failed": 0, "completed": len(SOURCES)}, "export_conflicts": 0}


def clip(text: str) -> bytes:
    """A spoken WAV via macOS `say` when available, else a short tone. Either way synthetic."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "clip.wav"
        if shutil.which("say"):
            subprocess.run(["say", "-o", str(out), "--file-format=WAVE", "--data-format=LEI16@16000", text], check=True)
        else:
            import math
            with wave.open(str(out), "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
                w.writeframes(b"".join(int(8000 * math.sin(i / 16000 * 2 * math.pi * 220)).to_bytes(2, "little", signed=True)
                                       for i in range(16000)))
        return out.read_bytes()


def upload_clips(url: str, token: str, observations: dict):
    for rid, o in observations.items():
        if o["provenance"] == "confirmed":
            continue
        for n, text in zip(o["clips"], o["say"]):
            data = clip(text)
            call(url, token, "PUT", f"/v1/worker/clips/{rid}/{n}", body=data,
                 headers={"Content-Type": "audio/wav", "X-Content-SHA256": hashlib.sha256(data).hexdigest()})


def add_todos(url: str, token: str):
    ops = [{"operation_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"demo-op:{t}")), "entity_type": "todo",
            "entity_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"demo-todo:{t}")), "base_revision": 0,
            "action": "create", "payload": {"title": t}} for t in TODOS]
    call(url, token, "POST", "/v1/operations", {"operations": ops})  # replays are receipts, not duplicates


# --- Simulated Windows worker --------------------------------------------------------------

def apply(command: dict, people: dict, observations: dict) -> None:
    """The subset of Windows' speaker and clarification commands the demo needs."""
    p, action = command["payload"], command["payload"]["action"]
    if action == "assign":
        person = p.get("person") or next((k for k, v in people.items() if v.lower() == p["name"].lower()), None)
        if not person:
            person = f"demo-person-{uuid.uuid4().hex[:8]}"
            people[person] = p["name"]
        observations[p["observation"]] |= {"person_id": person, "provenance": "confirmed"}
    elif action == "clear":
        observations[p["observation"]] |= {"person_id": None, "provenance": "cleared"}
    elif action == "discard":
        observations[p["observation"]] |= {"clips": []}
    elif action == "rename":
        people[p["person"]] = p["name"]
    elif action == "forget":
        people.pop(p["person"], None)
    elif action in ("clarify", "clarify_dismiss"):
        CLARIFICATIONS[p["item"]] |= {"state": "answered" if action == "clarify" else "dismissed", "answer": p.get("answer")}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--follow", action="store_true", help="keep reporting and apply phone commands")
    args = parser.parse_args()
    if not args.url.startswith(("http://127.0.0.1", "http://localhost")):
        raise SystemExit("seed_demo only targets a local server")
    t = tokens()
    people, observations = dict(PEOPLE), json.loads(json.dumps(OBSERVATIONS))
    result = call(args.url, t["producer"], "POST", "/v1/worker/events", {"records": records(people, observations), "status": status()})
    upload_clips(args.url, t["producer"], observations)
    add_todos(args.url, t["device"])
    print(f"seeded: {result['accepted']} records, {len(TODOS)} todos, clips for {len(observations)} speakers")
    while args.follow:
        time.sleep(5)
        commands = call(args.url, t["producer"], "GET", "/v1/worker/commands")["commands"]
        for c in commands:
            ok = True
            try:
                apply(c, people, observations)
            except KeyError:
                ok = False
            call(args.url, t["producer"], "POST", f"/v1/worker/commands/{c['id']}/result",
                 {"ok": ok, **({} if ok else {"error": "unknown target"})})
            print(f"applied {c['payload']['action']}" if ok else f"failed {c['payload']['action']}")
        call(args.url, t["producer"], "POST", "/v1/worker/events", {"records": records(people, observations), "status": status(0)})


if __name__ == "__main__":
    main()

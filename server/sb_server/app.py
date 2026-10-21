"""API v1, todo slice. Contract: docs/mobile/architecture-and-sync.md."""
import hashlib
import json
import uuid
from typing import Annotated, Literal

import re
from datetime import date

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from psycopg.types.json import Jsonb
from psycopg_pool import PoolTimeout
from pydantic import BaseModel, Field, field_validator

from . import auth
from .limits import BodyLimit
from .db import pool

# No interactive docs or schema on the public host.
app = FastAPI(title="Second Brain", version="1", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(BodyLimit)
pairing_failures = auth.FailureLimiter()

MAX_BATCH = 100
MAX_PAGE = 500


def db():
    try:
        conn = pool().getconn()
    except PoolTimeout:
        raise HTTPException(503, "busy", headers={"Retry-After": "5"}) from None
    try:
        yield conn
    finally:
        pool().putconn(conn)


Db = Annotated[object, Depends(db)]


def device(conn: Db, authorization: Annotated[str | None, Header()] = None) -> uuid.UUID:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "missing_token")
    row = conn.execute(
        """SELECT t.device_id FROM tokens t JOIN devices d ON d.id = t.device_id
           WHERE t.hash = %s AND t.kind = 'access' AND t.expires_at > now() AND d.revoked_at IS NULL""",
        (auth.digest(token),),
    ).fetchone()
    if not row:
        raise HTTPException(401, "invalid_token")
    return row["device_id"]


Device = Annotated[uuid.UUID, Depends(device)]


@app.get("/v1/health")
def health(conn: Db):
    conn.execute("SELECT 1")
    return {"ok": True}


class PairRequest(BaseModel):
    code: str = Field(max_length=64)


@app.post("/v1/pair")
def pair(body: PairRequest, conn: Db, request: Request):
    client = request.client.host if request.client else "unknown"  # real IP: forwarded only from Traefik
    slot = pairing_failures.reserve(client)
    if slot is None:
        raise HTTPException(429, "too_many_attempts", headers={"Retry-After": "600"})
    with conn.transaction():
        row = conn.execute(
            """UPDATE pairing_codes SET used_at = now()
               WHERE code_hash = %s AND used_at IS NULL AND expires_at > now() RETURNING device_name""",
            (auth.digest(body.code.strip().upper()),),
        ).fetchone()
        if not row:
            raise HTTPException(401, "invalid_code")  # the reserved slot stays: a failure
        pairing_failures.release(client, slot)
        device_id = uuid.uuid4()
        conn.execute("INSERT INTO devices (id, name) VALUES (%s, %s)", (device_id, row["device_name"]))
        return auth.issue_tokens(conn, device_id)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(max_length=256)


@app.post("/v1/auth/refresh")
def refresh(body: RefreshRequest, conn: Db):
    token_hash, issued = auth.digest(body.refresh_token), None
    with conn.transaction():
        row = conn.execute(
            """SELECT t.device_id, t.used_at, t.expires_at > now() AS live, d.revoked_at
               FROM tokens t JOIN devices d ON d.id = t.device_id
               WHERE t.hash = %s AND t.kind = 'refresh' FOR UPDATE OF t""",
            (token_hash,),
        ).fetchone()
        if row and row["used_at"] is not None and row["revoked_at"] is None:
            # A rotated token came back: it may be stolen. Revoke the device.
            conn.execute("UPDATE devices SET revoked_at = now() WHERE id = %s", (row["device_id"],))
        elif row and row["live"] and row["used_at"] is None and row["revoked_at"] is None:
            conn.execute("UPDATE tokens SET used_at = now() WHERE hash = %s", (token_hash,))
            issued = auth.issue_tokens(conn, row["device_id"])
    if not issued:
        raise HTTPException(401, "invalid_refresh_token")
    return issued


@app.post("/v1/auth/revoke", status_code=204)
def revoke(device_id: Device, conn: Db):
    conn.execute("UPDATE devices SET revoked_at = now() WHERE id = %s", (device_id,))


LEGACY_NS = uuid.UUID("5b0c8d1e-3f7a-4d2b-9c61-0e4a7b2f9d13")  # fixed forever: maps legacy ids to todo ids


def legacy_todo_id(legacy_id: str) -> uuid.UUID:
    return uuid.uuid5(LEGACY_NS, legacy_id)


def todo_json(row) -> dict:
    return {"id": str(row["id"]), "title": row["title"], "state": row["state"], "revision": row["revision"],
            "legacy_id": row["legacy_id"], "notes": row["notes"],
            "snoozed_until": row["snoozed_until"].isoformat() if row["snoozed_until"] else None,
            "updated_at": row["updated_at"].isoformat(),
            "deleted_at": row["deleted_at"].isoformat() if row["deleted_at"] else None}


def cursor(epoch, seq) -> str:
    return f"{epoch}:{seq}"


@app.get("/v1/bootstrap")
def bootstrap(_: Device, conn: Db):
    # ponytail: one page holding every live todo; page with a snapshot id once lists grow large.
    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        meta = conn.execute("SELECT epoch, seq FROM meta").fetchone()
        todos = conn.execute("SELECT * FROM todos WHERE deleted_at IS NULL ORDER BY id").fetchall()
        # ponytail: recent and unresolved commands only; the phone needs them for pending/failed badges.
        commands = conn.execute("""SELECT * FROM commands WHERE state = 'pending_worker'
                                   OR updated_at > now() - interval '30 days' ORDER BY position""").fetchall()
        memories = conn.execute("SELECT * FROM memories WHERE deleted_at IS NULL ORDER BY id").fetchall()
    return {"cursor": cursor(meta["epoch"], meta["seq"]), "todos": [todo_json(t) for t in todos],
            "commands": [command_json(c) for c in commands], "memories": [memory_json(m) for m in memories]}


@app.get("/v1/changes")
def changes(_: Device, conn: Db, cursor_: Annotated[str, Query(alias="cursor")], limit: int = Query(200, ge=1, le=MAX_PAGE)):
    epoch, _, seq = cursor_.partition(":")
    meta = conn.execute("SELECT epoch FROM meta").fetchone()
    if epoch != str(meta["epoch"]) or not seq.isdigit():
        raise HTTPException(410, "cursor_expired")  # different server epoch: bootstrap again
    rows = conn.execute(
        "SELECT seq, entity_type, data FROM change_log WHERE seq > %s ORDER BY seq LIMIT %s", (int(seq), limit + 1)
    ).fetchall()
    page = rows[:limit]
    last = page[-1]["seq"] if page else int(seq)
    return {"changes": [{"seq": r["seq"], "entity_type": r["entity_type"], "entity": r["data"]} for r in page],
            "cursor": cursor(meta["epoch"], last), "has_more": len(rows) > limit}


class Operation(BaseModel):
    operation_id: uuid.UUID
    entity_type: Literal["todo", "command", "memory"]
    entity_id: str = Field(pattern=r"^[A-Za-z0-9:_-]{1,128}$")  # a UUID except for memories
    base_revision: int = Field(ge=0)
    action: Literal["create", "set_title", "set_state", "set_text"]
    payload: dict

    @field_validator("payload")
    @classmethod
    def small(cls, v):
        if len(json.dumps(v)) > 10_000:
            raise ValueError("payload too large")
        return v


class OperationBatch(BaseModel):
    operations: list[Operation] = Field(max_length=MAX_BATCH)


def valid_date(v) -> bool:
    try:
        return isinstance(v, str) and len(v) == 10 and bool(date.fromisoformat(v))
    except ValueError:
        return False


def validate_payload(op: Operation) -> str | None:
    p = op.payload
    if op.action in ("create", "set_title"):
        title = p.get("title")
        # A create may carry the legacy id of a promoted router task (Hermes intake sync).
        allowed = ({"title"}, {"title", "legacy_id"}) if op.action == "create" else ({"title"},)
        if set(p) not in allowed or not isinstance(title, str) or not title.strip() or len(title) > 1000:
            return "invalid_title"
        if "legacy_id" in p and not (isinstance(p["legacy_id"], str) and RECORD_ID.match(p["legacy_id"])
                                     and op.entity_id == str(legacy_todo_id(p["legacy_id"]))):
            return "invalid_legacy_id"
    elif set(p) not in ({"state"}, {"state", "snoozed_until"}) or p.get("state") not in ("open", "completed", "dismissed"):
        return "invalid_state"
    elif p.get("snoozed_until") is not None and not (p["state"] == "open" and valid_date(p["snoozed_until"])):
        return "invalid_snooze"
    return None


def todo_authority(conn) -> dict:
    return conn.execute("SELECT * FROM authority WHERE domain = 'todos'").fetchone()


RECORD_ID = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")
# action -> (required, optional) payload fields besides "action". Applied by the worker through
# the same local commands as the tray (second_brain.speakers / clarifications).
COMMANDS = {
    "assign": ({"observation"}, {"person", "name", "base_hash"}),  # exactly one of person/name
    "clear": ({"observation"}, {"base_hash"}),
    "discard": ({"observation"}, {"base_hash"}),
    "rename": ({"person", "name"}, {"base_hash"}),
    "forget": ({"person"}, {"base_hash"}),
    "clarify": ({"item", "answer"}, set()),
    "clarify_dismiss": ({"item"}, set()),
}


def validate_command(p: dict) -> str | None:
    spec = COMMANDS.get(p.get("action"))
    if not spec:
        return "invalid_action"
    required, optional = spec
    fields = set(p) - {"action"}
    if not required <= fields or fields - required - optional:
        return "invalid_payload"
    if p["action"] == "assign" and ("person" in p) == ("name" in p):
        return "invalid_payload"
    for key in ("observation", "person", "item"):
        if key in p and not (isinstance(p[key], str) and RECORD_ID.match(p[key])):
            return "invalid_payload"
    if "base_hash" in p and not (isinstance(p["base_hash"], str) and 0 < len(p["base_hash"]) <= 128):
        return "invalid_payload"
    for key, limit in (("name", 80), ("answer", 4000)):
        v = p.get(key)
        if key in p and not (isinstance(v, str) and 1 <= len(v.strip()) <= limit and not any(ord(c) < 32 for c in v)):
            return f"invalid_{key}"
    return None


def memory_json(row) -> dict:
    o = row["original"]
    return {"id": row["id"], "kind": row["kind"], "text": row["text"], "state": row["state"],
            "original_text": o.get("text"), "confidence": o.get("confidence"),
            "entity_type": o.get("entity_type"), "entity_name": o.get("entity_name"),
            "observed_at": o.get("observed_at"), "evidence": row["evidence"], "revision": row["revision"],
            "updated_at": row["updated_at"].isoformat(),
            "deleted_at": row["deleted_at"].isoformat() if row["deleted_at"] else None}


def clean_text(v, limit=2000) -> str | None:
    """One line of plain text that the vault projection can write without escaping."""
    if not isinstance(v, str) or not 1 <= len(v.strip()) <= limit or any(ord(c) < 32 for c in v):
        return None
    if "%%" in v or "<!--" in v:  # Obsidian comment and router marker syntax
        return None
    return v.strip()


def apply_memory(conn, op: Operation, done, meta) -> dict:
    current = conn.execute("SELECT * FROM memories WHERE id = %s FOR UPDATE", (op.entity_id,)).fetchone()
    entity = memory_json(current) if current else None
    p = op.payload
    if op.action == "set_text":
        value = clean_text(p.get("text")) if set(p) == {"text"} else None
        if value is None:
            return done("rejected", entity, "invalid_text")
        column = "text"
    elif op.action == "set_state" and set(p) == {"state"} and p["state"] in ("proposed", "confirmed", "archived"):
        value, column = p["state"], "state"
    else:
        return done("rejected", entity, "invalid_state")  # memories come from the worker; no create
    if not current or current["deleted_at"]:
        return done("rejected", None, "not_found")
    if current["revision"] != op.base_revision:
        return done("conflict", entity, "stale_revision")
    row = conn.execute(f"UPDATE memories SET {column} = %s, revision = revision + 1, updated_at = now() "
                       "WHERE id = %s RETURNING *", (value, op.entity_id)).fetchone()
    entity = memory_json(row)
    log_change(conn, meta, "memory", entity)
    return done("applied", entity)


def import_memory(conn, meta, producer_id, r) -> None:
    """A new extracted fact/decision/idea becomes a proposed memory. Existing ones are never
    updated, so a replay cannot undo an edit, an archive or a confirmation."""
    if r.kind != "item" or not r.data or r.data.get("kind") not in ("fact", "decision", "idea"):
        return
    text = " ".join(str(r.data.get("text", "")).split()).replace("%%", "%").replace("<!--", "<!-")[:2000]
    if not text:
        return
    job, _, window = str(r.data.get("source_id", "")).rpartition("-")
    source = conn.execute("SELECT data FROM worker_records WHERE producer_id = %s AND kind = 'source' AND id = %s",
                          (producer_id, job)).fetchone()
    w = next((w for w in (source["data"]["windows"] if source else []) if w["id"] == window), None)
    evidence = {"source_id": r.data.get("source_id"), "note": r.data.get("note"),
                "recorded_at": source["data"].get("recorded_at") if source else None,
                **({"excerpt": w["excerpt"], "start": w["start"], "end": w["end"]} if w else {})}
    row = conn.execute("""INSERT INTO memories (id, kind, text, state, original, evidence, revision)
                          VALUES (%s, %s, %s, 'proposed', %s, %s, 1) ON CONFLICT (id) DO NOTHING RETURNING *""",
                       (r.id, r.data["kind"], text, Jsonb(r.data), Jsonb(evidence))).fetchone()
    if row:
        log_change(conn, meta, "memory", memory_json(row))


def command_json(row) -> dict:
    return {"id": str(row["id"]), "payload": row["payload"], "state": row["state"], "error": row["error"],
            "revision": row["revision"], "updated_at": row["updated_at"].isoformat(), "deleted_at": None}


def log_change(conn, meta: dict, entity_type: str, entity: dict):
    meta["seq"] += 1
    conn.execute("UPDATE meta SET seq = %s", (meta["seq"],))
    conn.execute("INSERT INTO change_log (seq, entity_type, entity_id, revision, data) VALUES (%s, %s, %s, %s, %s)",
                 (meta["seq"], entity_type, entity["id"], entity["revision"], Jsonb(entity)))


def payload_hash(op: Operation) -> str:
    body = op.model_dump(mode="json", exclude={"operation_id"})
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def apply_one(conn, device_id: uuid.UUID, op: Operation) -> dict:
    h = payload_hash(op)
    with conn.transaction():
        # Serializes every writer, so log positions commit in order (invariant 4).
        meta = conn.execute("SELECT epoch, seq FROM meta FOR UPDATE").fetchone()
        prior = conn.execute("SELECT payload_hash, result FROM receipts WHERE operation_id = %s", (op.operation_id,)).fetchone()
        if prior:
            if prior["payload_hash"] == h:
                return prior["result"]
            return {"operation_id": str(op.operation_id), "outcome": "rejected", "error": "operation_id_reused", "entity": None}

        def done(outcome, entity=None, error=None):
            result = {"operation_id": str(op.operation_id), "outcome": outcome, "error": error, "entity": entity}
            conn.execute("INSERT INTO receipts (operation_id, device_id, payload_hash, result) VALUES (%s, %s, %s, %s)",
                         (op.operation_id, device_id, h, Jsonb(result)))
            return result

        if op.entity_type == "memory":
            return apply_memory(conn, op, done, meta)
        try:
            uuid.UUID(op.entity_id)
        except ValueError:
            return done("rejected", None, "invalid_entity_id")
        if op.entity_type == "command":
            existing = conn.execute("SELECT * FROM commands WHERE id = %s", (op.entity_id,)).fetchone()
            if existing:
                return done("conflict", command_json(existing), "already_exists")
            if op.action != "create" or op.base_revision != 0:
                return done("rejected", None, "invalid_command")
            if error := validate_command(op.payload):
                return done("rejected", None, error)
            row = conn.execute("""INSERT INTO commands (id, device_id, payload, state, revision)
                                  VALUES (%s, %s, %s, 'pending_worker', 1) RETURNING *""",
                               (op.entity_id, device_id, Jsonb(op.payload))).fetchone()
            entity = command_json(row)
            log_change(conn, meta, "command", entity)
            return done("applied", entity)

        current = conn.execute("SELECT * FROM todos WHERE id = %s FOR UPDATE", (op.entity_id,)).fetchone()

        if error := validate_payload(op):
            return done("rejected", todo_json(current) if current else None, error)
        legacy = (current and current["legacy_id"]) or "legacy_id" in op.payload
        if legacy and todo_authority(conn)["mode"] != "server":
            # Imported todos stay owned by ToDos/Tasks.md and Hermes until cutover.
            return done("rejected", todo_json(current) if current else None, "legacy_authority")
        if op.action == "create":
            if current:
                return done("conflict", todo_json(current), "already_exists")
            if op.base_revision != 0:
                return done("rejected", None, "create_base_revision_must_be_0")
            row = conn.execute("INSERT INTO todos (id, title, state, revision, legacy_id) VALUES (%s, %s, 'open', 1, %s) RETURNING *",
                               (op.entity_id, op.payload["title"].strip(), op.payload.get("legacy_id"))).fetchone()
        else:
            if not current or current["deleted_at"]:
                return done("rejected", None, "not_found")
            if current["revision"] != op.base_revision:
                return done("conflict", todo_json(current), "stale_revision")
            if op.action == "set_title":
                sql, values = "title = %s", (op.payload["title"].strip(),)
            else:  # any state change replaces the snooze; completing or reopening clears it
                sql, values = "state = %s, snoozed_until = %s", (op.payload["state"], op.payload.get("snoozed_until"))
            row = conn.execute(
                f"UPDATE todos SET {sql}, revision = revision + 1, updated_at = now() WHERE id = %s RETURNING *",
                (*values, op.entity_id),
            ).fetchone()

        entity = todo_json(row)
        log_change(conn, meta, "todo", entity)
        return done("applied", entity)


@app.post("/v1/operations")
def operations(body: OperationBatch, device_id: Device, conn: Db):
    # Each operation commits on its own: a conflict never discards the others' results.
    return {"results": [apply_one(conn, device_id, op) for op in body.operations]}


# --- Worker bridge (milestone 2, shadow import) ------------------------------------------------
# The Windows service connects out to these routes; the server never connects to Windows.

MAX_RECORD_BYTES = 64_000


def producer(conn: Db, authorization: Annotated[str | None, Header()] = None) -> uuid.UUID:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "missing_token")
    row = conn.execute("SELECT id FROM producers WHERE token_hash = %s AND revoked_at IS NULL",
                       (auth.digest(token),)).fetchone()
    if not row:
        raise HTTPException(401, "invalid_token")
    return row["id"]


Producer = Annotated[uuid.UUID, Depends(producer)]


class WorkerRecord(BaseModel):
    kind: Literal["source", "item", "person", "observation", "clarification"]
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9:_-]+$")
    hash: str = Field(min_length=1, max_length=128)
    data: dict | None = None  # None is a tombstone

    @field_validator("data")
    @classmethod
    def small(cls, v):
        if v is not None and len(json.dumps(v)) > MAX_RECORD_BYTES:
            raise ValueError("record too large")
        return v


class WorkerEvents(BaseModel):
    records: list[WorkerRecord] = Field(max_length=MAX_BATCH)
    status: dict = Field(default_factory=dict)

    @field_validator("status")
    @classmethod
    def counts_only(cls, v):
        if len(json.dumps(v)) > 4_000:
            raise ValueError("status too large")
        return v


@app.post("/v1/worker/events")
def worker_events(body: WorkerEvents, producer_id: Producer, conn: Db):
    # Idempotent by (producer, kind, id, hash): a replayed record is "unchanged". The worker
    # marks a record exported only after this commit returns.
    with conn.transaction():
        meta = conn.execute("SELECT epoch, seq FROM meta FOR UPDATE").fetchone()  # memory imports log changes
        stored = 0
        for r in body.records:
            stored += conn.execute(
                """INSERT INTO worker_records (producer_id, kind, id, hash, data, deleted_at)
                   VALUES (%s, %s, %s, %s, %s, CASE WHEN %s THEN now() END)
                   ON CONFLICT (producer_id, kind, id) DO UPDATE
                   SET hash = EXCLUDED.hash, data = COALESCE(EXCLUDED.data, worker_records.data),
                       deleted_at = EXCLUDED.deleted_at, updated_at = now()
                   WHERE worker_records.hash <> EXCLUDED.hash""",
                (producer_id, r.kind, r.id, r.hash, Jsonb(r.data) if r.data is not None else None, r.data is None),
            ).rowcount
            # Clips exist only to name a speaker: drop them once confirmed, discarded or forgotten.
            if r.kind == "observation" and (r.data is None or r.data.get("provenance") == "confirmed"
                                            or not r.data.get("clips")):
                conn.execute("DELETE FROM clips WHERE observation_id = %s", (r.id,))
            import_memory(conn, meta, producer_id, r)
        conn.execute("DELETE FROM clips WHERE created_at < now() - interval '30 days'")
        conn.execute("UPDATE producers SET last_seen_at = now(), status = %s WHERE id = %s",
                     (Jsonb(body.status), producer_id))
    # The epoch changes when the database is restored: the worker then resends everything.
    return {"accepted": len(body.records), "stored": stored, "epoch": str(meta["epoch"])}


@app.get("/v1/pipeline")
def pipeline(_: Device, conn: Db):
    """What the phone can inspect during shadow import: worker freshness and record counts."""
    producers = conn.execute(
        "SELECT name, last_seen_at, status FROM producers WHERE revoked_at IS NULL ORDER BY name").fetchall()
    counts = conn.execute(
        "SELECT kind, count(*) AS n FROM worker_records WHERE deleted_at IS NULL GROUP BY kind").fetchall()
    oldest = conn.execute("SELECT min(created_at) AS t FROM commands WHERE state = 'pending_worker'").fetchone()["t"]
    return {"workers": [{"name": p["name"], "status": p["status"],
                         "last_seen_at": p["last_seen_at"].isoformat() if p["last_seen_at"] else None} for p in producers],
            "counts": {c["kind"]: c["n"] for c in counts},
            "oldest_pending_command_at": oldest.isoformat() if oldest else None}


@app.get("/v1/pipeline/records")
def pipeline_records(_: Device, conn: Db, kind: Literal["source", "item", "person", "observation", "clarification"],
                     limit: int = Query(50, ge=1, le=MAX_PAGE)):
    rows = conn.execute(
        """SELECT id, hash, data, updated_at FROM worker_records WHERE kind = %s AND deleted_at IS NULL
           ORDER BY updated_at DESC, id LIMIT %s""", (kind, limit)).fetchall()
    return {"records": [{"id": r["id"], "hash": r["hash"], "data": r["data"], "updated_at": r["updated_at"].isoformat()}
                        for r in rows]}


MAX_CLIP_BYTES = 2_000_000


@app.put("/v1/worker/clips/{observation_id}/{n}", status_code=204)
async def worker_clip(observation_id: str, n: int, request: Request, producer_id: Producer, conn: Db,
                      x_content_sha256: Annotated[str, Header()]):
    if not RECORD_ID.match(observation_id) or not 0 <= n < 100:
        raise HTTPException(422, "invalid_clip")
    if int(request.headers.get("content-length") or 0) > MAX_CLIP_BYTES:
        raise HTTPException(413, "clip_too_large")
    body = await request.body()
    if len(body) > MAX_CLIP_BYTES:
        raise HTTPException(413, "clip_too_large")
    if hashlib.sha256(body).hexdigest() != x_content_sha256:
        raise HTTPException(422, "hash_mismatch")  # partial or corrupted upload is never stored
    # ponytail: sync DB call inside an async route; fine for one worker uploading small clips.
    if not conn.execute("""SELECT 1 FROM worker_records WHERE producer_id = %s AND kind = 'observation' AND id = %s
                           AND deleted_at IS NULL""", (producer_id, observation_id)).fetchone():
        raise HTTPException(404, "unknown_observation")
    conn.execute("""INSERT INTO clips (observation_id, n, sha256, bytes) VALUES (%s, %s, %s, %s)
                    ON CONFLICT (observation_id, n) DO UPDATE SET sha256 = EXCLUDED.sha256, bytes = EXCLUDED.bytes,
                    created_at = now()""", (observation_id, n, x_content_sha256, body))


@app.get("/v1/clips/{observation_id}/{n}")
def clip(observation_id: str, n: int, _: Device, conn: Db):
    # ponytail: whole-file response, no range requests; clips are a few hundred KB.
    row = conn.execute("SELECT sha256, bytes FROM clips WHERE observation_id = %s AND n = %s",
                       (observation_id, n)).fetchone()
    if not row:
        raise HTTPException(410, "clip_unavailable")
    return Response(bytes(row["bytes"]), media_type="audio/wav",
                    headers={"ETag": f'"{row["sha256"]}"', "Cache-Control": "private, no-store"})


@app.get("/v1/worker/commands")
def worker_commands(_: Producer, conn: Db, limit: int = Query(20, ge=1, le=100)):
    # ponytail: one producer applies every command (single voice-profile worker, per the design).
    rows = conn.execute("SELECT id, payload FROM commands WHERE state = 'pending_worker' ORDER BY position LIMIT %s",
                        (limit,)).fetchall()
    return {"commands": [{"id": str(r["id"]), "payload": r["payload"]} for r in rows]}


class CommandResult(BaseModel):
    ok: bool
    error: str | None = Field(None, max_length=500)


@app.post("/v1/worker/commands/{command_id}/result")
def worker_command_result(command_id: uuid.UUID, body: CommandResult, _: Producer, conn: Db):
    with conn.transaction():
        meta = conn.execute("SELECT epoch, seq FROM meta FOR UPDATE").fetchone()
        row = conn.execute("SELECT * FROM commands WHERE id = %s FOR UPDATE", (command_id,)).fetchone()
        if not row:
            raise HTTPException(404, "unknown_command")
        if row["state"] != "pending_worker":
            return command_json(row)  # a retried result after a lost response; the first one stands
        row = conn.execute("""UPDATE commands SET state = %s, error = %s, revision = revision + 1, updated_at = now()
                              WHERE id = %s RETURNING *""",
                           ("applied_worker" if body.ok else "failed_worker", None if body.ok else body.error or "failed",
                            command_id)).fetchone()
        entity = command_json(row)
        log_change(conn, meta, "command", entity)
        return entity


@app.get("/v1/worker/memories")
def worker_memories(_: Producer, conn: Db):
    """Confirmed memories for the worker's vault projection.
    ponytail: the full set every cycle; add a since-cursor once there are thousands."""
    rows = conn.execute("SELECT * FROM memories WHERE state = 'confirmed' AND deleted_at IS NULL ORDER BY id").fetchall()
    return {"memories": [memory_json(r) for r in rows]}



# --- Milestone 5: todo migration and authority -------------------------------------------------

class LegacyTodo(BaseModel):
    legacy_id: str = Field(pattern=r"^[A-Za-z0-9:_-]{1,128}$")
    title: str = Field(min_length=1, max_length=1000)
    state: Literal["open", "completed", "dismissed"]
    snoozed_until: str | None = None
    notes: str | None = Field(None, max_length=20_000)
    deleted: bool = False  # imported once, since removed from Tasks.md: a tombstone, never recreated

    @field_validator("snoozed_until")
    @classmethod
    def a_date(cls, v):
        if v is not None and not valid_date(v):
            raise ValueError("snoozed_until must be YYYY-MM-DD")
        return v


class TodoImport(BaseModel):
    migration_id: str = Field(pattern=r"^[A-Za-z0-9:_-]{1,64}$")
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    todos: list[LegacyTodo] = Field(max_length=5000)


@app.post("/v1/migration/todos")
def import_todos(body: TodoImport, _: Producer, conn: Db):
    """Idempotent: each legacy todo maps to one fixed id, unchanged rows are left alone, so a
    restart or a repeat import has no effect. Allowed only while Tasks.md is authoritative."""
    with conn.transaction():
        meta = conn.execute("SELECT epoch, seq FROM meta FOR UPDATE").fetchone()
        if conn.execute("SELECT mode FROM authority WHERE domain = 'todos' FOR UPDATE").fetchone()["mode"] != "legacy":
            raise HTTPException(409, "authority_switched")
        if len({t.legacy_id for t in body.todos}) != len(body.todos):
            raise HTTPException(422, "duplicate_legacy_id")
        counts = {"created": 0, "updated": 0, "unchanged": 0}
        for t in body.todos:
            todo_id = legacy_todo_id(t.legacy_id)
            # A todo made in the app carries its own id as its marker after a rollback: same todo.
            try:
                own = uuid.UUID(t.legacy_id)
            except ValueError:
                own = None
            if own and conn.execute("SELECT 1 FROM todos WHERE id = %s AND legacy_id IS NULL", (own,)).fetchone():
                todo_id = own
            snooze = t.snoozed_until if t.state == "open" else None
            current = conn.execute("SELECT * FROM todos WHERE id = %s FOR UPDATE", (todo_id,)).fetchone()
            wanted = (t.title.strip(), t.state, snooze, t.notes, t.deleted)
            if current and (current["title"], current["state"],
                            current["snoozed_until"].isoformat() if current["snoozed_until"] else None,
                            current["notes"], current["deleted_at"] is not None) == wanted:
                counts["unchanged"] += 1
                continue
            row = conn.execute(
                """INSERT INTO todos (id, legacy_id, title, state, snoozed_until, notes, revision, deleted_at)
                   VALUES (%s, %s, %s, %s, %s, %s, 1, CASE WHEN %s THEN now() END)
                   ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, state = EXCLUDED.state,
                     snoozed_until = EXCLUDED.snoozed_until, notes = EXCLUDED.notes, revision = todos.revision + 1,
                     updated_at = now(), deleted_at = CASE WHEN %s THEN COALESCE(todos.deleted_at, now()) END
                   RETURNING *""",
                (todo_id, t.legacy_id, *wanted[:4], t.deleted, t.deleted)).fetchone()
            counts["updated" if current else "created"] += 1
            log_change(conn, meta, "todo", todo_json(row))
        conn.execute("UPDATE authority SET source_hash = %s, migration_id = %s WHERE domain = 'todos'",
                     (body.source_hash, body.migration_id))
    return counts


@app.get("/v1/worker/todos")
def worker_todos(_: Producer, conn: Db):
    """Everything the Tasks.md projection needs, including tombstones so they stay out of it."""
    authority = todo_authority(conn)
    rows = conn.execute("SELECT * FROM todos ORDER BY created_at, id").fetchall()
    return {"authority": authority["mode"], "source_hash": authority["source_hash"],
            "todos": [todo_json(r) for r in rows]}

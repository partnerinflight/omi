"""API v1, todo slice. Contract: docs/mobile/architecture-and-sync.md."""
import hashlib
import json
import uuid
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, field_validator

from . import auth
from .db import connect

app = FastAPI(title="Second Brain", version="1")
pairing_failures = auth.FailureLimiter()

MAX_BATCH = 100
MAX_PAGE = 500


def db():
    with connect() as conn:
        yield conn


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
def pair(body: PairRequest, conn: Db):
    if pairing_failures.blocked():
        raise HTTPException(429, "too_many_attempts", headers={"Retry-After": "600"})
    with conn.transaction():
        row = conn.execute(
            """UPDATE pairing_codes SET used_at = now()
               WHERE code_hash = %s AND used_at IS NULL AND expires_at > now() RETURNING device_name""",
            (auth.digest(body.code.strip().upper()),),
        ).fetchone()
        if not row:
            pairing_failures.fail()
            raise HTTPException(401, "invalid_code")
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


def todo_json(row) -> dict:
    return {"id": str(row["id"]), "title": row["title"], "state": row["state"], "revision": row["revision"],
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
    return {"cursor": cursor(meta["epoch"], meta["seq"]), "todos": [todo_json(t) for t in todos]}


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
    entity_type: Literal["todo"]
    entity_id: uuid.UUID
    base_revision: int = Field(ge=0)
    action: Literal["create", "set_title", "set_state"]
    payload: dict

    @field_validator("payload")
    @classmethod
    def small(cls, v):
        if len(json.dumps(v)) > 10_000:
            raise ValueError("payload too large")
        return v


class OperationBatch(BaseModel):
    operations: list[Operation] = Field(max_length=MAX_BATCH)


def validate_payload(op: Operation) -> str | None:
    p = op.payload
    if op.action in ("create", "set_title"):
        title = p.get("title")
        if set(p) != {"title"} or not isinstance(title, str) or not title.strip() or len(title) > 1000:
            return "invalid_title"
    elif set(p) != {"state"} or p.get("state") not in ("open", "completed", "dismissed"):
        return "invalid_state"
    return None


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

        current = conn.execute("SELECT * FROM todos WHERE id = %s FOR UPDATE", (op.entity_id,)).fetchone()

        def done(outcome, entity=None, error=None):
            result = {"operation_id": str(op.operation_id), "outcome": outcome, "error": error, "entity": entity}
            conn.execute("INSERT INTO receipts (operation_id, device_id, payload_hash, result) VALUES (%s, %s, %s, %s)",
                         (op.operation_id, device_id, h, Jsonb(result)))
            return result

        if error := validate_payload(op):
            return done("rejected", todo_json(current) if current else None, error)
        if op.action == "create":
            if current:
                return done("conflict", todo_json(current), "already_exists")
            if op.base_revision != 0:
                return done("rejected", None, "create_base_revision_must_be_0")
            row = conn.execute("INSERT INTO todos (id, title, state, revision) VALUES (%s, %s, 'open', 1) RETURNING *",
                               (op.entity_id, op.payload["title"].strip())).fetchone()
        else:
            if not current or current["deleted_at"]:
                return done("rejected", None, "not_found")
            if current["revision"] != op.base_revision:
                return done("conflict", todo_json(current), "stale_revision")
            column = "title" if op.action == "set_title" else "state"
            value = op.payload[column].strip() if column == "title" else op.payload[column]
            row = conn.execute(
                f"UPDATE todos SET {column} = %s, revision = revision + 1, updated_at = now() WHERE id = %s RETURNING *",
                (value, op.entity_id),
            ).fetchone()

        entity = todo_json(row)
        seq = meta["seq"] + 1
        conn.execute("UPDATE meta SET seq = %s", (seq,))
        conn.execute("INSERT INTO change_log (seq, entity_type, entity_id, revision, data) VALUES (%s, 'todo', %s, %s, %s)",
                     (seq, row["id"], row["revision"], Jsonb(entity)))
        return done("applied", entity)


@app.post("/v1/operations")
def operations(body: OperationBatch, device_id: Device, conn: Db):
    # Each operation commits on its own: a conflict never discards the others' results.
    return {"results": [apply_one(conn, device_id, op) for op in body.operations]}

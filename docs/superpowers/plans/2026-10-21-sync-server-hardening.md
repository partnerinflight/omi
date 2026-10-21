# Sync Server Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `server/` safe to expose on HA_Selfhost: unauthenticated callers cannot exhaust memory or shared Postgres connections, pairing cannot be locked out by strangers, restores do not revive revoked credentials, and devices can be listed and revoked.

**Architecture:** An ASGI body-limit middleware runs before routing/auth/JSON. A small `psycopg_pool` replaces per-request connections. The pairing limiter becomes per-IP plus a global ceiling, with atomic reservation. CLI gains `devices`/`revoke-device`; `new-epoch` revokes all credentials by default; worker data is read only from live producers; clips are keyed by producer. Deploy files gain Traefik body/rate limits, a memory/CPU cap, a narrowed forwarded-IP trust, an internal network, a hash-checked image build, and README steps for pg_hba and connection limits, with a CI lint of the compose file.

**Tech Stack:** Python 3.12, FastAPI 0.143, Starlette, uvicorn, psycopg 3 + psycopg_pool, Postgres 14 (Docker), unittest + httpx, Node 22 (`apps/mobile/tests/restore.test.ts`), Docker Compose, Traefik labels.

Spec: `docs/superpowers/specs/2026-10-21-sync-server-hardening-design.md`. One deliberate deviation, applied to the spec in Task 5: a malformed `/v1/changes` cursor returns **410** (`cursor_expired`, the existing contract: the phone bootstraps again) rather than 400; the bug fixed is the 500.

## File map

| File | Responsibility |
|---|---|
| `server/sb_server/limits.py` (create) | Body caps per route; ASGI middleware answering 413 before the app parses anything |
| `server/sb_server/app.py` (modify) | Install middleware, docs off, pooled `db` dependency, per-IP pairing, live-producer reads, clip producer scope, cursor regex |
| `server/sb_server/db.py` (modify) | `pool()` (psycopg_pool) for requests; `connect()` stays for CLI/migrations |
| `server/sb_server/auth.py` (modify) | `FailureLimiter` per key + global ceiling, thread-safe `reserve`/`release` |
| `server/sb_server/__main__.py` (modify) | `devices`, `revoke-device`, `new-epoch [--keep-credentials]` |
| `server/sb_server/migrations/006_clip_producer.sql` (create) | `clips.producer_id`, new primary key |
| `server/pyproject.toml`, `server/uv.lock` (modify) | add `psycopg-pool` |
| `server/Dockerfile` (modify) | hash-checked install, uv image pinned by digest |
| `server/tests/test_api.py` (modify) | boundary tests for every item |
| `apps/mobile/tests/restore.test.ts` (modify) | restore drill re-pairs after `new-epoch` |
| `deploy/selfhost/secondbrain.yml` (modify) | mem/cpu caps, Traefik middlewares, forwarded-IP subnet |
| `deploy/selfhost/ci-stub.yml`, `deploy/selfhost/check_compose.py` (create) | CI lint of the merged compose config |
| `deploy/selfhost/README.md` (modify) | internal network, pg_hba, CONNECTION LIMIT, restore, host checklist |
| `.github/workflows/ci.yml` (modify) | compose lint step in `contracts` |

Commands (from `server/` unless noted):
- One test: `uv run python -m unittest tests.test_api.Api.<name> -v` (needs Docker; it starts `postgres:14-alpine`).
- All server tests: `uv run python -m unittest discover -s tests -t .`
- Repo suites: from the repo root, `python3 scripts/test.py` (both suites print `OK`; a trailing `swift test` failure in a sandboxed shell is pre-existing).
- Docker commands may need to run outside a sandboxed shell.

The test harness (`tests/test_api.py`) starts uvicorn in a thread of the test process with default `proxy_headers`, which trusts `X-Forwarded-For` from `127.0.0.1`; tests use that header to act as different client IPs.

---

### Task 1: Body limits before auth, and docs off

**Files:**
- Create: `server/sb_server/limits.py`
- Modify: `server/sb_server/app.py:17` (app construction)
- Test: `server/tests/test_api.py`

- [ ] **Step 1: Write the failing tests**

Add to class `Api` in `server/tests/test_api.py`:

```python
    def test_oversized_bodies_get_413_before_auth(self):
        big = b"{" + b" " * 300_000 + b"}"
        # No token at all: the cap answers first, not the 401.
        self.assertEqual(self.http("POST", "/v1/pair", content=big,
                                   headers={"Content-Type": "application/json"}).status_code, 413)
        self.assertEqual(self.http("POST", "/v1/operations", content=b"x" * 1_200_000).status_code, 413)

        def chunks():  # no Content-Length: Transfer-Encoding: chunked
            for _ in range(40):
                yield b" " * 10_000
        r = httpx.post(self.base + "/v1/pair", content=chunks(), headers={"Content-Type": "application/json"}, timeout=10)
        self.assertEqual(r.status_code, 413)
        # Normal requests still work.
        self.assertEqual(self.http("POST", "/v1/pair", json={"code": "WRONGCODE1"}).status_code, 401)

    def test_route_caps_allow_real_payloads(self):
        from sb_server.limits import cap_for
        self.assertEqual(cap_for("PUT", "/v1/worker/clips/obs-1/0"), 2_100_000)
        self.assertEqual(cap_for("POST", "/v1/worker/events"), 7_000_000)
        self.assertEqual(cap_for("POST", "/v1/migration/todos"), 4_000_000)
        self.assertEqual(cap_for("POST", "/v1/operations"), 1_100_000)
        self.assertEqual(cap_for("POST", "/v1/pair"), 256_000)
        self.assertEqual(cap_for("GET", "/v1/bootstrap"), 256_000)

    def test_api_docs_are_not_served(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(self.http("GET", path).status_code, 404, path)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m unittest tests.test_api.Api.test_oversized_bodies_get_413_before_auth tests.test_api.Api.test_route_caps_allow_real_payloads tests.test_api.Api.test_api_docs_are_not_served -v`
Expected:
- the first fails: the 300 KB body gives 422 or 401, not 413;
- the second errors: `ModuleNotFoundError: sb_server.limits`;
- the third fails: `/docs` returns 200.

- [ ] **Step 3: Implement**

Create `server/sb_server/limits.py`:

```python
"""Request body caps, enforced before routing, authentication or JSON parsing.

FastAPI reads and parses a whole body before it resolves the auth dependency, so without
this an anonymous caller could make the process buffer gigabytes. Traefik caps bodies at
8 MB in front (deploy/selfhost); these per-route caps are the app's own floor."""
import re

CAPS = [  # (method, path, max body bytes); first match wins
    ("PUT", re.compile(r"/v1/worker/clips/[^/]+/\d+"), 2_100_000),  # one <=30 s PCM16 clip (~960 KB) + slack
    ("POST", re.compile(r"/v1/worker/events"), 7_000_000),  # 100 records x 64 KB
    ("POST", re.compile(r"/v1/migration/todos"), 4_000_000),
    ("POST", re.compile(r"/v1/operations"), 1_100_000),  # 100 operations x 10 KB payloads
]
DEFAULT_CAP = 256_000

_BODY = b'{"detail":"body_too_large"}'
_START = {"type": "http.response.start", "status": 413,
          "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(_BODY)).encode())]}


def cap_for(method: str, path: str) -> int:
    for m, pattern, cap in CAPS:
        if m == method and pattern.fullmatch(path):
            return cap
    return DEFAULT_CAP


class _TooLarge(Exception):
    pass


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        cap = cap_for(scope["method"], scope["path"])
        length = dict(scope["headers"]).get(b"content-length")
        if length is not None and (not length.isdigit() or int(length) > cap):
            await send(_START)
            return await send({"type": "http.response.body", "body": _BODY})

        seen, exceeded, started = 0, False, False

        async def limited_receive():
            nonlocal seen, exceeded
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > cap:
                    exceeded = True
                    raise _TooLarge  # stop reading; the app's error response is replaced below
            return message

        async def guarded_send(message):
            nonlocal started
            if not exceeded:
                started = started or message["type"] == "http.response.start"
                return await send(message)
            if message["type"] == "http.response.start" and not started:
                started = True
                await send(_START)
            elif message["type"] == "http.response.body" and not message.get("more_body"):
                await send({"type": "http.response.body", "body": _BODY})

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _TooLarge:
            if not started:
                await send(_START)
                await send({"type": "http.response.body", "body": _BODY})
```

In `server/sb_server/app.py` replace

```python
app = FastAPI(title="Second Brain", version="1")
```

with

```python
# No interactive docs or schema on the public host.
app = FastAPI(title="Second Brain", version="1", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(BodyLimit)
```

and add `from .limits import BodyLimit` to the imports next to `from . import auth`.

- [ ] **Step 4: Run to verify they pass**

Run: the same three tests as Step 2. Expected: PASS. Then run all server tests: `uv run python -m unittest discover -s tests -t .`. Expected: `OK`, which shows the real clip uploads, worker events and migration bodies stay under their caps.

- [ ] **Step 5: Commit**

```bash
git add server/sb_server/limits.py server/sb_server/app.py server/tests/test_api.py
git commit -m "feat(server): cap request bodies before auth; no public API docs"
```

---

### Task 2: Connection pool

**Files:**
- Modify: `server/sb_server/db.py`, `server/sb_server/app.py:24-29` (`db` dependency), `server/pyproject.toml`, `server/uv.lock`
- Test: `server/tests/test_api.py`

- [ ] **Step 1: Add the dependency**

Run (from `server/`): `uv add "psycopg-pool>=3.2"`
Expected: `pyproject.toml` lists `psycopg-pool`, and `uv.lock` is updated.

- [ ] **Step 2: Write the failing test**

In `setUpClass` of `server/tests/test_api.py`, directly after `os.environ["SB_DATABASE_URL"] = ...`, add:

```python
        os.environ["SB_DB_TIMEOUT"] = "1"  # pool wait before 503, short for the exhaustion test
```

Add to class `Api`:

```python
    def test_exhausted_pool_answers_503_not_a_hang(self):
        from sb_server import db
        self.assertEqual(self.http("GET", "/v1/health").status_code, 200)  # pool exists
        held = [db.pool().getconn() for _ in range(db.pool().max_size)]
        try:
            started = time.monotonic()
            r = self.http("GET", "/v1/health")
            self.assertEqual((r.status_code, r.headers.get("retry-after")), (503, "5"))
            self.assertLess(time.monotonic() - started, 5)
        finally:
            for conn in held:
                db.pool().putconn(conn)
        self.assertEqual(self.http("GET", "/v1/health").status_code, 200)
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run python -m unittest tests.test_api.Api.test_exhausted_pool_answers_503_not_a_hang -v`
Expected: ERROR `AttributeError: module 'sb_server.db' has no attribute 'pool'`.

- [ ] **Step 4: Implement**

`server/sb_server/db.py`: replace the `connect` function and its comment with:

```python
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def connect() -> psycopg.Connection:
    """A direct connection, for the admin CLI and migrations. Requests use pool()."""
    # Autocommit: each `with conn.transaction()` block is a real, independent commit.
    return psycopg.connect(os.environ["SB_DATABASE_URL"], row_factory=dict_row, autocommit=True)


def pool() -> ConnectionPool:
    """At most SB_DB_POOL (5) connections to the shared postgres-db, whatever the request rate;
    a request that waits SB_DB_TIMEOUT (5 s) for one is refused rather than queued forever."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(os.environ["SB_DATABASE_URL"], min_size=1, max_size=int(os.environ.get("SB_DB_POOL", "5")),
                               timeout=float(os.environ.get("SB_DB_TIMEOUT", "5")), check=ConnectionPool.check_connection,
                               kwargs={"row_factory": dict_row, "autocommit": True}, open=True)
    return _pool
```

`server/sb_server/app.py`: change the import `from .db import connect` to `from .db import pool`, then replace

```python
def db():
    with connect() as conn:
        yield conn
```

with

```python
def db():
    try:
        conn = pool().getconn()
    except PoolTimeout:
        raise HTTPException(503, "busy", headers={"Retry-After": "5"}) from None
    try:
        yield conn
    finally:
        pool().putconn(conn)
```

and add `from psycopg_pool import PoolTimeout` to the imports. Search `app.py` for any other `connect(` use. There should be none left; if one exists, switch it to the `Db` dependency.

`check=ConnectionPool.check_connection` drops connections broken by a Postgres restart, so the restore drill's "API reconnects per request" behaviour survives.

- [ ] **Step 5: Run to verify it passes**

Run: the test from Step 3, then all server tests. Expected: `OK`.

- [ ] **Step 6: Commit**

```bash
git add server/pyproject.toml server/uv.lock server/sb_server/db.py server/sb_server/app.py server/tests/test_api.py
git commit -m "feat(server): bounded connection pool; 503 instead of piling onto postgres-db"
```

---

### Task 3: Per-IP pairing limiter with a global ceiling

**Files:**
- Modify: `server/sb_server/auth.py` (`FailureLimiter`), `server/sb_server/app.py` (`pair`)
- Test: `server/tests/test_api.py`

- [ ] **Step 1: Write the failing tests**

Add to class `Api`:

```python
    def test_pairing_limiter_is_per_ip_with_a_global_ceiling(self):
        from sb_server import app as api
        api.pairing_failures.reset()
        attacker, owner = {"X-Forwarded-For": "203.0.113.7"}, {"X-Forwarded-For": "198.51.100.2"}
        for _ in range(10):
            self.assertEqual(self.http("POST", "/v1/pair", json={"code": "WRONGCODE1"}, headers=attacker).status_code, 401)
        r = self.http("POST", "/v1/pair", json={"code": self.pairing_code()}, headers=attacker)
        self.assertEqual((r.status_code, r.headers.get("retry-after")), (429, "600"))
        # Another address still pairs.
        self.assertEqual(self.http("POST", "/v1/pair", json={"code": self.pairing_code()}, headers=owner).status_code, 200)
        api.pairing_failures.reset()

    def test_limiter_counts_concurrent_attempts_and_global_ceiling(self):
        from sb_server.auth import FailureLimiter
        lim = FailureLimiter(per_key=10, window=600, total=100, total_window=3600)
        slots = [lim.reserve("a") for _ in range(12)]
        self.assertEqual(sum(s is not None for s in slots), 10)  # in-flight attempts count
        lim.release("a", slots[0])  # a success frees its slot
        self.assertIsNotNone(lim.reserve("a"))
        lim = FailureLimiter(per_key=10, window=600, total=3, total_window=3600)
        self.assertEqual([lim.reserve(k) is not None for k in "abcd"], [True, True, True, False])
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m unittest tests.test_api.Api.test_pairing_limiter_is_per_ip_with_a_global_ceiling tests.test_api.Api.test_limiter_counts_concurrent_attempts_and_global_ceiling -v`
Expected: both ERROR. The first with `AttributeError: 'FailureLimiter' object has no attribute 'reset'`, the second with a `TypeError` on `FailureLimiter(per_key=...)`.

- [ ] **Step 3: Implement**

In `server/sb_server/auth.py`, replace the whole `FailureLimiter` class with the version below. Add `import threading` at the top.

```python
class FailureLimiter:
    """Failed attempts per client key (IP) in a sliding window, plus a global ceiling.

    An attempt reserves a slot before it is checked and releases it on success, so
    concurrent guesses cannot slip past the limit. ponytail: in-process, so it resets on
    restart and assumes one server process (the container runs one)."""

    def __init__(self, per_key=10, window=600, total=100, total_window=3600):
        self.per_key, self.window, self.total, self.total_window = per_key, window, total, total_window
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.keys: dict[str, list[float]] = {}
            self.all: list[float] = []

    def reserve(self, key: str) -> float | None:
        """A slot (pass it to release on success), or None when the caller must wait."""
        now = time.monotonic()
        with self.lock:
            mine = [t for t in self.keys.get(key, []) if t > now - self.window]
            self.all = [t for t in self.all if t > now - self.total_window]
            if len(mine) >= self.per_key or len(self.all) >= self.total:
                self.keys[key] = mine
                return None
            mine.append(now)
            self.keys[key] = mine
            self.all.append(now)
            return now

    def release(self, key: str, slot: float):
        with self.lock:
            if slot in self.keys.get(key, []):
                self.keys[key].remove(slot)
            if slot in self.all:
                self.all.remove(slot)
```

In `server/sb_server/app.py`, change the `pair` route to the version below.

```python
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
```

Search the rest of `server/` for `pairing_failures.blocked` and `.fail(`. Update every hit to `reserve`/`release`, or delete it if it's dead.

- [ ] **Step 4: Run to verify they pass**

Run: the tests from Step 2, then all server tests. Expected: `OK`. Also run `test_pairing_code_is_single_use_and_endpoints_require_auth`, which must still pass.

- [ ] **Step 5: Commit**

```bash
git add server/sb_server/auth.py server/sb_server/app.py server/tests/test_api.py
git commit -m "feat(server): pairing limiter per client IP with a global ceiling; atomic under threads"
```

---

### Task 4: Device list and revoke; `new-epoch` revokes credentials; worker data from live producers only

**Files:**
- Modify: `server/sb_server/__main__.py`, `server/sb_server/app.py` (`pipeline`, `pipeline_records`)
- Modify: `apps/mobile/tests/restore.test.ts:152` (re-pair after `new-epoch`)
- Test: `server/tests/test_api.py`

- [ ] **Step 1: Write the failing tests**

Add to class `Api`:

```python
    def test_devices_list_and_revoke_cut_off_a_live_session(self):
        tokens = self.http("POST", "/v1/pair", json={"code": self.pairing_code("stolen phone")}).json()
        listing = self.cli("devices")
        self.assertIn("stolen phone", listing)
        self.assertIn("device", listing)
        self.cli("revoke-device", "stolen phone")
        self.assertEqual(self.http("GET", "/v1/bootstrap", tokens["access_token"]).status_code, 401)
        self.assertEqual(self.http("POST", "/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}).status_code, 401)
        self.assertIn("revoked", self.cli("devices"))

    def test_expired_codes_and_tokens_are_refused(self):
        code = self.pairing_code()
        with psycopg.connect(os.environ["SB_DATABASE_URL"], autocommit=True) as c:
            c.execute("UPDATE pairing_codes SET expires_at = now() - interval '1 second' WHERE used_at IS NULL")
        self.assertEqual(self.http("POST", "/v1/pair", json={"code": code}).status_code, 401)
        token = self.paired()["access_token"]
        with psycopg.connect(os.environ["SB_DATABASE_URL"], autocommit=True) as c:
            c.execute("UPDATE tokens SET expires_at = now() - interval '1 second' WHERE kind = 'access'")
        self.assertEqual(self.http("GET", "/v1/bootstrap", token).status_code, 401)

    def test_new_epoch_revokes_every_credential_unless_kept(self):
        phone = self.paired()
        worker = self.cli("add-producer", "epoch-pc").strip()
        out = self.cli("new-epoch")
        self.assertTrue(out.startswith("epoch: "))
        self.assertIn("revoked:", out)
        self.assertEqual(self.http("GET", "/v1/bootstrap", phone["access_token"]).status_code, 401)
        self.assertEqual(self.http("POST", "/v1/auth/refresh", json={"refresh_token": phone["refresh_token"]}).status_code, 401)
        self.assertEqual(self.http("GET", "/v1/worker/commands", worker).status_code, 401)
        # Worker data from a revoked producer is no longer shown.
        fresh = self.paired()["access_token"]
        self.assertNotIn("epoch-pc", [w["name"] for w in self.http("GET", "/v1/pipeline", fresh).json()["workers"]])
        kept = self.paired()
        self.cli("new-epoch", "--keep-credentials")
        self.assertEqual(self.http("GET", "/v1/bootstrap", kept["access_token"]).status_code, 200)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m unittest tests.test_api.Api.test_devices_list_and_revoke_cut_off_a_live_session tests.test_api.Api.test_expired_codes_and_tokens_are_refused tests.test_api.Api.test_new_epoch_revokes_every_credential_unless_kept -v`
Expected:
- The devices test and the new-epoch test fail with `subprocess.CalledProcessError`: `invalid choice: 'devices'`, and `new-epoch` doesn't revoke.
- The expiry test passes already. Keep it as regression coverage, since the review flagged expiry as untested.

- [ ] **Step 3: Implement**

In `server/sb_server/__main__.py`:

1. Change the module docstring's command list to `{migrate|pair NAME|devices|revoke-device NAME_OR_ID|add-producer NAME|revoke-producer NAME|add-integration NAME|authority [cutover|rollback]|new-epoch [--keep-credentials]|serve}`.

2. Next to the other `sub.add_parser` calls, register:

```python
    sub.add_parser("devices", help="list phones, integrations and Windows workers")
    revoke_device = sub.add_parser("revoke-device", help="cut off a phone or integration now")
    revoke_device.add_argument("device", help="name or id, as shown by 'devices'")
```

Then replace the existing `sub.add_parser("new-epoch", ...)` line with:

```python
    epoch = sub.add_parser("new-epoch", help="run after restoring a backup: devices bootstrap again and every "
                                             "credential is revoked (pair phones, reconnect Windows)")
    epoch.add_argument("--keep-credentials", action="store_true",
                       help="only for a restore you know predates no revocation")
```

3. Add these branches before `elif args.command == "new-epoch":`, and replace that branch:

```python
    elif args.command == "devices":
        with connect() as conn:
            rows = conn.execute(
                """SELECT 'device' AS kind, d.id, d.name, d.created_at, d.revoked_at,
                          (SELECT max(t.expires_at) FROM tokens t WHERE t.device_id = d.id AND t.used_at IS NULL) AS expires
                   FROM devices d
                   UNION ALL
                   SELECT 'producer', p.id, p.name, p.created_at, p.revoked_at, NULL FROM producers p
                   ORDER BY created_at""").fetchall()
        for r in rows:
            state = f"revoked {r['revoked_at']:%Y-%m-%d}" if r["revoked_at"] else (
                f"active until {r['expires']:%Y-%m-%d}" if r["expires"] else "active")
            print(f"{r['kind']:8} {r['id']}  {r['name']}  (added {r['created_at']:%Y-%m-%d}, {state})")
    elif args.command == "revoke-device":
        with connect() as conn, conn.transaction():
            rows = conn.execute("SELECT id, name FROM devices WHERE revoked_at IS NULL AND (id::text = %s OR name = %s)",
                                (args.device, args.device)).fetchall()
            if len(rows) != 1:
                raise SystemExit(f"{len(rows)} active devices match {args.device!r}; use an id from 'devices'")
            conn.execute("UPDATE devices SET revoked_at = now() WHERE id = %s", (rows[0]["id"],))
            conn.execute("DELETE FROM tokens WHERE device_id = %s", (rows[0]["id"],))
        print(f"revoked: {rows[0]['name']} ({rows[0]['id']})")
    elif args.command == "new-epoch":
        with connect() as conn, conn.transaction():
            print("epoch:", conn.execute("UPDATE meta SET epoch = gen_random_uuid() RETURNING epoch").fetchone()["epoch"])
            if not args.keep_credentials:
                # A restored dump also restores revocations as of the backup: start every credential over.
                devices = conn.execute("UPDATE devices SET revoked_at = now() WHERE revoked_at IS NULL").rowcount
                producers = conn.execute("UPDATE producers SET revoked_at = now() WHERE revoked_at IS NULL").rowcount
                conn.execute("DELETE FROM tokens")
                conn.execute("DELETE FROM pairing_codes WHERE used_at IS NULL")
                print(f"revoked: {devices} devices, {producers} producers; pair phones again and reconnect Windows")
```

In `server/sb_server/app.py`, make worker data come only from producers that are not revoked:
- `pipeline()`: the counts query becomes
  `SELECT w.kind, count(*) AS n FROM worker_records w JOIN producers p ON p.id = w.producer_id AND p.revoked_at IS NULL WHERE w.deleted_at IS NULL GROUP BY w.kind`.
  The producers query already filters `revoked_at IS NULL`.
- `pipeline_records()`: the query becomes
  `SELECT w.id, w.hash, w.data, w.updated_at FROM worker_records w JOIN producers p ON p.id = w.producer_id AND p.revoked_at IS NULL WHERE w.kind = %s AND w.deleted_at IS NULL ORDER BY w.updated_at DESC, w.id LIMIT %s`.
  Keep the same parameters and output shape.

In `apps/mobile/tests/restore.test.ts`, the drill must now re-pair after `new-epoch`. Directly after `assert.match(sb('new-epoch'), /^epoch: /);` (line 152), insert:

```ts
  // new-epoch revoked every credential: both phones see sign-in expired, keep their queued
  // edits, and pair again with the same server; the worker is reconnected as well.
  assert.equal((await a.sync.sync(true)).state, 'auth_required');
  await a.sync.pair(url, sb('pair', 'a').split(/\s+/)[0]);
  await b.sync.pair(url, sb('pair', 'b').split(/\s+/)[0]);
```

Then read the rest of the drill. If later steps use the `worker` token from line 114 (`add-producer`), add `worker = sb('add-producer', 'drill-pc')` after the re-pair, changing its declaration from `const` to `let`, and say so in your report. The two calls below each use a fresh code. `b` is offline at this point in the drill, so if `b.sync.pair` needs the network, move that line after `b.net.offline = false;`, before the existing `b.sync.sync(true)`.

- [ ] **Step 4: Run to verify they pass**

Run: the tests from Step 2, then all server tests. Then, from `apps/mobile`, run `npm run test:restore` (Docker; builds the server image). Expected: server tests `OK`, and the restore drill passes, still asserting that both phones lose nothing.

- [ ] **Step 5: Commit**

```bash
git add server/sb_server/__main__.py server/sb_server/app.py server/tests/test_api.py apps/mobile/tests/restore.test.ts
git commit -m "feat(server): list/revoke devices; new-epoch revokes every credential; hide revoked workers' data"
```

---

### Task 5: Clips keyed by producer; malformed cursors

**Files:**
- Create: `server/sb_server/migrations/006_clip_producer.sql`
- Modify: `server/sb_server/app.py` (`worker_events` clip delete, `worker_clip`, `clip`, `changes`), `server/tests/test_api.py:52` (migration list), `docs/superpowers/specs/2026-10-21-sync-server-hardening-design.md` (cursor status)
- Test: `server/tests/test_api.py`

- [ ] **Step 1: Write the failing tests**

In `setUpClass`, change the migration assertion to include `"006_clip_producer.sql"` at the end of the list. Add to class `Api`:

```python
    def _observation(self, token, oid="obs-shared"):
        rec = {"kind": "observation", "id": oid, "hash": uuid.uuid4().hex,
               "data": {"display": "Speaker 1", "recorded_at": "2026-10-21T10:00:00Z", "person_id": None,
                        "provenance": None, "score": None, "published": 1, "clips": [0]}}
        self.assertEqual(self.http("POST", "/v1/worker/events", token, json={"records": [rec], "status": {}}).status_code, 200)

    def _clip(self, token, data, oid="obs-shared"):
        import hashlib
        return self.http("PUT", f"/v1/worker/clips/{oid}/0", token, content=data,
                         headers={"X-Content-SHA256": hashlib.sha256(data).hexdigest()})

    def test_clips_are_scoped_to_their_producer(self):
        one, two = self.cli("add-producer", "pc-one").strip(), self.cli("add-producer", "pc-two").strip()
        self._observation(one)
        self.assertEqual(self._clip(one, b"RIFF-one").status_code, 204)
        self.assertEqual(self._clip(two, b"RIFF-two").status_code, 404)  # not its observation
        self._observation(two)
        self.assertEqual(self._clip(two, b"RIFF-two").status_code, 204)
        # pc-two clearing its observation's clips leaves pc-one's clip alone.
        rec = {"kind": "observation", "id": "obs-shared", "hash": uuid.uuid4().hex, "data": None}
        self.http("POST", "/v1/worker/events", two, json={"records": [rec], "status": {}})
        phone = self.paired()["access_token"]
        r = self.http("GET", "/v1/clips/obs-shared/0", phone)
        self.assertEqual((r.status_code, r.content), (200, b"RIFF-one"))
        self.cli("revoke-producer", "pc-one")
        self.cli("revoke-producer", "pc-two")
        self.assertEqual(self.http("GET", "/v1/clips/obs-shared/0", phone).status_code, 410)

    def test_malformed_cursor_is_expired_not_a_server_error(self):
        token = self.paired()["access_token"]
        epoch = self.http("GET", "/v1/bootstrap", token).json()["cursor"].split(":")[0]
        for bad in (f"{epoch}:²", f"{epoch}:" + "9" * 25, f"{epoch}:-1", f"{epoch}:"):
            self.assertEqual(self.http("GET", "/v1/changes", token, params={"cursor": bad}).status_code, 410, bad)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run python -m unittest tests.test_api.Api.test_clips_are_scoped_to_their_producer tests.test_api.Api.test_malformed_cursor_is_expired_not_a_server_error -v`
Expected: `setUpClass` fails on the migration list assertion first, because 006 doesn't exist. After you create an empty 006 file, the clip test fails on the GET returning `RIFF-two` or 410, and the cursor test fails with a 500 for `²`.

- [ ] **Step 3: Implement**

Create `server/sb_server/migrations/006_clip_producer.sql`:

```sql
-- Clips belong to the producer that uploaded them: one Windows worker cannot overwrite,
-- delete or serve another's clip by reusing an observation id.
ALTER TABLE clips ADD COLUMN producer_id uuid REFERENCES producers;
UPDATE clips c SET producer_id = w.producer_id
  FROM worker_records w WHERE w.kind = 'observation' AND w.id = c.observation_id;
DELETE FROM clips WHERE producer_id IS NULL;
ALTER TABLE clips ALTER COLUMN producer_id SET NOT NULL;
ALTER TABLE clips DROP CONSTRAINT clips_pkey;
ALTER TABLE clips ADD PRIMARY KEY (producer_id, observation_id, n);
```

In `server/sb_server/app.py`:

- In `worker_events`, change the clip delete to scope it to this producer:
  `conn.execute("DELETE FROM clips WHERE producer_id = %s AND observation_id = %s", (producer_id, r.id))`.
- In `worker_clip`, change the insert to:

```python
    conn.execute("""INSERT INTO clips (producer_id, observation_id, n, sha256, bytes) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (producer_id, observation_id, n) DO UPDATE SET sha256 = EXCLUDED.sha256,
                    bytes = EXCLUDED.bytes, created_at = now()""", (producer_id, observation_id, n, x_content_sha256, body))
```

- In `clip` (the device download), change the query to:

```python
    row = conn.execute("""SELECT c.sha256, c.bytes FROM clips c JOIN producers p ON p.id = c.producer_id AND p.revoked_at IS NULL
                          WHERE c.observation_id = %s AND c.n = %s ORDER BY c.created_at DESC LIMIT 1""",
                       (observation_id, n)).fetchone()
```

- In `changes`, replace `or not seq.isdigit()` with `or not re.fullmatch(r"\d{1,18}", seq)`. `re` is already imported.

In the spec, under "5. Smaller items", change the cursor bullet to: ``Cursor parsing: `re.fullmatch(r"\d{1,18}", seq)`; anything else is 410 `cursor_expired` (the existing contract: the phone bootstraps again), never a 500 (Low 11).``

- [ ] **Step 4: Run to verify they pass**

Run: the tests from Step 2, then all server tests, then from the repo root `python3 scripts/test.py`. The Windows bridge test uploads clips through the real API. Expected: everything `OK`.

- [ ] **Step 5: Commit**

```bash
git add server/sb_server/migrations/006_clip_producer.sql server/sb_server/app.py server/tests/test_api.py docs/superpowers/specs/2026-10-21-sync-server-hardening-design.md
git commit -m "fix(server): clips scoped to their producer; malformed cursors expire instead of 500"
```

---

### Task 6: Image build and deploy files

**Files:**
- Modify: `server/Dockerfile`, `deploy/selfhost/secondbrain.yml`, `deploy/selfhost/README.md`, `.github/workflows/ci.yml`
- Create: `deploy/selfhost/ci-stub.yml`, `deploy/selfhost/check_compose.py`

- [ ] **Step 1: Dockerfile**

Get the uv image digest:
`docker buildx imagetools inspect ghcr.io/astral-sh/uv:0.12.10 --format '{{json .Manifest.Digest}}'`
It prints `"sha256:..."`.

In `server/Dockerfile`, replace the `COPY --from=ghcr.io/astral-sh/uv:0.12.10 ...` and `RUN uv export ...` lines with the following, using the real digest:

```dockerfile
COPY --from=ghcr.io/astral-sh/uv:0.12.10@sha256:<digest printed above> /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
# Hashes from uv.lock are enforced; nothing outside the lock is installed.
RUN uv export --frozen --no-dev --no-emit-project -o /tmp/req.txt \
 && pip install --no-cache-dir --require-hashes --no-deps -r /tmp/req.txt && rm /tmp/req.txt
```

Keep the existing `WORKDIR`, `COPY sb_server`, `USER`, `ENV` (including `SB_HOST=0.0.0.0`), `EXPOSE`, `ENTRYPOINT` and `CMD` lines, and don't duplicate `WORKDIR` or `COPY pyproject.toml`. Verify with `docker build -t sb-server-check server`; it must succeed. Then run `docker run --rm --entrypoint python sb-server-check -c "import fastapi, psycopg, psycopg_pool, uvicorn; print('ok')"`; it must print `ok`.

- [ ] **Step 2: Compose hardening**

In `deploy/selfhost/secondbrain.yml`, service `secondbrain`:

- After `pids_limit: 256`, add:

```yaml
    mem_limit: 256m   # worst case is this container restarting, never the host OOM-killing Home Assistant
    cpus: 1.0
```

- Change `SB_FORWARDED_ALLOW_IPS: "*"  # only Traefik can reach this container` to:

```yaml
      SB_FORWARDED_ALLOW_IPS: "172.30.5.32/27"  # proxy_secondbrain: trust X-Forwarded-For only from Traefik
```

- In `labels`, replace `- "traefik.http.routers.secondbrain-rtr.middlewares=chain-no-auth@file"` with:

```yaml
      - "traefik.http.routers.secondbrain-rtr.middlewares=chain-no-auth@file,secondbrain-rl,secondbrain-body"
      # Per-client-IP rate limit and an 8 MB body cap in front of the app's own per-route caps.
      - "traefik.http.middlewares.secondbrain-rl.ratelimit.average=20"
      - "traefik.http.middlewares.secondbrain-rl.ratelimit.burst=40"
      - "traefik.http.middlewares.secondbrain-rl.ratelimit.period=1s"
      - "traefik.http.middlewares.secondbrain-body.buffering.maxRequestBodyBytes=8000000"
```

- [ ] **Step 3: CI lint of the merged compose config**

Create `deploy/selfhost/ci-stub.yml`, which stands in for what HA_Selfhost's own compose files define:

```yaml
# CI only: stands in for the HA_Selfhost networks and postgres service secondbrain.yml joins.
networks:
  proxy_secondbrain:
    internal: true
  secondbrain_db:
    internal: true
services:
  postgresdb:
    image: postgres:14-alpine
    healthcheck: {test: ["CMD", "true"]}
```

Create `deploy/selfhost/check_compose.py`:

```python
"""Fail if the merged selfhost compose config loses its hardening.

    docker compose -f deploy/selfhost/ci-stub.yml -f deploy/selfhost/secondbrain.yml --profile apps \\
        config --format json | python3 deploy/selfhost/check_compose.py
"""
import json
import sys

config = json.load(sys.stdin)
s = config["services"]["secondbrain"]
labels = s.get("labels", {})
labels = labels if isinstance(labels, dict) else dict(l.split("=", 1) for l in labels)
router = labels.get("traefik.http.routers.secondbrain-rtr.middlewares", "")
checks = {
    "no published ports": not s.get("ports"),
    "non-root user": s.get("user") == "65532:65532",
    "read-only root": s.get("read_only") is True,
    "all capabilities dropped": s.get("cap_drop") == ["ALL"],
    "no-new-privileges": "no-new-privileges:true" in s.get("security_opt", []),
    "memory limit": bool(s.get("mem_limit")),
    "cpu limit": float(s.get("cpus") or 0) > 0,
    "pids limit": bool(s.get("pids_limit")),
    "forwarded IPs not wildcard": s["environment"].get("SB_FORWARDED_ALLOW_IPS") not in (None, "*"),
    "rate limit on router": "secondbrain-rl" in router.split(","),
    "body limit on router": "secondbrain-body" in router.split(","),
    "body limit configured": labels.get("traefik.http.middlewares.secondbrain-body.buffering.maxRequestBodyBytes") == "8000000",
}
failed = [name for name, ok in checks.items() if not ok]
print("\n".join(f"{'ok  ' if ok else 'FAIL'} {name}" for name, ok in checks.items()))
sys.exit(1 if failed else 0)
```

Run locally (Docker required):

```bash
HA_COMPOSE_PROJECT_NAME=ci DOMAINNAME_CLOUD_SERVER=example.com SECONDBRAIN_SERVER_COMMIT=0000000 SECONDBRAIN_DB_PASSWORD=x \
  docker compose -f deploy/selfhost/ci-stub.yml -f deploy/selfhost/secondbrain.yml --profile apps config --format json \
  | python3 deploy/selfhost/check_compose.py
```

Expected: every line `ok`, exit 0. Then check that it catches regressions: temporarily delete `mem_limit` and confirm `FAIL memory limit` and exit 1, then restore it. If `docker compose config` normalises a field differently (for example `cpus` as a string, or `mem_limit` in bytes), adjust only the check's parsing, never the condition, and say so in your report.

In `.github/workflows/ci.yml`, job `contracts`, after `- run: python scripts/test.py`, add:

```yaml
      - name: Selfhost compose keeps its hardening
        env:
          HA_COMPOSE_PROJECT_NAME: ci
          DOMAINNAME_CLOUD_SERVER: example.com
          SECONDBRAIN_SERVER_COMMIT: "0000000"
          SECONDBRAIN_DB_PASSWORD: ci-not-a-secret
        run: |
          docker compose -f deploy/selfhost/ci-stub.yml -f deploy/selfhost/secondbrain.yml --profile apps \
            config --format json | python3 deploy/selfhost/check_compose.py
```

- [ ] **Step 4: README**

Edit `deploy/selfhost/README.md`:

1. Section 1, step 2, networks snippet: add `internal: true` under `proxy_secondbrain` (next to its `name:`). Add a sentence after the snippet: "`internal: true` on both networks: the container has no internet egress; Traefik still reaches it on `proxy_secondbrain`."

2. Section 1, step 3, after the three `psql` lines, add:

````markdown
   Then limit the role to its own database and a few connections (the app pools at most 5):
   ```sh
   ./ha compose exec postgresdb psql -U "$POSTGRES_DB_USERNAME" -c "ALTER ROLE secondbrain CONNECTION LIMIT 8"
   ```
   In postgres-db's `pg_hba.conf`, **above** the generic `host all all ...` lines, add:
   ```
   host  secondbrain  secondbrain  samenet  scram-sha-256
   host  all          secondbrain  all      reject
   ```
   reload (`./ha compose exec postgresdb psql -U "$POSTGRES_DB_USERNAME" -c "SELECT pg_reload_conf()"`) and
   verify the role cannot reach any other database — this must fail:
   ```sh
   ./ha compose exec postgresdb psql -U secondbrain -d postgres -h 127.0.0.1 -c "SELECT 1"
   ```
   Other apps' databases are not changed: revoking `PUBLIC` privileges there could break them.
````

3. Section 1, add a step 6, "Before exposing it, confirm on the host":
   - what `chain-no-auth@file` contains (no auth bypass beyond Google forward auth, plus security headers and HSTS);
   - Traefik's entrypoint timeouts (`readTimeout` set, so slow uploads can't hold connections forever);
   - that Traefik is the client-facing edge. If a CDN or tunnel sits in front, the rate limit's `sourceCriterion` must use its client-IP header;
   - from outside, `https://brain.<domain>/docs` and `/openapi.json` return 404, and a 10 MB POST returns 413.

4. "Restore" section: replace the first paragraph with the text below. Keep the paragraph after it.

````markdown
   Restore `all_postgres.sql` as usual, then run
   `./ha compose exec secondbrain python -m sb_server new-epoch`. It resets the sync epoch **and revokes
   every phone, integration and Windows credential** (a dump also restores revocations as of the backup,
   so a phone revoked since would otherwise work again). Pair each phone again (Status tab; queued edits
   are kept and sent), create a new token for Windows with `add-producer` and reconnect it, and recreate
   the Hermes integration token. Phones bootstrap again and re-send edits the backup is missing; the
   Windows bridge resends its records. `new-epoch --keep-credentials` skips the revocation, for a restore
   you know predates no revocation.
````

5. Section 1, step 5: add a sentence: "`sb_server devices` lists every phone, integration and Windows worker; `sb_server revoke-device NAME_OR_ID` cuts a phone or integration off immediately; `revoke-producer NAME` does the same for Windows."

- [ ] **Step 5: Verify and commit**

Run: the compose check from Step 3, `docker build -t sb-server-check server`, all server tests, `npm run test:restore` (from `apps/mobile`), and `python3 scripts/test.py` (from the repo root). Expected: all pass.

```bash
git add server/Dockerfile deploy/selfhost/secondbrain.yml deploy/selfhost/ci-stub.yml deploy/selfhost/check_compose.py deploy/selfhost/README.md .github/workflows/ci.yml
git commit -m "feat(deploy): hash-checked image; Traefik rate/body limits; mem/cpu caps; narrowed proxy trust; CI compose lint"
```

---

### Task 7: Final verification

- [ ] Push the branch and watch CI (`gh run list --branch feature/wifi-local-upload --limit 1`, then `gh run watch <id> --exit-status`). Both jobs must pass, including the new compose step.
- [ ] Say what was verified and what wasn't.
  - **Verified** (Docker locally and CI): server boundary tests, the restore drill, the compose lint and the image build.
  - **Not verified** (the owner checks on the real host, per the README checklist): `pg_hba` and `CONNECTION LIMIT` on HA's `postgres-db`, the behaviour of the live Traefik middleware, the contents of `chain-no-auth`, and timeouts.

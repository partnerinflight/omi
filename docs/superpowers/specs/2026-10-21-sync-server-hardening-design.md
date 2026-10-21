# Sync server hardening — design

Date: 2026-10-21. Status: approved direction (owner, 2026-10-21); first of three specs
(hardening → device linking → one-command deploy + installer wizard).

## Problem

`server/` (the sync API for the phone and the Windows bridge) is about to be exposed to the
internet on HA_Selfhost behind Traefik/TLS, on a host that also runs Home Assistant and a
shared `postgres-db`. A read-only security review on 2026-10-21 found it must not ship as is:
unauthenticated callers can exhaust host memory and shared database connections, a backup
restore revives revoked credentials, and devices cannot be revoked. Goal: the service cannot
become a way into, or a way to take down, the rest of the server.

Already fixed during the review (2026-10-21, uncommitted): `sb_server serve` binds
`SB_HOST` (default `127.0.0.1`); the image sets `SB_HOST=0.0.0.0` and publishes no port.

## Decisions

### 1. Request size, before auth (review High 1, Low 9)

- **Traefik** (labels in `deploy/selfhost/secondbrain.yml`): a `secondbrain-body` buffering
  middleware with `maxRequestBodyBytes: 8000000` added to the router chain.
- **App**: a pure-ASGI middleware in `sb_server/limits.py` counts body bytes as they arrive
  (covers chunked bodies with no `Content-Length`) and answers 413 the moment a route's cap is
  passed, before FastAPI parses JSON or resolves auth. Caps by path:
  `PUT /v1/worker/clips/*` 2.1 MB; `POST /v1/worker/events` 7 MB (100 records × 64 KB);
  `POST /v1/migration/todos` 4 MB; `POST /v1/operations` 1.1 MB (100 × 10 KB payloads);
  everything else 256 KB. A `Content-Length` above the cap is refused without reading.
  The clip cap rests on `speaker_audio.candidates`: clips are at most 12 s of 16 kHz mono
  PCM16 (~385 KB) whatever the recording length, uploaded one per request; the existing
  2 MB `MAX_CLIP_BYTES` (server and bridge) stays, and the bridge skips a larger file.
- **Container**: `mem_limit: 256m`, `cpus: 1.0` (plus the existing `pids_limit`), so the worst
  case is this container restarting, never the host OOM-killing Home Assistant.

### 2. Shared Postgres protection (Medium 4, Medium 5)

- `sb_server/db.py` moves to `psycopg_pool.ConnectionPool(min_size=1, max_size=5,
  timeout=5)`; a request that cannot get a connection in 5 s gets 503. The `Db` dependency
  borrows from the pool. Admin CLI commands keep direct connections.
- Deploy: `ALTER ROLE secondbrain CONNECTION LIMIT 8` (pool + CLI headroom), and pg_hba
  lines placed before the generic ones, so the role can reach only its own database:
  `host secondbrain secondbrain samenet scram-sha-256` then `host all secondbrain all reject`.
  The README gives the exact lines and a verification (`psql -U secondbrain -d postgres`
  must fail). Other HA databases are not changed (revoking `PUBLIC` there could break
  their apps).

### 3. Rate limits and client IPs (Medium 6)

- Traefik `secondbrain-rl` ratelimit middleware: average 20/s, burst 40, per client IP.
- `SB_FORWARDED_ALLOW_IPS` in compose becomes Traefik's subnet on `proxy_secondbrain`
  (`172.30.5.32/27`, the README's network) instead of `"*"`, so `request.client.host` is the
  real client.
- Pairing limiter (`auth.FailureLimiter`): per IP 10 failures / 10 min, plus a global ceiling
  of 100 / hour; a lock makes check-and-record atomic across threadpool workers. Separate
  limiter instances per endpoint (`/v1/pair` now; `/v1/enroll` in the linking spec).

### 4. Credentials: list, revoke, and restore (High 2, Medium 3)

- CLI: `sb_server devices` lists devices and producers (name, kind, created, revoked, token
  expiry). `sb_server revoke-device NAME|ID` sets `revoked_at` and deletes the device's
  `tokens` rows; `revoke-producer` unchanged. Revoking the Hermes integration device uses the
  same command.
- Restore: `sb_server new-epoch` also revokes every device and producer and deletes all
  tokens, in the same transaction as the epoch change, and prints what was revoked. Phones see
  `auth_required` ("Sign-in expired — pair again", already in the app); Windows reconnects
  (manually now, through `connect` once linking ships). `new-epoch --keep-credentials` exists
  for a restore the owner knows predates no revocation; the README restore section says so.

### 5. Smaller items

- Docs off: `FastAPI(docs_url=None, redoc_url=None, openapi_url=None)` (Low 7).
- Supply chain: Dockerfile exports the lock with hashes and installs
  `pip install --require-hashes --no-deps`; the uv build image is pinned by digest (Low 8).
- Clips keyed by producer: migration `006_clip_producer.sql` adds `producer_id` to `clips`
  (primary key `(producer_id, observation_id, n)`); upload, delete and download scope by it
  (download joins the observation's producer) (Low 10).
- Cursor parsing: `re.fullmatch(r"\d{1,18}", seq)`; anything else is 400 (Low 11).
- Network: `proxy_secondbrain` becomes `internal: true`, so the container has no internet
  egress; Traefik reaches it on that network as now (Low 12).

## Out of scope

Device linking endpoints (next spec; they must use the limiter keys and revocation cascade
defined here). What `chain-no-auth@file`, Traefik timeouts/HSTS and `postgres-db` settings
contain on the real host: the deploy README gets a checklist to confirm them, since they are
not visible from this repo.

## Testing

`server/tests/test_api.py` (uvicorn over HTTP, Postgres 14 in Docker), new cases:
- 413 for an oversized JSON body with and without `Content-Length` (chunked), before auth
  (an unauthenticated oversized request still gets 413, not 401); clip cap; events cap.
- Pool exhaustion returns 503, not a hang.
- Pairing limiter: per-IP 429 after 10 failures while another IP still pairs; global ceiling;
  concurrent failures cannot exceed the limit.
- Expired pairing code and expired access token are refused; a revoked device's refresh token
  is refused; `revoke-device` cuts off an active session immediately.
- `new-epoch` revokes everything; `--keep-credentials` does not.
- `/docs`, `/redoc`, `/openapi.json` return 404.
- Clips from one producer cannot be overwritten, deleted or read through another's ids.
- Bad cursors (`²`, 25 digits) give 400.
The deploy-side items (Traefik labels, `mem_limit`, pg_hba, internal network) are checked
by a compose lint in CI (`docker compose config` on the file with dummy env) and by the
README's verification steps on the real host; the owner runs those, and this spec does not
claim them as tested.

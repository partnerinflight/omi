# Second Brain sync API (milestones 1–2: todos, worker shadow import)

FastAPI + Postgres. Implements the todo slice of
[architecture-and-sync.md](../docs/mobile/architecture-and-sync.md): device
pairing, rotating refresh tokens, idempotent operation receipts, whole-entity
revision conflicts, and an ordered change log with epoch-scoped cursors.
Deployment: [deploy/selfhost](../deploy/selfhost/README.md).

```sh
uv sync
uv run python -m unittest discover -s tests -t .   # needs Docker; starts postgres:14-alpine
SB_DATABASE_URL=postgresql://... uv run python -m sb_server serve
SB_DATABASE_URL=postgresql://... uv run python -m sb_server pair "Test phone"
SB_DATABASE_URL=postgresql://... uv run python -m sb_server add-producer "Home PC"   # token for the Windows bridge
```

| Route | Notes |
|---|---|
| `POST /v1/pair` | `{code}` → device id, access (15 min) and refresh (90 days) tokens |
| `POST /v1/auth/refresh` | rotates; replaying a used refresh token revokes the device |
| `POST /v1/auth/revoke` | revokes the calling device |
| `GET /v1/bootstrap` | all live todos plus a cursor, from one snapshot |
| `GET /v1/changes?cursor=&limit=` | ordered entity snapshots; 410 after `new-epoch` |
| `POST /v1/operations` | ≤100 ops `create`/`set_title`/`set_state`; each commits alone |
| `POST /v1/worker/events` | producer token only; ≤100 `source`/`item`/`person`/`observation` records, deduplicated by id and hash; `data: null` is a tombstone; also records worker freshness |
| `GET /v1/pipeline`, `GET /v1/pipeline/records?kind=` | device token only; worker freshness, counts and latest records (read-only shadow) |

Worker records are untrusted model output stored as inert JSON. Device and producer
tokens are separate tables, so neither can call the other's routes.

Not yet: OpenAPI-generated client types, editable memories/people/speakers, worker
commands, clips, tombstone retention/compaction, multi-owner scoping.

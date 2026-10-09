# Proposed architecture and sync contract

Design only, 2026-10-09. No network API below exists yet.

## Topology and authority

```text
Omi / Mac meeting capture --> Windows receiver + local models
                                   | durable outbound events / command polling
                                   v
iPhone SQLite <--- HTTPS ---> self-hosted API + PostgreSQL + private clip storage
                                   |
                         export events to Windows
                                   v
                           secondary Obsidian vault
```

Proposed server: Python/FastAPI, PostgreSQL, schema migrations, and a private
filesystem volume for short review clips. Deploy later as containers behind the
host's HTTPS reverse proxy; a local Compose setup suffices for development.
Prefer one service and DB over introducing a broker, object-storage cluster or
hosted sync vendor. Actual host OS/CPU, volume paths, domain, VPN/proxy and backup
destination must be checked before deployment. The phone can use a private VPN
URL or publicly reachable authenticated HTTPS. Never expose receiver port 7331,
the tray mailbox, SMB, or database ports to the internet.

| Domain | Authority after cutover | Replicas / effects |
|---|---|---|
| Audio, checkpoints, job attempts, frozen manifests | Windows | Existing durable local archive and publication |
| Todo status/text/dates, curated memories, clarification answers | Server DB | Phone cache; versioned vault projections |
| Desired person identity/observation assignments | Server DB | Phone UI and ordered worker commands |
| Applied assignments, embeddings, enrollment and matching results | Windows speaker store | Server applied-status projection; embeddings stay local |
| Raw source text and original attribution | Immutable processing evidence | Minimal server evidence/excerpts; richer vault notes |
| Human prose in existing vault notes | Vault author | Preserved; no implicit bidirectional import |

Server acceptance and worker application are separate durable states. A phone
assignment can be saved while Windows is offline; it is not yet a trained voice
reference. Worker matching publishes inferred assignments, never overwrites a
human desired assignment, and never converts automatic matches into enrollment.
Clears/forgets create durable overrides that later observation replays cannot undo.

After speaker cutover the tray must send identity mutations through the same
server command path (queue locally when offline), or become read-only for these
actions. Two independent writable identity masters are not supported. Preserve
the worker's current names/enrollment while disconnected; show pending changes.
Forgetting a profile may therefore remain pending until the worker acknowledges.

## Structured records

All mutable server rows have owner scope, stable opaque ID, integer revision,
server timestamps and optional deleted_at. ID mappings preserve old IDs; never
deduplicate by display name alone. New offline objects use client-generated UUIDs.

| Entity | Required domain fields |
|---|---|
| Todo | title/body, open/completed/dismissed state, due date or instant, timezone, snoozed-until, optional person/project, source references |
| Memory | kind (fact/decision/idea/event), current text, original extraction, proposed/confirmed/archived state, person/project links, evidence, revision history |
| Person | stable ID, display name, active/forgotten state; voice-enrollment status is a separate worker result |
| Observation | stable worker/source-scoped ID, anonymous label, desired and applied person IDs, confirmed/inferred provenance, embedding eligibility, clip metadata |
| Source | producer/job/window identity, source hash, known/unknown time, offsets, immutable excerpt, original attribution, relative archive reference |
| Clarification | target entity/revision, question, evidence, answer/dismissal, worker/export application status |
| Command | operation ID, device/producer identity, payload hash, base revision, desired action, saved/applied/failed state and safe error |

Keep confidence and evidence distinct from user confirmation. Every derived item
references its source; corrections append revisions and never falsify historical
evidence. Source excerpts are untrusted text, never executable instructions.
Extractor updates are proposals: they cannot overwrite edited text, reopen a
completed task, or revive a tombstone. Conflict/refinement decisions retain audit.

## API v1 draft

Implement a versioned OpenAPI contract and generated TypeScript types before UI
integration. Routes below are proposed names, not compatibility with existing APIs.

| Route | Contract |
|---|---|
| `POST /v1/pair` | Exchange short-lived single-use enrollment code for device-scoped credentials |
| `POST /v1/auth/refresh`, `POST /v1/auth/revoke` | Rotate refresh credentials / revoke device |
| `GET /v1/bootstrap` | Paginated consistent snapshot, snapshot ID and change-log watermark |
| `GET /v1/changes?cursor=...&limit=...` | Ordered owner-scoped upserts/tombstones and next cursor |
| `POST /v1/operations` | Bounded batch of desired-state commands, individual durable results |
| `GET /v1/sources/{id}` | Authorized source excerpt and archive metadata |
| `GET /v1/clips/{id}` | Authorized streamed audio; range support, content hash, private cache policy |
| `POST /v1/worker/events` | Idempotent source/item/observation/result imports from registered producer |
| `GET /v1/worker/commands?cursor=...` | Ordered command delivery to a specific worker |
| `POST /v1/worker/commands/{id}/result` | Idempotent worker application/failure acknowledgment |

Operation example (synthetic):

```json
{"operation_id":"00000000-0000-4000-8000-000000000001",
 "entity_type":"todo","entity_id":"00000000-0000-4000-8000-000000000002",
 "base_revision":7,"action":"set_state","payload":{"state":"completed"}}
```

Each result contains operation_id, applied/conflict/rejected outcome, current
entity/revision, and a stable error code. Speaker commands additionally expose
pending_worker/applied_worker/failed_worker. Use 401 for expired credentials, 403
for denied scope, 410 for expired cursor or removed media, 429 with Retry-After,
and bounded retriable 5xx. Per-operation conflicts in a batch are explicit results.

## Sync invariants

1. Phone writes entity overlay and operation outbox in one SQLite transaction.
   Only one in-flight operation per entity; later edits remain ordered behind it.
   Acknowledgment rebases unsent local edits on the returned revision. A crash
   never marks an unacknowledged edit synchronized.
2. Server commits entity revision, change-log entry, audit, worker/export outbox
   and operation receipt in one DB transaction. Repeating an operation ID with
   the same payload returns the same result; different payload is rejected.
   Receipts survive retry/device recovery, and old IDs are never silently reusable.
3. Revisions, not phone clocks, control concurrency. MVP uses whole-entity
   optimistic concurrency: stale edits conflict even for different fields.
   Identical desired-state replay is safe via receipts. Never implement completion
   as a toggle. Resolving a conflict emits a new operation against the latest revision.
4. Change cursors follow a committed ordered server log, scoped to account and
   server epoch. Do not use timestamps or a bare sequence allocation that can
   commit out of order: serialize log allocation/commit per owner. Paging has a
   fixed high-watermark, so concurrent writes are picked up on the next pull.
5. Bootstrap uses one consistent snapshot and matching watermark; pages do not
   mix database times. Apply pages into staging tables, then swap cache atomically.
   Preserve the pending outbox. Expired snapshots/cursors require a fresh snapshot,
   followed by revision-aware replay, not clearing unsynced changes.
6. Delete/archive events have tombstones. Retain tombstones/change history for a
   proposed 90-day offline window; clients older than retention must bootstrap.
   Compact only with explicit retention rules; imported sources retain dedup IDs
   so bootstrap or producer replay cannot resurrect a deleted item.
7. Sync on open/resume, after edits, on connectivity recovery and manual refresh.
   Persist retry state with exponential backoff/jitter. Background tasks/push may
   accelerate freshness but are never a correctness requirement. Do not spin when
   auth expires; retain edits and request reauthentication.
8. Device caches/cursors are bound to server instance and owner. Switching servers
   cannot upload old pending edits to the new endpoint. Sign-out warns about pending
   changes; explicit discard clears local records/audio/secrets. Revocation blocks
   future access but cannot remotely erase data from an offline device.

## Windows adapter and recovery

Add a durable outbound spool under private ProgramData; use HTTPS initiated by
the service, never inbound internet access to Windows. Configure an absolute
credential path readable only by the service/admin, separate from the Omi pairing
key. No interactive login, mapped drive or user PATH is required.

The existing databases cannot atomically commit with PostgreSQL. Use a local
outbox plus a reconciliation scan over immutable manifests, router results and
speaker records to recover missed exports. Derive immutable event identities from
producer/source/kind/revision and deduplicate at the server. Acknowledgment follows
durable server commit. A server outage does not stop capture, ASR, or vault output;
bound the spool and report storage pressure without silently dropping metadata.

Use the existing `SpeakerStore.command` behavior behind an adapter, with stable
UUID command IDs and ordered delivery. Persist adapter revision/precondition state;
current local commands do not supply network concurrency controls. After a crash
between local apply and result upload, retry reads the existing command receipt.
Do not send later commands for that observation/person until earlier effects are
resolved. Reject stale/deleted observations with a terminal result, not endless retry.
Initial scope is one producer for voice profiles; multi-worker enrollment is deferred.

Upload clips atomically with size/hash validation before advertising availability.
The current service retains review clips only until name/discard; copy eligible
clips into a bounded transfer spool before cleanup, or report them unavailable.
Retain server clips while pending review with a proposed 30-day cap, and delete
within 24 hours after resolved/discarded unless explicitly retained. Keep only
explicitly downloaded clips offline (proposed 100 MB LRU); sync deletion metadata
and clear cache on sign-out. These are proposed defaults to validate on-device.
Do not retain entire recordings merely to support mobile playback.

## Obsidian and reminders

Existing conversation/meeting notes remain the richer evidence archive, preserving
the current memory gate and meeting exceptions; filtered transcripts stay in the
private processing archive. Secondary storage does not imply publishing all chatter.
Frozen publication manifests and old named transcripts remain immutable.

Project structured state into new managed files/sections with stable IDs and an
export revision ledger. Compare the last exported content hash before replacement;
conflicting human edits stop that projection and create a visible conflict, while
mobile data remains usable. Prefer separate current-state overlays over mutating
historic notes. Server acceptance is independent of archive-export completion.
New clarification answers update canonical structured text and schedule a guarded
vault projection; existing legacy clarifications require an explicit ID mapping.

After todo cutover, `ToDos/Tasks.md` is a projection. Hermes/Telegram must use the
same API and scoped desired-state commands, or task-mutating callbacks/jobs must
be disabled. Invalidate old buttons; do not leave the Markdown housekeeping writer
active. Retain notification selection/schedule only once its canonical read path
has moved. Keep obsolete source-to-todo mappings and deletion/snooze state through
migration; extraction intake is not automatically an accepted task.

## Security and operations

Single owner is not unauthenticated. An administrator creates a short-lived pairing
code via server CLI; rate-limit exchanges and bind every device to the owner.
Use short-lived access tokens, rotating/revocable refresh tokens stored hashed
server-side and in phone secure storage. Worker credentials have producer-only
scopes; phone credentials cannot administer the host or upload arbitrary worker data.
Authorize every object/media request. Do not trust client-supplied owner IDs.

Require verified HTTPS; no certificate-validation bypass. Local development uses
synthetic data and a trusted development certificate. Keep secrets out of URLs,
logs, Git, public tray status and notification bodies. Redact content from telemetry.
Use OS data protection for phone DB/files, protected server volumes and encrypted
backups; verify platform configuration rather than assuming SQLite is encrypted.
This design trusts the selfhost server with plaintext content; it is not end-to-end
encryption. Embeddings and full raw audio remain on Windows.

Back up PostgreSQL, clip metadata/files, schema version and server identity
consistently. Test restoration before cutover. Restore increments a sync epoch so
devices bootstrap and reconcile outboxes rather than trusting vanished cursors.
Windows checkpoints/voice stores and vault require separate existing backups.
Track sync lag, worker command age, export conflicts, spool size, disk space,
backup age and last worker heartbeat without exposing content. Schema/API changes
must support the preceding mobile release or return an explicit upgrade-required
response that preserves pending edits. No production deployment occurs in this task.

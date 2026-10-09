# Implementation and migration plan

Planning only, 2026-10-09. Follow the current branch rule; do not merge/push main.

## Milestones and completion gates

### 0. Validate the mobile foundation on Mac

- Reconcile handoff provenance and inspect uncommitted integration availability.
- Try the intended Snow UI if its official source becomes reachable. Otherwise
  prototype the recommended Expo stack; pin toolchain/dependencies after proof.
- Use synthetic todos, people, excerpts and short audio. Build with local Xcode,
  then install a development build on a physical iPhone.
- Prove airplane-mode CRUD and app-kill persistence, clip playback/interruption,
  secure credentials, VoiceOver/Dynamic Type, and a minimal Android build path.
- Record selected framework, exact versions, tested OS/device, build instructions
  and remaining limitations. No production data or cloud build service needed.

Exit: a disposable or retained prototype demonstrates required native boundaries.

### 1. Server contract and first vertical slice

Proposed new layout (directories do not exist merely because this plan names them):

```text
apps/mobile/                  cross-platform app and local SQLite migrations
server/                       API, schema migrations, auth, change log, tests
contracts/                    versioned OpenAPI and synthetic fixtures
deploy/selfhost/               example Compose/proxy/backup instructions
src/second_brain/sync/         Windows adapter, durable spool, recovery scanner
```

- Implement auth/pairing, todo schema, idempotent operations, snapshot/bootstrap,
  revision conflicts and changes/tombstones. Generate client types from OpenAPI.
- Build local development server with persistent volumes and synthetic fixtures.
- Deliver todo create/edit/complete/reopen/snooze across two local clients.
- Build the phone sync engine before adding multiple feature screens; keep UI
  overlays separate from last acknowledged server state.

Exit: airplane-mode edits survive termination, reconnect once, and agree across
two devices; stale edits show a resolvable conflict. No vault/Windows dependency.

### 2. Read-only pipeline bridge

- Add opt-in outbound producer configuration and credentials without modifying
  receiver pairing, ACK timing, recording closure or v3 scoring/gating behavior.
- Export durable source, extracted item and speaker-observation events, with
  eligible review clips; recover missed events after crashes by reconciliation.
- Run shadow import only. Existing vault and tray stay authoritative during this
  stage. Phone can inspect evidence, inferred identities and worker freshness.
- Test with saved synthetic manifests, then a separately authorized small real
  dataset during deployment preparation. Treat model output as untrusted text.

Exit: replaying inputs yields one record per source identity; offline server
does not block Windows ingestion/publication; no full recording upload occurs.

### 3. Speaker and clarification round trip

- Implement server desired assignments and worker acknowledgment states, command
  ordering/preconditions, clip lifecycle and restart-safe local receipts.
- Phone supports assign/create person, rename, clear, discard and forget; preserve
  enrollment eligibility, model fingerprints and conservative matching semantics.
- Move tray identity writes to the shared path before enabling remote writes.
- Map clarification entries to structured entity IDs; answer/dismiss with revision
  checks and expose archive conflicts separately from successful canonical saves.

Exit: a phone assignment while Windows is offline remains visibly pending; worker
restart applies it once and reflects enrollment status accurately. Clear/forget
prevents subsequent replay from restoring the old reference. Old notes stay intact.

### 4. Memories, search and archive projections

- Add structured memory inbox, correction/archive/restore, evidence details and
  SQLite search. Preserve original extraction and user revisions separately.
- Add guarded deterministic vault projection, export ledger and visible conflicts.
- Do not rewrite curated prose, historic attribution or frozen publication output.

Exit: user edits survive extraction replay, manual vault conflicts preserve both
versions, and offline search returns cached records without querying the vault.

### 5. Controlled authority migration

Deploy only as a later explicitly scheduled task, after host details and backups
are known. Use an explicit migration ID and checkpoints; a restart resumes rather
than creating another import. First run is dry-run inventory and discrepancy report.

1. Inventory versioned Windows stores, router ledgers, clarification queue,
   canonical ToDos and reminder state; hash/snapshot inputs. Resolve duplicate IDs,
   Syncthing conflicts, missing files and unknown item mappings before cutover.
2. Import canonical todos including manually created items, completion, dismissals,
   snoozes, known dates, indented notes/subtasks and deletion tombstones. Preserve
   free prose in the vault; report unsupported structures instead of discarding them.
   Map old todo/router IDs to stable server IDs. Do not import every intake bullet
   as a new open task or infer completion from age.
3. Import routed memories from ledger/result evidence, not an indiscriminate scan
   of Markdown. Preserve source hashes and human corrections. Distinguish retained
   historical items from new extraction proposals. Record unresolved items for review.
4. Import people/observations with existing IDs and confirmed/inferred distinction;
   keep embeddings local. Import clarification answers and pending questions.
5. Compare counts, IDs, statuses, source links and representative rendered rows.
   Repeat import and prove no additional records/effects. Keep the app read-only.
6. Briefly freeze old task/identity writers, drain pending commands and import the
   final delta against the inventory. Switch each domain's authority using a
   recorded epoch; reject stale old-client writes after the switch.
7. Enable phone writes only after the tray adapter and Hermes/Telegram adapter
   use the same canonical API, or disable those legacy mutating paths. Stop old
   Markdown housekeeping writes and invalidate legacy callback buttons. Never
   run both reminder delivery paths concurrently.
8. Observe queue age, sync errors, mismatches and export conflicts through a pilot.
   Keep existing audio processing/vault publication active throughout. Expand
   backfill only after the pilot has passed acceptance.

Rollback before cutover simply discards the shadow environment. After live phone
writes, rollback must first stop new writers, drain or preserve pending commands,
export canonical changes with their IDs, and reconcile into legacy state before
restoring legacy writers. Never restore an old Markdown/DB backup over accepted
phone edits. If reconciliation cannot be proven, keep the app read-only and retain
both states for repair. Test this rollback with synthetic data before real migration.

### 6. Selfhost release and optional notifications

- Validate target CPU/OS, persistent volume ownership, trusted TLS endpoint,
  network reachability from iPhone, secret provisioning and automatic restarts.
- Verify full backup/restore including server epoch reset and offline phone replay.
- Validate a signed iPhone build and chosen private distribution/update workflow
  on Mac. Record provisioning requirements; do not assume a simulator proves install.
- Add local reminders or direct APNs only if needed. APNs is an Apple service even
  with a selfhosted API; payloads contain opaque change hints/counts, not memories.
  A denied notification permission or lost push must not impair sync correctness.

## Required verification matrix

| Boundary | Evidence required before release |
|---|---|
| Offline phone | Create/edit/complete, kill/reopen, reconnect; pending operations persist and converge |
| Concurrency | Two devices edit same revision; completion vs reopen; edit vs delete; clear visible conflict, no silent loss |
| Server durability | Crash before/after commit and before response; duplicate request replay; changed payload under same ID rejected |
| Paging | Writes during snapshot/paging; ordered commits; expired cursors; tombstones; bootstrap preserves pending edits |
| Worker | Server offline/restart, crash after speaker apply before receipt upload, repeated events, forgotten-person replay, local/remote command race |
| Media | Missing/expired clip, partial upload, hash mismatch, unauthorized/range reads, cache eviction, audio interruption |
| Identity | Scoped labels, inferred vs confirmed, ineligible clips, clear/forget/discard, audiobook exclusion; no automatic enrollment |
| Archive | Deterministic replay, manual edit conflict, backup, export failure independent of mobile acceptance, immutable prior attribution |
| Migration | Manual and extracted todos, completion/snooze/deletion, duplicate IDs, repeat import, legacy callback invalidation, rollback after live writes |
| Security | Revoked/expired tokens, object authorization, producer/device scope isolation, credential redaction, server switching, sign-out with pending work |
| Device UX | Physical iPhone offline/reconnect, VoiceOver, large text, dark mode, low-memory/relaunch and background suspension |
| Recovery | Restore server backup with new epoch while device has unsynced edits; prove no silent data loss or resurrection |

Run new server/mobile/adapter contract tests at their real persistence and HTTP
boundaries. Pipeline/worker modifications require `python scripts/test.py`;
Windows presenter/tray modifications require `windows/build.ps1` and applicable
live Windows checks. Preserve existing scoring/gating tests. Fixtures demonstrate
orchestration, never ASR recognition quality. Real matching/ASR claims need their
own authorized audio evaluation. CI additions apply only to these new components,
not the removed upstream products.

## Decisions deferred to implementation/deployment

Framework finalization follows milestone 0; the API remains framework-independent.
Choose target minimum iOS/Android versions and exact library versions on Mac.
Confirm selfhost resources and URL, distribution method, backup destination and
media retention before deployment. MVP assumes one owner, one Windows producer,
foreground sync, and no required push. These defaults permit development now
without guessing host credentials or interrupting the current service.

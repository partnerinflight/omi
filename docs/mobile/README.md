# Mobile-first Second Brain: Mac handoff

Date: 2026-10-09. Status: proposed design, no app/server implemented or deployed.

The owner's new direction is an iOS-first, cross-platform app connected to an
owner-hosted server. It becomes the primary place to manage todos, identify
speakers, and review/correct key facts and memories. Obsidian remains secondary,
with richer source notes and transcripts. Phone use must not require vault sync.

This explicitly supersedes the *future product direction* in SYSTEM.md that calls
the tray the intended replacement UI. Existing implementation descriptions and
durability rules still apply. Build a new companion product; do not restore the
removed upstream Omi cloud/mobile stack or its dependencies/CI gates.

Read in order:

1. [Product and framework decision](product-and-framework.md)
2. [Architecture and sync contract](architecture-and-sync.md)
3. [Implementation, migration, and acceptance plan](implementation-plan.md)

## Handoff provenance

Prepared on `feature/wifi-local-upload`, based on commit `593243808f` plus the
working-tree documentation and code reviewed on this date. The initial planning
commit excluded the pending speaker, router, clarification, firmware and Hermes
work. The owner subsequently requested that work be committed on the same branch;
pull the updated branch on the Mac to obtain it together with these plans.
Deployment and live validation status still differ by component; consult their
guides rather than assuming that a committed change is installed.

Relevant current boundaries:

- [SYSTEM](../../SYSTEM.md), [service architecture](../../src/second_brain/ARCHITECTURE.md):
  Windows owns durable ingestion, processing, queue/recovery, and publication.
- [Speaker review](../speakers.md), `src/second_brain/speakers.py`:
  private SQLite and idempotent local commands; scoped observations; only human
  confirmation enrolls clean voice references; publication freezes attribution.
- [Clarifications](../clarifications.md), `src/second_brain/clarifications.py`:
  human correction currently targets an unchanged managed vault entry with backup.
- `src/second_brain/router.py`: route queue and source-derived event IDs; it is
  not an editable mobile knowledge database. The v3 memory gate remains authoritative.
- `integrations/hermes/plugins/secondbrain-checklist/README.md`:
  canonical `ToDos/Tasks.md`, completion/snooze state, Telegram
  callbacks and reminder jobs require a coordinated authority change.

## Start here on the Mac

Read all three plans, inspect branch/worktree state, and begin milestone 0 with
synthetic data. Prove offline todo editing, clip playback, Keychain credentials,
and accessibility on a physical iPhone before committing to the framework.
Then implement the API contract and one end-to-end todo slice. Keep deployment,
live migration, real-data upload, firmware flashing, and production reminder
changes for the later deployment task. Selfhost host details are intentionally
unspecified; no credentials, private data, or infrastructure assumptions belong here.

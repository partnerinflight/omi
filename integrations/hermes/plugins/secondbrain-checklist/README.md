SecondBrain Checklist (staged Hermes plugin)

Purpose

This isolated plugin registers one public Hermes tool, secondbrain_checklist, and one narrowly scoped Telegram callback handler using ctx.register_platform_handler("telegram", factory). It never starts polling and updates only the canonical ToDos checklist; routed intake stays unchanged.

Status

This is the deployable source copy. Runtime activation on the configured Hermes
host was verified on 2026-10-08 after its Telegram group-ID correction and restart.
The installed skill and morning job now use interactive checklist delivery. See
`docs/router-quality.md` in the SecondBrain repository for deployment evidence.

Required private settings

Configure these only after review under plugins.entries.secondbrain-checklist.settings in the active profile's private config:

vault: /absolute/path/to/vault
chat_id: "<telegram-chat-id>"
owner_user_id: "<telegram-owner-user-id>"
timezone: America/Los_Angeles

The manifest intentionally has empty defaults for vault, chat_id, and owner_user_id. The tool returns a clear configuration error until all three are supplied. No bot token belongs in plugin settings or source. Sending uses the existing TELEGRAM_BOT_TOKEN already available to Hermes.

Store `chat_id` and `owner_user_id` as strings, matching the manifest. Hermes's
CLI can coerce individual numeric arguments to integers even when the shell
argument is quoted. Use `hermes config set` with a JSON object for this plugin's
settings, with the IDs as JSON strings, preserving every other existing setting.
Verify the loaded YAML types afterward. Integer values still work at runtime
because the plugin converts them, but produce schema warnings.

Tool contract

secondbrain_checklist accepts:

- action: sync, display, done, dismiss, snooze, or reopen
- router_ids: exact intake IDs for sync; canonical ToDos IDs for display/state actions after migration
- request_id: required for display; use a stable per-caller value. The morning cron should use its local calendar date, namespaced for that caller.
- until: future local YYYY-MM-DD for snooze

Display sends each task in a separate message with its own Done or Reopen button. A confirmed send returns sent=true and confirmed=true. Only scheduled cron runs may then emit [SILENT]; direct user questions must receive a normal acknowledgement. An empty list returns status=empty and sends nothing. A reserved or ambiguous request is never automatically retried with the same request_id.

Source and state rules

The source index accepts both native Obsidian comments (%% router:ID %%) and legacy HTML comments (<!-- router:ID -->) on Markdown list items. Unknown IDs, duplicate source IDs, duplicate selections, missing sources, and malformed state are rejected. Intake scans Decisions/*.md and Projects/_Tasks.md; after migration display and state changes use only ToDos/Tasks.md. Caller-controlled paths are not accepted.

Reminder state remains System/Reminders/state.json with version/items and every unrelated field preserved. Plugin delivery reservations live in the additive secondbrain_checklist top-level field. Writes use a process/thread lock, flock, fsync, temporary file, and atomic replace. Original routed notes are read-only. ToDos/Tasks.md is the authoritative editable checklist after migration.

Telegram security

Callback data is scoped to ^sbr:, below Telegram's 64-byte limit, and contains an unguessable random token plus explicit desired-state action and list slot. A callback is accepted only when actual Telegram user ID, chat ID, sent message ID, and stored token all match. Repeated desired-state clicks are idempotent. Foreign users/chats/messages and unknown or stale bindings are rejected.

Cron subprocess behavior

Every tool-driven delivery creates a short-lived PTB Bot solely for send_message using the inherited TELEGRAM_BOT_TOKEN. This avoids sharing a live gateway Bot across event loops or worker threads. Callback edits stay on the gateway loop through the received query. The plugin never calls getUpdates, runs polling, or creates a second consumer.

Tests

Run without enabling the plugin or sending network traffic:

<hermes-repo>/venv/bin/python -m unittest discover -s tests -t . -v

The plugin requires Linux/POSIX file locking. Repository Linux CI runs the suite
with `python-telegram-bot==22.5` and fake transport only. The real Plugin Doctor
registration test explicitly skips when `hermes_cli` is absent; it must also pass
inside the target Hermes environment before deployment. Other missing dependencies
remain failures.

The tests use a fake transport and cover rendering, confirmed receipts, done/reopen persistence, text actions, authorization, message binding, repeated/stale callbacks, legacy/native markers, unknown/ambiguous IDs, corrupt-state fail-closed behavior, concurrent writers, unrelated-state preservation, empty lists, ambiguous sends, and duplicate suppression. The registration test imports the installed Hermes Plugin Doctor and loads this staged directory through the real PluginContext contract while Doctor blocks network access.

Deployment requirements after review

1. Copy the reviewed directory to the active profile's plugins directory.
2. Add secondbrain-checklist to plugins.enabled and set the three private required settings plus timezone.
3. Ensure the existing Telegram dependency and TELEGRAM_BOT_TOKEN are available to gateway and cron subprocess environments.
4. Restart is required for plugin discovery, but this staged review deliberately performs no restart.
5. Update the SecondBrain skill and existing job to call secondbrain_checklist for both morning and interactive daily lists, and route all text state actions through it. Remove a pending-activation guard only after the callback handler is loaded. Do not create another cron job.
6. Before live use, perform one explicitly approved delivery test, then verify Done and Reopen persistence against ToDos/Tasks.md and its state.json cache.


## Canonical Obsidian ToDos

`sync` accepts explicitly selected meaningful intake IDs and promotes them to
`ToDos/Tasks.md`. `sync` with an empty list adopts manual checkboxes and returns all
canonical rows. Known due dates remain in the task text; missing dates are not
invented. Import has no three-item cap. Telegram display still selects at most three.
Existing rows, edits, completed states and deleted-task tombstones are preserved.
Done/Reopen modifies the native checkbox. JSON stores the cache and delivery ledger.
Markdown saves before the cache and repairs stale cache on the next operation.
Duplicate IDs, detected Syncthing conflicts, removed tasks, and stale buttons fail
closed. Local compare-before-replace catches overlapping editor changes; it is not
an atomic distributed transaction with Obsidian/Syncthing. Reconcile sync conflicts
before continuing. After migration a missing canonical file is an error.

Deploy all plugin files, run the plugin tests, and externally restart the gateway
before first `sync`. Do not migrate while an old JSON-only callback handler is live.
Existing pre-migration buttons are intentionally invalidated; request a fresh list.
Do not send a new Telegram test message without authorization.


## Per-task Telegram messages (0.3.0)

Each selected task is sent as its own message with its own Done/Reopen button
immediately below it. Telegram keyboards attach to messages, not text lines.
The delivery ledger binds each task slot to its own Telegram message ID. A failed
partial delivery does not resend confirmed cards automatically; already-confirmed
cards remain actionable. Legacy whole-list messages retain their prior bindings.

For interactive questions the agent must finish with a normal acknowledgement
(or a clear empty/error result). `[SILENT]` is only for the scheduled cron job after
confirmed delivery or a genuinely empty selection. Using it after a direct question
causes Hermes to emit its silence-marker error even when checklist sending succeeded.


## Regular housekeeping

`maintenance.py` and `../../scripts/scrub-todos.py` reorganize the canonical
checklist using its existing writer lock. Checked tasks move to `## Completed`
at the bottom; reopened tasks return to `## Tasks`. Indented task notes/subtasks
move with their parent, while free prose, due annotations and IDs are preserved.
Fenced examples are ignored and ambiguous duplicate/missing managed headings fail
closed. The existing hourly `second-brain-morning-gate.py` runs this helper afresh
on every tick, then wakes the reminder agent only at 06:00 Pacific. No extra cron,
Telegram message, or gateway restart is needed. The daily cleanup is deterministic;
it does not delete stale tasks or infer new completion states. Missing vaults,
sync conflicts and write failures cause the gate to fail rather than claim success.

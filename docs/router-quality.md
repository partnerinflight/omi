# Router quality and follow-through

The router extraction prompt now applies a usefulness threshold to every output
category. Daily summaries are consequential outcomes, not a conversational diary;
ordinary studying, practice, chores, and vague intentions are excluded regardless
of speaker identity. This changes router extraction only, not the authoritative
adaptive v3 conversation gate. Semantic quality still requires real Hermes review;
fixture tests do not demonstrate model compliance.

New entries use Obsidian native `%% ... %%` comments for replay IDs. These are
hidden in rendered notes; raw source mode still shows them. Legacy HTML markers
remain readable for deduplication and clarification. Run the backed-up
`scripts/clean-router-notes.py --vault <absolute-path>` migration with the service
stopped to convert existing simple managed bullets. Unrecognized human continuation
lines are preserved. `--remove <review.json>` now supports reviewed event IDs in
all categories, including Daily, instead of only Decisions. Removal is explicit;
the cleanup does not guess which existing prose is low value.

The deployable Hermes skill is at
`integrations/hermes/skills/second-brain-reminders/SKILL.md`. It prepares at most
three meaningful open follow-through items, with done/dismiss/snooze state separate
from historical notes. The Hermes gateway needs vault access, persistent state,
and a configured private phone channel. Repository creation does not install the
skill on a remote host or activate a schedule. Supply channel, morning time,
timezone, owner, and the vault path visible to that host during setup.

Selected delivery: the private Telegram group `SecondBrain`, daily at 06:00
America/Los_Angeles, following daylight saving changes. The configured authenticated endpoint at
`192.168.1.8:8642` was verified on 2026-10-06: it exposes server-side agent tools,
skills, and cron management. The credential remains in the installed private
SecondBrain configuration; no token belongs in this repository.

Deployment check on 2026-10-06: the skill is installed at
`/home/eugene/.hermes/skills/second-brain-reminders/SKILL.md`. Vault access is
verified at `/home/eugene/second-brain` (task file readable, six decision notes,
System writable). After the owner sent `/start@Polmiliclaw_bot`, Telegram verified
the SecondBrain group and bot membership. Job `71736297cea1` is enabled, with the
skill attached, explicit group delivery, vault working directory, and reply-session
context enabled. Its saved configuration was checked against the cron tool result.
The host scheduler ticks hourly (`0 * * * *`); the attached
`second-brain-morning-gate.py` allows the brief only at 06:00 America/Los_Angeles,
covering DST without changing the host timezone. Empty briefs use `[SILENT]`.
First delivery opportunity: 2026-10-07 at 06:00 PDT. The first hourly run was an
off-hours gate skip; its `ok` status is not evidence of a delivered reminder.

Follow-up verification on 2026-10-06: the owner authorized and received connection
test message 1457. Their direct reply reached session
`20261006_151010_52983d5b`; Hermes responded and the gateway logged
`content_delivered=true`. `/start` was treated as a platform ping rather than a
conversation. Syncthing reported the live vault idle, 574/574 files synchronized,
zero needed files, and no errors.

The basic group's explicit cron target cannot seed a cold user-isolated reply
session using `attach_to_session` alone. A prompt scoped only to SecondBrain was
saved with the supported `hermes config set telegram.channel_prompts` command,
instructing done/dismiss/snooze/reopen replies to load the reminder skill and
verify state persistence. Gateway restart is required to activate that binding.
The observed unit is the system service `hermes-gateway.service`. Hermes's
command-safety policy rejected scheduling its own restart; no restart timer was
created. Existing SSH credentials were not accepted, so activation remains
pending an external `sudo systemctl restart hermes-gateway.service` on that host.
Actual scheduled-brief delivery and done/snooze state changes remain untested.

## Tappable Telegram checklists

The isolated Linux Hermes plugin in
`integrations/hermes/plugins/secondbrain-checklist` uses the public plugin tool
and Telegram handler APIs; it does not modify Hermes core. Its Done buttons become
checked Reopen buttons. Daily questions and morning briefings share the existing
reminder state. Text state changes also use the plugin writer so button revisions
cannot overwrite newer decisions. Source notes remain read-only.

The plugin validates source IDs, owner/chat/message binding, and source snapshots;
it rejects stale changes and preserves unrelated state under an interprocess lock.
Delivery reservations prevent automatic duplicate sends after an ambiguous timeout.
The skill suppresses the normal cron output only after confirmed plugin delivery
or an empty selection. There is no second Telegram polling consumer.

Linux tests run in the Hermes environment with fake Telegram transport; the
plugin doctor checks the installed public registration contract. Windows parsing
checks do not exercise Linux file locks or prove live Telegram callbacks. Live
checklist delivery and tapping remain unverified until gateway activation and a
user-authorized end-to-end test. Keep the existing text morning delivery while
callback activation is pending.

Deployment on 2026-10-06: the reviewed plugin was copied to the Hermes profile,
enabled through `hermes plugins enable`, and configured for the existing private
group and owner. All 17 tests passed on the Linux Hermes host; plugin doctor
passed discovery, import, manifest, and registration. The installed reminder skill
and group prompt cover daily task questions as well as state replies. The skill
contains a pending-activation guard, and job `71736297cea1` keeps its existing text
delivery until activation is verified. After an external gateway restart, verify
the Telegram handler, remove that guard, and verify the existing job uses the
plugin with confirmed-delivery suppression. No extra live Telegram message was
sent during this implementation.

Follow-up on 2026-10-08: the October 7 and 8 06:00 Pacific runs both passed
the timezone gate and generated nonempty briefs. Telegram rejected delivery
because the original basic group migrated to a supergroup with a different chat
ID. Both the live-adapter and standalone fallback attempts used the obsolete ID.
Later off-hours skips replaced the visible last status with `ok`; that did not
mean either morning brief was delivered.

The existing job destination, private plugin chat setting, and group-specific
prompt were updated to the verified migration destination. No duplicate job was
created. The hourly schedule and Pacific timezone gate remain unchanged. The
running gateway had loaded the plugin (startup logged its native handler), but
its service object captured the original chat ID. A further operator restart is
needed to load the corrected destination for buttons. The installed skill and
job now distinguish this migration-related activation condition from initial
plugin installation and retain text delivery until it is verified. No additional
Telegram messages were sent in this repair. The next scheduled morning delivery
is October 9 at 06:00 America/Los_Angeles; actual delivery remains unverified.

Activation verified later on 2026-10-08: gateway PID 1137900 started at 10:27:27
Pacific after the relevant configuration update, and its 10:28:40 startup log
confirmed the checklist's native Telegram handler was wired. The current IDs
load as strings. Stale activation guards were removed from the installed skill;
the existing job prompt now calls the checklist tool with a date-stable request
ID and suppresses normal output only for confirmed delivery or a true empty
selection. The loaded group prompt already loads the updated skill. The plugin
is globally enabled and the cron job has no restrictive tool allowlist.

An empty-selection invocation returned `status=empty`, `sent=false`, and all
17 plugin tests passed using the Hermes interpreter and fake transport. No live
checklist was sent and no task completion state was changed in this verification;
actual button taps and the next morning delivery remain to be observed. No further
restart is required for this activation.

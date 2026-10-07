# Second Brain: agent handoff

Snapshot: 2026-09-29. Read this first, then [AGENTS.md](AGENTS.md).
Source code and installed configuration override examples; dated validation is
not proof of current machine health. This document distinguishes implemented
behavior, product intent, and unresolved work.

## Intent and scope

Turn wearable audio into useful, attributable, durable personal memory in an
Obsidian vault, without requiring an Omi cloud account or an interactive desktop
session. Capture offline, upload safely, transcribe/diarize locally, preserve
source evidence, and publish selectively. **Novelty is not durability**: ordinary
chatter stays archived; decisions, commitments, tasks, dates, project information,
reusable ideas, and durable personal facts should survive the memory gate.

The repository contains CV1 firmware, a local receiver, the imported adaptive-v3
pipeline, a Windows service, and a tray/speaker-review app. The owner explicitly
removed the old desktop/mobile/web/cloud products and SDK/plugin tooling. Do not
restore those dependencies. The tray is the intended replacement UI.

Current topology: Omi → Windows `192.168.1.85:7331` → local models → vault
`C:\Users\Eugene\SecondBrain`. Paths/host are user configuration, not constants;
the Windows checkout was reported at `F:\SecondBrain`. Hermes is an optional,
separately hosted scoring endpoint; its live availability is unverified.

## Implemented flow

```text
CV1 mic → PCM → Opus → sequence-numbered SD ring
  → authenticated Wi-Fi upload → durable .opus + metadata/checkpoint
  → closed-recording .ready receipt → SQLite job queue (one worker)
  → ffmpeg silence segmentation → MOSS ASR + anonymous speaker turns
  → conversation windows → heuristics + optional Hermes scoring
  → selective VibeVoice refinement → deterministic durable-memory gate
  → review clips / optional voice embeddings → speaker identity mapping
  → frozen publication manifest → additive Obsidian conversation notes
```

A successful upload, a closed recording, a completed processing job, and a
published note are different events. A completed job may publish zero notes if
all its windows are filtered.

## Firmware and transport

Sources: [firmware guide](omi/firmware/AGENTS.md),
[provisioning/protocol](omi/firmware/PROVISIONING.md),
[mic.c](omi/firmware/omi/src/mic.c),
[wifi_upload.c](omi/firmware/omi/src/wifi_upload.c).

- Hardware: Omi CV1, nRF5340 + nRF7002, T5838 microphone; NCS 2.9.0.
  Last recorded `.15` deployments: Windows on Sep 29 and Mac on Sep 30; see the firmware validation log.
- Capture: 16 kHz, 16-bit PCM; stereo PDM is averaged to mono and Opus encoded.
  SD stores 444-byte records, not independently playable files. Records contain
  a timestamp and packed complete Opus frames. A full ring drops new audio;
  unread records are not overwritten. Track dropped counts.
- VOX: `.15`: level measured
  after 250 Hz high-pass / 3.4 kHz low-pass, threshold **200**, continuous silence
  **30,000 ms**; the timer resets only when at least 3 of the last 5 100 ms blocks
  reach the threshold. This detects speech-band level, not speech.
  Trailing silence remains recorded. PCM/codec/packer drain before an
  explicit recording-end marker and acoustic sleep. Hardware acoustic wake
  (80 dB in `.15`, was 75 dB) has separate tuning, startup latency, and no pre-roll.
- Wake feedback: an 80 ms vibration only after at least 5 minutes of acoustic
  sleep; every mic restart discards 500 ms of PCM (startup transient + motor).
- Normal recording: LEDs off. Acoustic silence: solid red. Acoustic wake after
  5+ minutes asleep: 80 ms vibration. `.15`: every gesture is
  hold-then-release; release at 3–5 s toggles manual pause/resume;
  paused microphone rail and acoustic wake are off, red flashes 200 ms every
  3 seconds. Pause is not persistent across reboot. Other warning/setup/upload
  LED priorities still apply; manual pause overrides awake indications.
- Button, built `.15`: release at 10–15 s (2 pulses at 10 s) powers off; release at
  20 s or more (3 pulses at 20 s) enters setup (Wi-Fi build only; BLE-only has no
  setup). Short clicks and 5–10 s / 15–20 s releases do nothing.
  **The 20-second action opens setup; it is not a reboot.**
- Setup: temporary `OMI-Setup-XXXX`, HTTP `192.168.4.1`; configure SSID/password,
  receiver hostname/IPv4/port, and existing 64-hex-character pairing key. Portal
  expires after five minutes; configured boot does not expose it automatically.
- Automatic upload requires the charging signal, about one minute of queued
  audio, and a five-minute retry interval. BLE `upload-now` bypasses charging.
  From `.15` a started upload runs to completion: the
  CHG signal only reports current flow, so a full battery on the charger drops it
  and `.14` aborted uploads mid-transfer ("aborted (charger removed / busy)",
  seen live 2026-09-29 at 100 %/4215 mV). Only a Wi-Fi setup request interrupts.
  A full battery can still delay the *start* of an automatic upload.
  Recording and upload can coexist. One owner arbitrates BLE/Wi-Fi bulk access;
  BLE bulk reads defer acoustic sleep. Wi-Fi shuts down after its operation.
- Protocol: mutual HMAC-SHA256 challenge/response, sequence resume, durable ACK.
  Firmware advances its read pointer only for acknowledged/previously persisted
  records. Authentication is not payload encryption; use a trusted LAN.
- Wi-Fi credentials use Nordic Settings/NVS; destination/key use app settings.
  NVS ABI is pinned at `0xf8000`, size `0x2000`; credentials are not encrypted at
  rest. Preserve this layout and the raw-ring format. Stock LittleFS migration
  can destroy old recordings; prior consent concerned this owner's migration.
- Gain and VOX are separate. Saved gain default level 6 maps to register `0x3c`
  (**+10 dB**, despite inherited comments claiming +20 dB; see
  [Nordic gain definition](https://docs.nordicsemi.com/r/bundle/ps_nrf5340/page/pdm.html)). BLE setting UUID
  `19b10012-e8f2-537e-4f6c-d104768a1214` reads/writes the level. Gain changes affect
  VOX calibration; raising the silence threshold does not attenuate samples.
- RTC validity is observable over BLE; sync UTC after reboot. Unknown-clock
  recordings still retain sequence identity; do not invent wall-clock timestamps.

## Receiver, queue, and crash boundaries

Sources: [receiver library](omi/firmware/scripts/omi-local/omi_local/server.py),
[recording writer](omi/firmware/scripts/omi-local/omi_local/cli.py),
[service receiver](src/second_brain/receiver.py),
[queue](src/second_brain/queue.py), [runtime](src/second_brain/runtime.py).

1. Receiver writes original Opus packets into Ogg `.opus` files plus JSON
   metadata. It fsyncs audio and atomically checkpoints sequence/file length
   before ACK. Resume truncates any uncommitted tail. Pairing key and existing
   incoming directory must survive migration from standalone `omi-local serve`.
2. End markers close recordings; legacy timestamp/sequence boundaries also
   split them. Upload DONE only finishes a transfer snapshot. The final open
   recording may remain pending across multiple uploads.
3. Closed, checkpointed files produce `.ready/*.json` receipts. Startup recovers
   missing receipts from receiver state. Discovery enqueues each source once;
   job identity hashes device, start sequence, and audio SHA-256. Input hash is
   checked again before processing.
4. SQLite WAL is authoritative; `status.json` is a snapshot. States:
   `pending → processing → complete`, or `pending/retry_wait → failed` after
   exhausted attempts. One process owns the data-directory lock; one job runs
   at a time. Default: five attempts, exponential retry from 60 seconds, 4-hour
   deadline per processing attempt. Restart recovers interrupted jobs.
5. Each attempt has its own directory/config/log. Completed `manifest.json`
   avoids repeating ASR after publication failure. `publication-manifest.json`
   freezes speaker names/content so crash replay remains deterministic.
6. Receiver protocol v2 serves authenticated file clients (Mac meeting capture).
   Whole files land in `incoming/meetings/` only after a verified atomic commit
   (length and SHA-256); capture markers live in `.captures/` and completion
   receipts in `.ready/`. With `meetings_enabled`, discovery queues these receipts as ordinary jobs. The pipeline's
   `--meeting` mode transcribes L (mic) and R (meeting app) separately. A mic segment is kept as
   `owner_name` only where the mic is >= 6 dB louder than R in the 300-3400 Hz band; the rest is remote
   bleed, kept only in the manifest (`meeting.bleed_dropped`). R is diarized and voice-matched.
   Windows are scored as usual and the gate still decides routing, but every window is published in
   one meeting note (highlights first). 7B refinement is off for meetings because it works on a mono
   mix. Omi recordings overlapping a meeting are deduplicated
   after ASR and before windows. The service (`second_brain.meetings`) picks overlapping
   captures from the capture markers and passes the pipeline a written index
   (`--meeting-dedupe`); the pipeline never reads receiver layout. Alignment: each 60 s block of
   overlap, starting and ending 10 s inside it, is matched (50 ms speech-band RMS envelopes,
   Pearson) against the capture's L and R within +-10 s; a block needs a peak >= 0.7 to be
   trusted, and a meeting needs >= 50 s of overlap to try. Per-block offsets track clock drift.
   Per segment: only segments >= 2 s, scored within +-0.1 s of the nearest aligned block's
   offset; >= 0.6 drops it. `dedupe.dropped` in the manifest records deduped_by,
   deduped_channel, score, offset_seconds; `dedupe.alignments` records per-meeting status and
   blocks. A mic (L) match drops a segment only where the meeting job would publish that mic span
   as the owner (the same >= 6 dB rule); otherwise only an R match can drop it, so speech the
   meeting note files as bleed stays in the Omi note. No audio is deleted;
   moss_all_segments.json keeps every segment. Conversation windows never span dropped speech, so
   7B refinement can only restore dropped words inside a turn that straddles a window edge. Dedupe
   never fails a job:
   unreadable captures are skipped (status "unreadable" + a fallbacks entry); any other error
   keeps every segment. While an overlapping capture is `open` the job waits (stage
   `waiting-for-meeting`, rechecked every 5 min) without spending a retry; an open marker older
   than 24 h stops blocking, and an open capture is assumed to last at most 8 h. A recording whose
   device clock was never set (`first_timestamp` 0) is never deduplicated. Replays from a saved
   manifest never re-run dedupe or wait. The spec's per-segment +-10 s search was rejected:
   on the owner's real Omi recordings it falsely dropped 93% of unrelated 0.8 s segments (9% at 8 s).
   Directory fsync is a no-op on Windows, so after a power loss a rename the
   client was already told is committed can be lost; the client has then
   deleted its copy. Accepted platform limit, as for other receiver renames.
   The client is `mac/SecondBrainCapture` (menu-bar app; see its README). It opens a
   short connection per operation, sends CAPTURE_OPEN when a meeting starts, and uploads
   stereo Opus CAF (L = owner mic, R = meeting app) after the meeting ends.

## Adaptive processing and memory policy

Sources: [pipeline](src/second_brain/adaptive/pipeline.py),
[runners](src/second_brain/adaptive/runners),
[import provenance](docs/import-provenance.md). The supplied v3 policy and tests
are authoritative; explain deliberate policy changes.

- ffmpeg excludes long silence (defaults: −42 dB, ≥12 seconds); active spans
  become coarse chunks of at most 240 seconds. This is independent of Omi VOX.
- MOSS C++ runs first on every coarse chunk using the configured GGUF. The
  device is `moss_device` (default `cpu`; `cuda` with a `-Cuda` engine build,
  3–4.7x faster than CPU on a GTX 1660 Super, 2026-09-30). CPU uses
  `moss_threads`, default 16 (on a 32-core Threadripper 1.66x faster than 8 with
  identical output; 24-48 were no faster). The log names the backend actually
  used, since MOSS falls back to CPU silently. It consumes timestamps,
  transcript, and speaker fields from MOSS JSON. Diarization is built into MOSS;
  there is no separate diarization toggle. Missing labels become unknown.
- Speaker labels are scoped by chunk/window, never global identities. Group
  turns into conversation windows (gap 35 seconds, maximum 180 seconds).
- Heuristics score importance, uncertainty, durability, actionability, and
  retrieval value. Novelty uses lexical overlap with up to 4,000 vault notes;
  this is not a vector index. Vault entity names supply VibeVoice hotwords.
- VibeVoice ASR Streaming 7B refines selected windows with 12-second context
  padding. Default CPU/float32 can be expensive; device/dtype are configurable.
  `skip_vibe7` disables refinement, not MOSS diarization. Typical escalation:
  uncertainty ≥72; importance ≥86 with uncertainty ≥42; critical durable
  signals with importance ≥78 and uncertainty ≥30. Low-value rejected windows
  skip refinement; uncertainty ≥92 permits a rescue pass.
- Refinement outside the target window is excluded. VibeVoice timestamps are
  approximate. Missing/empty/error refinement results fall back to MOSS with
  recorded warnings; process-level failures can still fail the whole job.
- Final gate: fewer than ten words are rejected; windows below
  `memory_gate_min_importance` (default 0 = off) are rejected; durable signals
  pass; ephemeral categories normally fail. Filler "like"/"love" and a lone
  "I'll"/"we'll"/"let's" are not durable signals (2026-09-30: on 300 real windows
  these fillers had kept 53 conversations of chatter and a novel read aloud). Otherwise durability ≥58 and retrieval ≥55 pass,
  or importance ≥90 with retrieval ≥48 overrides. Novelty alone never keeps a
  window. Both publication flags must be true: `memory_keep` and
  `route_to_knowledge_router`.
- Every window retains a final transcript. Filtered windows get audit metadata;
  kept windows get `router_queue` artifacts. Audio of completed Omi recordings
  (original, sidecar, receipt, working WAVs) is deleted after publication when
  `delete_audio_after_processing` is on (default); text artifacts are kept, and a
  startup sweep completes interrupted deletions. Meeting captures are exempt until
  meeting processing/dedupe defines their retention. Legacy `router_*` threshold settings remain
  in the example but `should_route()` does not control the current final gate.

## Hermes: exact integration boundary

Implementation: `hermes_score()` in [pipeline.py](src/second_brain/adaptive/pipeline.py).

- Optional, **disabled by default**. Enable `hermes_scoring_enabled` in the
  installed pipeline config, set `hermes_url` to the full OpenAI-compatible
  `/v1/chat/completions` endpoint, and set `hermes_model`. `no_hermes` in service
  config overrides it. The `YOUR-PI-IP` example is a placeholder, not discovery.
- POST JSON with `model`, system/user `messages`, and `temperature: 0`; default
  request timeout 90 seconds. Optional bearer token comes from the private file
  `hermes_api_key_file` (preferred for the service: keep it in the ProgramData
  config folder) or else the environment variable named by `hermes_api_key_env`
  (default `HERMES_API_KEY`), which the service must inherit. The installer does
  not provision this secret. Never commit it.
- `hermes_scoring_mode`: `all` (v3) scores every window, and either source's
  durable signal keeps it. `borderline` asks Hermes only about windows the
  heuristic would keep with importance below `hermes_borderline_max_importance`
  (default 45), and Hermes's `memory_keep: false` then drops them; if Hermes fails,
  the heuristic decision stands. Before 2026-09-30 the prompt could not be built
  (unescaped braces), so no Hermes scoring had ever run.
- Request contains the timestamped anonymous-speaker MOSS transcript, heuristic
  scores, and up to three nearby vault note paths/excerpts (800 characters each).
  These data leave the Windows worker for the configured endpoint. This scorer
  receives text, not raw audio, and runs before human identity assignment.
- Response: `choices[0].message.content` containing JSON scores (importance,
  novelty, ASR uncertainty, durability, actionability, retrieval), categories,
  conversation type, durable `contains` flags, reasons, and `memory_keep`.
  Numerical scores are clamped to 0–100. Transport/parse errors use heuristics
  and record a fallback; an unset/placeholder URL silently disables the call.
- Blend heuristic/Hermes: importance and uncertainty 35/65, novelty 25/75,
  durability/actionability/retrieval 40/60. Durable flags are ORed. The
  deterministic gate decides publication; Hermes's `memory_keep` is advisory in
  mode `all` and a veto over the window in mode `borderline`.
  Final gating reuses first-pass Hermes scores; no second Hermes call follows
  VibeVoice refinement. Treat transcript/model text as untrusted input.
- **Knowledge router** ([router.py](src/second_brain/router.py)), ported on
  2026-09-30 from the standalone `C:\second-brain-router` (v4.1, previously run
  by hand on two test files only). With `router_enabled` (default false), every
  published window is queued in `data/router.sqlite3` and a separate service
  loop sends its named transcript to Hermes: extract facts/decisions/tasks/
  ideas/daily events, collapse duplicates within the batch, then reconcile
  against all router-written items (new/duplicate/refinement/conflict). Items
  are appended under `## Router Inbox` in People/Projects/Topics/Decisions/
  Ideas/Daily (curated prose is never rewritten), with a ledger and audit JSON
  in `System/Router`. Up to 3 Hermes calls per window (`router_timeout_seconds`,
  default 180). Failures back off (1 min ×4 per attempt, ≤6 h) and stop after 6
  attempts; `second-brain retry` requeues them. A routed source is skipped by
  its ledger, so replays cost no calls. Transcription never waits for routing.
  `no_hermes` disables it. Notes published before enabling are not backfilled.
  `router_queue/` remains a local audit artifact.
- Router bullets show readable content only; source IDs and confidence remain in
  `System/Router/Ledger`, and hidden event markers preserve replay deduplication.
  Decision extraction excludes fictional/media dialogue, routine transactions,
  incidental logistics, and suggestions that were never adopted. This narrows
  router extraction; the adaptive v3 conversation gate is unchanged.
  `scripts/clean-router-notes.py --vault <path> --remove <review.json>` cleans
  existing managed bullets and removes reviewed decision event IDs (a JSON map
  of IDs to reasons). It backs up changed notes and review reasons under
  `System/Router/Cleanup`, preserving curated prose and unmarked bullets.

## Speaker identity and Obsidian

Missing-context review: see [clarifications](docs/clarifications.md). The router
records questions for useful incomplete entries in private
`data/clarifications.sqlite3`; the tray's Needs clarification page uses the
existing review mailbox and `review/clarifications.json`. Only the pending count
appears in public status. Human corrections update the exact unchanged router
entry with a backup and survive crash replay; they never train a voice profile.
The extraction prompt requests full participant/context details when available,
and asks rather than inventing missing identities. Existing entries need explicit
registration; the Wednesday meeting example is registered during this update.

Sources: [speaker guide](docs/speakers.md), [speaker extraction](src/second_brain/speaker_audio.py),
[identity store](src/second_brain/speakers.py), [vault writer](src/second_brain/vault.py).

- Tray offers up to five clips per speaker observation, each ≤12 seconds; user
  assigns/renames/clears/forgets people through a restricted local mailbox.
- `speaker_python` + `speaker_model` enable offline SpeechBrain ECAPA embeddings.
  Null values still allow diarization/manual review, but no future voice matching.
  Only clean, nonoverlapping, precisely timed turns ≥2 seconds enroll; approximate
  VibeVoice turns remain playable/nameable but cannot become voice references.
- Only human-confirmed references train profiles. Matching compares the row's
  average voiceprint with each person's average: cosine ≥0.40 and runner-up
  margin ≥0.12 by default (calibrated on Omi audio 2026-09-29, 17/26 confirmed
  rows named, none wrongly; `scripts/speaker_calibration.py` re-checks).
  Thresholds live in **service config**. Model fingerprints prevent incompatible
  embeddings mixing. Ambiguous voices remain unknown; matching is not certainty.
- Confirmed observation averages also provide individual voice examples. A failed
  average match may be rescued at similarity ≥0.55, with the same best person and
  the configured margin over competing examples and averages. Only human-confirmed
  rows contribute; clearing/correcting them recomputes automatic assignments.
- SQLite identities survive restarts. Configure the encoder before processing;
  completed jobs are not automatically re-encoded when it becomes available.
- Vault output: `Omi/Conversations/<job-id>-<window-id>.md`, with source hash,
  recording time, audio link, offsets, engine, speaker provenance, transcript,
  and gate reason. Deterministic, atomic creation; identical replay is a no-op,
  conflicting human edits raise `NoteConflict`. No existing-note semantic edits.
  Later speaker corrections update review/future notes, not already published notes.

## Deployment and operations

### Upload recovery investigation (2026-10-04)

Obsidian date-note presentation: the repository router omits the redundant H1
for date-named notes, and the cleanup script removes an exact matching leading
date heading from Daily/Decisions notes with backups. Applied to 10 existing
notes on Oct 4; the running service still needs this renderer change at its next
service update (left running during backlog transfer).

Receiver logs show real Omi transfer timeouts on Oct 2, 3, and 4. The device
reported an explicit abort (-ECANCELED); before the `.16` deployment its active SMP digest was
`4b641c58cb02f8ab86c8221c5c8c64ae75ed86d9451575c457e7378ef3e92462`,
which differs from the verified `.15` image (`9f0f7d1a...`) despite GATT reporting
`.15`. That binary's abort cause has not been established.
Do not infer a charging abort from the legacy CLI's generic result label.

Source `.16` fixes a separate confirmed recovery gap: a started transfer gets up
to six retries for transient failures, five minutes apart from session end, even
if CHG becomes inactive when a docked battery fills. Retry tails below one minute
are eligible. Success, authentication/protocol rejection, or deliberate setup
abort ends those retries. Initial automatic uploads still require CHG and 600
queued records. No ACK/checkpoint or recording-close semantics change.
The extended BLE status reports failure stage and retries remaining. Receiver
diagnostic source changes include frame header/payload wait, phase, persisted
sequence, target, and packet count; that receiver wheel is not installed (the
administrator prompt was canceled). Native policy and socket timeout/resume
tests cover the change. On Oct 4, the user authorized `.16` deployment: SMP
verified application digest `cc174aa8...` active/confirmed, GATT reported `.16`,
and the existing network core was retained. Recordings and configuration survived,
with zero reported dropped packets. A manually initiated transfer resumed at the
receiver's durable checkpoint and advanced it. A real failure followed by an
automatic retry has not yet been observed. See the firmware validation log.

[README](README.md) contains commands; [Windows scripts](windows) implement them.
Defaults: [service config](config/service.example.json) and
[pipeline config](config/pipeline.example.json).

- Build with Python 3.12+, .NET 10 SDK, ffmpeg/ffprobe: install Python packages,
  run `python scripts/test.py`, then `windows/build.ps1`. Output `dist/windows`.
  Executables are self-contained; Python/model runtimes and weights are separate.
- Copy/configure pipeline example; reuse working MOSS runtime/model.
  `setup-engines.ps1` builds pinned MOSS source and downloads MOSS/VibeVoice
  weights; `setup-speakers.ps1` configures the separate speaker encoder.
  `setup.ps1` combines setup/build/test/install; see [fresh setup](docs/install-windows.md).
  [Import audit](docs/import-provenance.md) maps every original ZIP file.
- Elevated `install.ps1`: explicit machine Python, config, existing key, ffmpeg,
  local vault/incoming paths, and review user. Stop standalone receiver first
  (port conflict). Installs `SecondBrain` under `NT SERVICE\SecondBrain`, delayed
  auto-start/recovery, filesystem ACLs, Private/LocalSubnet TCP firewall rule.
- .NET supervises Python/model descendants in a Windows Job Object. Runs without
  login in Session 0. Tray registers at user login; closing it leaves service up.
  No mapped drives, GUI dependency, or runtime model downloads; HF offline mode
  is set. Obsidian need not run; separate vault sync may still require login.
- Installer copies configuration into ProgramData. Edit installed files, not
  just repo examples; restart service to reload. Reinstall for new engine paths
  so account ACLs are updated. Preserve keys/state/audio during upgrades.

Default machine state (`service.json` may override paths):

| Location under `C:\ProgramData\SecondBrain` | Meaning |
|---|---|
| `config/service.json`, `config/pipeline.json` | Service policy/paths versus model/gate/Hermes settings |
| `config/upload-secret.hex` | Existing device pairing key; private |
| `incoming/`, `.omi-local/state.json`, `devices/`, `.ready/` | Audio, receiver checkpoints, completion receipts |
| `incoming/meetings/` | v2 whole-file meeting captures (audio + JSON sidecar), committed after verification |
| `incoming/meetings/.captures/` | Per-capture open/closed/cancelled markers |
| `incoming/meetings/.ready/` | v2 commit receipts; queued when meetings_enabled |
| `data/queue.sqlite3`, `data/speakers.sqlite3` | Authoritative job and identity stores |
| `data/service.log` | Rotating receiver/discovery/worker errors |
| `data/jobs/<id>/attempt-N/pipeline.log`, `progress.json` | Detailed stage/model output; may contain transcripts |
| `data/jobs/<id>/manifest.json`, `publication-manifest.json` | Processing and publication recovery boundaries |
| `status/status.json` | Sanitized heartbeat, counts/current/recent/events, receiver status |
| `review/catalog.json`, `clips/`, `requests/`, `responses/` | Private speaker-review UI contract |

Logs/state are private; public status contains operational metadata. Tray marks
heartbeats stale after 15 seconds. Service-host errors also go to Windows
Application Event Log. Use installed Python `-m second_brain.cli` with `check`,
`status`, or `retry --config <service.json>`; retry requeues exhausted failed jobs.

## Verification, known gaps, and agent rules

- Verified code: `d103c03ec`: 29 pipeline tests pass on Windows. On branch
  `feature/meeting-capture` (receiver v2, 2026-09-29) all 147 tests (32 pipeline
  + 115 receiver/native) pass on macOS via `scripts/test.py`; Windows is not yet
  re-verified for that branch. With the Mac capture app (`feature/mac-capture`,
  2026-09-30), `scripts/test.py` also runs its 66 Swift tests on macOS, including
  an upload against the real receiver. Windows CI also verifies clean
  wheel installation, service/tray builds, and actual SCM Session 0 upload →
  fixture ASR → note → speaker naming → restart without duplicate notes or lost
  names. Pinned MOSS source builds on Windows. See [validation](docs/validation.md).
- Setup audit fixed two Windows failures: developer `PYTHONPATH` metadata made
  pip skip service wheels; inherited supervisor stdin stalled child Python
  startup before ASR. Installer/checks now use isolated Python and reinstall
  wheels; processing children receive `DEVNULL` stdin. Keep these boundaries.
- Active owner reports: processing exceeds deadline; repeated upload
  `IncompleteReadError`; original recordings sound very quiet except close speech.
  The service-startup stall above is reproduced/fixed in CI; the owner's exact
  timeout cause and real-model throughput still need validation. Inspect attempt
  logs/config before raising timeout. Interrupted sockets differ from processing
  timeouts; durable ACK/resume
  prevents deleting unacknowledged records. Diagnosed 2026-09-29 via
  `omi-local wifi-status`: interruptions were the firmware aborting automatic
  uploads when the CHG signal dropped on a full battery (fixed in `.15`,
  installed), plus one receiver-side `WinError 1450` on the sidecar rename (now
  retried). Records hold ~100 ms of audio; manual uploads moved ~50 records/s, about 5x real time.
- `.13` deployed; saved gain read back as level 6. Recent sample had low average
  level with full-scale peaks; no controlled speech comparison yet. Do not claim
  gain calibration, real ASR/diarization accuracy, or voice-match accuracy from
  fixture tests. Battery percentage/charging behavior also needs physical evidence:
  charging GPIO alone does not prove current flow; check voltage/sample age.
- Firmware build/sign/deploy evidence: [VALIDATION_MAC.md](omi/firmware/VALIDATION_MAC.md).
  Build with pinned SDK, bump both version strings for behavior changes, verify
  signed OTA ZIP; deployment requires user authorization. SDK caches/build outputs,
  recordings, credentials, transcripts, and weights stay outside Git.
- Meeting notes, known limits (2026-10-07): the 6 dB owner rule compares whole MOSS
  segments. A segment mixing owner speech and remote bleed is credited to the owner
  when the owner is loud (remote words then appear twice), and a short owner
  interjection inside long remote speech can be dropped as bleed. Headsets and clean
  turn-taking are unaffected. Everything on the mic channel is the owner, including
  other people in the room sharing that mic. For meetings the job result's `filtered`
  count means "not routed to Hermes"; those windows are still in the note. Real ASR on
  meeting audio, real voice matching of remote speakers, and Windows ffmpeg decoding
  of a real Mac CAF are unverified (fixtures only).
- Omi/meeting dedupe, known limits (2026-10-07): a meeting uploaded after its overlapping Omi
  recording was already processed leaves two notes; nothing reconciles them (owner accepted).
  Decisions are per ASR segment: a segment mixing meeting and room speech is kept or dropped
  whole, and segments < 2 s are never dropped (duplicate backchannels remain). Loud room speech
  through a whole block, or the owner silent in it, makes that block unusable; segments then use
  the nearest aligned block, or none. Thresholds (0.7 alignment, 0.6 drop) were measured on the
  owner's real Omi audio for false drops (0.7% at 2 s) and on simulated second-microphone audio
  for detection (80-87%); real Mac-mic vs Omi pairs are unverified. Meeting audio is never
  deleted, so incoming/meetings/ grows without bound; capture markers are re-read per job. The
  end-to-end dedupe tests turn the memory gate off (their fixture text is too short to pass it);
  dedupe with the gate on is covered only at the pipeline level. Room speech the Mac's mic also
  picked up matches the meeting's L channel and is dropped from the Omi note when the mic span is
  owner-dominant; it is then in the meeting note attributed to `owner_name` (the mic channel is the
  owner), not in the Omi note under its room speaker. Clock dependency: the Omi clock must be
  within +-10 s of the Mac's. When it is off by more, captures are either not selected as
  overlapping or fail alignment, and nothing is deduped (two notes; the private log warns
  "meeting dedupe could not align"). The Omi RTC is set only by the Bluetooth time-sync write
  (`omi-local time-sync`, and every `omi-local` connect unless `--no-time-sync`). On boot the
  firmware restores the epoch persisted at the last sync; only after a button power-off is the
  elapsed off-time added back (IMU timestamp), so other reboots leave the clock behind by the
  time since that sync. Between syncs the clock free-runs on device uptime and drifts. Keep the
  Omi clock synced.
- Mac encoder stereo separation, measured 2026-10-07 on this Mac with the production
  `CaptureEncoder` settings (16 kHz stereo Opus, 32 kbit/s): with one channel active the
  other sits about 43 dB down, so owner attribution is correct. Two simultaneous pure
  tones collapse almost completely (leakage 1.5-3.2 dB), while two simultaneous real
  speech sources kept an 10.9 dB channel margin versus 11.4 dB at 64 kbit/s. So
  low-bitrate joint-stereo coding can merge channels during overlapping talk, but real
  speech showed no material loss. Raising `bitRate` to 64000 removes the risk at double
  the size (about 29 MB/h); not changed, since the 32 kbit/s evidence is adequate.
- Keep changes on the feature branch; do not merge/push `main` without instruction.
  Preserve durability boundaries, v3 gates, edited notes, and third-party licenses.
  Add tests at real failure boundaries. Never present mocked model tests or a
  cross-compiled executable as proof of physical/Windows/model performance.

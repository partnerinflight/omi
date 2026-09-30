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
  Last installed/verified image: `3.0.22-localwifi.14`.
- Capture: 16 kHz, 16-bit PCM; stereo PDM is averaged to mono and Opus encoded.
  SD stores 444-byte records, not independently playable files. Records contain
  a timestamp and packed complete Opus frames. A full ring drops new audio;
  unread records are not overwritten. Track dropped counts.
- VOX: built `.15` (not yet installed; `.14` behavior differs): level measured
  after 250 Hz high-pass / 3.4 kHz low-pass, threshold **200**, continuous silence
  **30,000 ms**; the timer resets only when at least 3 of the last 5 100 ms blocks
  reach the threshold. This detects speech-band level, not speech.
  Trailing silence remains recorded. PCM/codec/packer drain before an
  explicit recording-end marker and acoustic sleep. Hardware acoustic wake
  (80 dB in `.15`, was 75 dB) has separate tuning, startup latency, and no pre-roll.
- Wake feedback: an 80 ms vibration only after at least 5 minutes of acoustic
  sleep; every mic restart discards 500 ms of PCM (startup transient + motor).
- Normal recording: LEDs off. Acoustic silence: solid red. Acoustic wake after
  5+ minutes asleep: 80 ms vibration. Built `.15` (not yet installed): every gesture is
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
   receipts in `.ready/`. The pipeline does not yet process these receipts.
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
  runner currently forces CPU, default eight threads. It consumes timestamps,
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
- Final gate: fewer than ten words are rejected; durable signals pass; ephemeral
  categories normally fail. Otherwise durability ≥58 and retrieval ≥55 pass,
  or importance ≥90 with retrieval ≥48 overrides. Novelty alone never keeps a
  window. Both publication flags must be true: `memory_keep` and
  `route_to_knowledge_router`.
- Every window retains a final transcript. Filtered windows get audit metadata;
  kept windows get `router_queue` artifacts. No automatic archive retention or
  deletion policy is implemented. Legacy `router_*` threshold settings remain
  in the example but `should_route()` does not control the current final gate.

## Hermes: exact integration boundary

Implementation: `hermes_score()` in [pipeline.py](src/second_brain/adaptive/pipeline.py).

- Optional, **disabled by default**. Enable `hermes_scoring_enabled` in the
  installed pipeline config, set `hermes_url` to the full OpenAI-compatible
  `/v1/chat/completions` endpoint, and set `hermes_model`. `no_hermes` in service
  config overrides it. The `YOUR-PI-IP` example is a placeholder, not discovery.
- POST JSON with `model`, system/user `messages`, and `temperature: 0`; default
  request timeout 90 seconds. Optional bearer token comes from the environment
  variable named by `hermes_api_key_env` (default `HERMES_API_KEY`). The Windows
  service must inherit it; a user's interactive shell variable is insufficient.
  The installer does not provision this secret. Never commit it.
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
  deterministic gate decides publication; Hermes's `memory_keep` is advisory.
  Final gating reuses first-pass Hermes scores; no second Hermes call follows
  VibeVoice refinement. Treat transcript/model text as untrusted input.
- **No downstream Hermes agent/Knowledge Router consumer is implemented here.**
  `router_queue` is a local output artifact, not a dispatched message. The
  current service directly writes approved windows to Obsidian. Semantic
  merging into people/projects/tasks and richer Hermes orchestration remain
  future work; the original ZIP's diagram is product intent, not a deployed API.

## Speaker identity and Obsidian

Sources: [speaker guide](docs/speakers.md), [speaker extraction](src/second_brain/speaker_audio.py),
[identity store](src/second_brain/speakers.py), [vault writer](src/second_brain/vault.py).

- Tray offers up to five clips per speaker observation, each ≤12 seconds; user
  assigns/renames/clears/forgets people through a restricted local mailbox.
- `speaker_python` + `speaker_model` enable offline SpeechBrain ECAPA embeddings.
  Null values still allow diarization/manual review, but no future voice matching.
  Only clean, nonoverlapping, precisely timed turns ≥2 seconds enroll; approximate
  VibeVoice turns remain playable/nameable but cannot become voice references.
- Only human-confirmed references train profiles. Matching requires at least two
  usable agreeing clips, cosine ≥0.80 and runner-up margin ≥0.10 by default.
  Thresholds live in **service config**. Model fingerprints prevent incompatible
  embeddings mixing. Ambiguous voices remain unknown; matching is not certainty.
- SQLite identities survive restarts. Configure the encoder before processing;
  completed jobs are not automatically re-encoded when it becomes available.
- Vault output: `Omi/Conversations/<job-id>-<window-id>.md`, with source hash,
  recording time, audio link, offsets, engine, speaker provenance, transcript,
  and gate reason. Deterministic, atomic creation; identical replay is a no-op,
  conflicting human edits raise `NoteConflict`. No existing-note semantic edits.
  Later speaker corrections update review/future notes, not already published notes.

## Deployment and operations

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
| `incoming/meetings/.ready/` | v2 commit receipts; not yet consumed by the pipeline |
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
  prevents deleting unacknowledged records, but repeated interruptions need diagnosis.
- `.13` deployed; saved gain read back as level 6. Recent sample had low average
  level with full-scale peaks; no controlled speech comparison yet. Do not claim
  gain calibration, real ASR/diarization accuracy, or voice-match accuracy from
  fixture tests. Battery percentage/charging behavior also needs physical evidence:
  charging GPIO alone does not prove current flow; check voltage/sample age.
- Firmware build/sign/deploy evidence: [VALIDATION_MAC.md](omi/firmware/VALIDATION_MAC.md).
  Build with pinned SDK, bump both version strings for behavior changes, verify
  signed OTA ZIP; deployment requires user authorization. SDK caches/build outputs,
  recordings, credentials, transcripts, and weights stay outside Git.
- Keep changes on the feature branch; do not merge/push `main` without instruction.
  Preserve durability boundaries, v3 gates, edited notes, and third-party licenses.
  Add tests at real failure boundaries. Never present mocked model tests or a
  cross-compiled executable as proof of physical/Windows/model performance.

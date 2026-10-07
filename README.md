# Second Brain — Omi to Obsidian

A local pipeline for one paired Omi and a Windows PC. The old Omi desktop,
mobile, cloud and web applications have been removed from this fork.

New agents: read [SYSTEM.md](SYSTEM.md) for the complete system contract,
implemented Hermes integration, operational boundaries, and known gaps.

For a fresh machine, use [single-checkout Windows setup](docs/install-windows.md).
`windows/setup.ps1` builds the app and MOSS, downloads configured engine/model
dependencies, verifies the installed packages, and installs the service.

```text
Omi CV1 → authenticated Wi-Fi upload :7331 → durable .opus recordings
  → SQLite queue → MOSS speech recognition + speaker labels
  → adaptive durable-memory scoring → selective VibeVoice refinement
  → final memory gate → source-linked Obsidian notes
                         ↘ filtered transcripts remain in the private archive
```

**SecondBrain** is a native Windows service with automatic delayed startup and
crash recovery. It supervises a Python worker and model subprocesses in a Windows
Job Object. It runs in Session 0 under a virtual service account, without login.
The separate **SecondBrain.Tray** app starts at user login. Hovering the icon
shows a compact status card; clicking it opens the Second Brain window with
**Overview** (health, receiver, queue, current stage, recent results),
**Speakers**, **People** and **Activity** pages. It follows the Windows light/dark
theme. Closing the window or exiting the tray leaves the service running. A stale
heartbeat is shown as stopped rather than falsely reporting idle.

The **Speakers** page lets you play snippets, replace anonymous labels with names,
and build local voice profiles for future recordings.
See [speaker review and setup](docs/speakers.md) for model setup and correction controls.

## Repository

| Path | Purpose |
|---|---|
| `omi/firmware/` | CV1 firmware, build tools, local receiver/BLE CLI and tests |
| `src/second_brain/adaptive/` | Imported v3 policy and MOSS/VibeVoice runners |
| `src/second_brain/` | Persistent queue, receiver integration, worker and vault writer |
| `windows/` | Native service, tray UI, installer, build and SCM smoke test |
| `config/` | Service/model configuration examples; no secrets |
| `tests/` | Policy, recovery, publication and full-loop contract tests |

The imported ZIP and modifications are recorded in [provenance](docs/import-provenance.md).
See [architecture](src/second_brain/ARCHITECTURE.md) for durability boundaries.

## Build on Windows

Requires Python 3.12+, .NET 10 SDK, and ffmpeg/ffprobe for tests/processing.
The resulting Windows x64 executables are self-contained; no .NET runtime is
needed on the target PC. Python and model runtimes are separate prerequisites.

```powershell
python -m pip install .\omi\firmware\scripts\omi-local .
python scripts\test.py
.\windows\build.ps1
```

The bundle is written to `dist\windows` (service, tray, Python wheels, install
scripts and examples). CI runs Linux receiver/native tests and a real Windows
Service Control Manager smoke test using synthetic audio and a deterministic
ASR fixture. Fixture success does **not** measure speech recognition accuracy.

## Configure the engines

Copy `config\pipeline.example.json` to a private configuration file, then set
MOSS binary/model paths and VibeVoice's environment/local model directory to
match the Windows machine. `windows\setup-engines.ps1 -Config <file>` builds
pinned MOSS source (or accepts `-MossBinaryDir`), downloads its Q5_K model, and
prepares VibeVoice's environment and checkpoint. Model weights are downloaded
outside Git before unattended service use; use a **local directory** for
`vibe_7b_model`. The service runs with Hugging Face offline mode enabled.

The imported policy uses MOSS for the first pass, optional Hermes scoring
(disabled by default), and selective VibeVoice 7B refinement. `-SkipVibe7` can
be used when installing an initial MOSS-only service. Model IDs, device, dtype,
thresholds, threads and hotwords remain configurable. The service's vault path
overrides the pipeline example's path. Its engine processes need no GUI.

## Install on the Windows receiver

Use an elevated PowerShell after building and configuring the engines. First
stop the old standalone `omi-local serve` process so port 7331 is available.
The installer refuses an occupied port; it never kills the old receiver.

```powershell
.\dist\windows\install.ps1 `
  -Python 'C:\Program Files\Python312\python.exe' `
  -PipelineConfig 'C:\SecondBrainConfig\pipeline.json' `
  -SecretFile "$env:USERPROFILE\.omi-local\upload-secret.hex" `
  -FfmpegDir 'C:\ffmpeg\bin' `
  -Vault 'C:\Users\Eugene\SecondBrain' `
  -IncomingDir 'D:\SecondBrain-Audio\incoming'
```

Replace paths with the actual installed paths. `IncomingDir` should be the
**same destination directory used by the existing receiver** to preserve resume
state and import completed recordings. It defaults to ProgramData for a fresh
install. Import the **existing pairing key**: the service must use the same key
already configured on Omi. The installer does not print it or generate a new
one. No firmware update/re-pairing is required for this service integration.

The installer creates `NT SERVICE\SecondBrain`, installs code under Program
Files and private state under `C:\ProgramData\SecondBrain`, grants read access
to the configured models/vault and modify access to `Omi\Conversations`, and
adds a TCP 7331 rule restricted to Private networks and LocalSubnet. It uses no
stored user password. Prefer machine-wide Python; Windows Store aliases and
mapped drive letters are unsuitable. The vault must be a local filesystem
folder available before login. Obsidian itself does not need to be open; any
separate sync client has its own login requirements.

In a normal (non-elevated) PowerShell for your own account:

```powershell
.\dist\windows\register-tray.ps1
```

The status card reads sanitized operational status. Speaker review separately
reads private snippets and submits naming commands through a user-restricted
local mailbox. It cannot read the pairing key or stop the service.

## Notes, archives and recovery

- Closed, checkpointed recordings are queued; a completed upload alone does
  not close a still-active recording. VOX/end markers remain authoritative.
- Durable windows create one deterministic Markdown note each under
  `Omi/Conversations`. Notes contain recording time, audio link, window offsets,
  engine, speaker-labelled transcript, content type and filtering reason.
- Anonymous speaker labels are scoped to chunks/windows. Confirmed voice profiles
  can attach names to future recordings with sufficiently strong evidence;
  ambiguous voices remain unidentified. VibeVoice's approximate timing cannot
  provide word-accurate boundaries or clean voice-enrollment samples.
- This is an additive vault writer, not a semantic merger into existing people,
  project or task notes. Existing notes are only read for novelty/hotwords.
- Filtered conversation remains in private job results and is not published.
- `meetings_enabled` (false): process Mac meeting captures from `incoming/meetings/` into one note
  each under `meetings_vault_folder` (default `Omi/Meetings`). The capture's microphone channel is
  attributed to `owner_name`; the meeting app's channel is diarized and voice-matched like Omi
  speakers. Meetings are never dropped by the memory gate, and meeting audio is retained.
  Captures uploaded before enabling are processed on the next scan.
  When an Omi recording overlaps a finished capture, the Omi segments the meeting already holds
  are dropped from its note; anything else the Omi heard (someone in the room) is kept. An Omi
  recording whose meeting is still uploading waits for it (up to 24 hours).
- **Audio is deleted after processing** (`delete_audio_after_processing`, on by
  default). Once a recording's note is published and its job is complete, the
  service deletes the original Omi recording, its sidecar and receipt, and all
  working audio (chunk/window WAVs). Transcripts, manifests and logs are kept;
  notes say "Audio deleted after processing" instead of linking a file. Failed or
  pending recordings keep their audio for retry, and speaker-review clips stay
  until you name or discard the speaker. A startup sweep finishes deletions
  interrupted by a crash and applies the policy to recordings completed earlier.
  Deleted audio cannot be reprocessed. Meeting captures are not deleted: planned
  Omi dedupe needs their audio. Set the option to `false` in `service.json` to
  keep audio (for example while debugging a recording).
- SQLite records jobs, attempts, errors and operations. Interrupted jobs resume
  after service restart. Failed jobs retry with backoff, then stay visible.
  A completed manifest is reused after a publication failure, avoiding another
  expensive model run. Notes are published atomically without replacing edits.
- A conflicting human-edited note causes a visible failure and remains intact.
  Resolve that conflict before retrying; the service never overwrites it.

Administrator commands:

```powershell
$python = 'C:\Program Files\SecondBrain\python\Scripts\python.exe'
$config = 'C:\ProgramData\SecondBrain\config\service.json'
& $python -m second_brain.cli check --config $config
& $python -m second_brain.cli status --config $config
& $python -m second_brain.cli retry --config $config
Restart-Service SecondBrain
```

Private service logs rotate under `ProgramData\SecondBrain\data`; each job's
attempt contains its own `pipeline.log`, configuration, transcripts and report.
Model output and transcripts never enter the public status file. Current upload
session counters reset when the worker restarts; job history persists.

`windows\uninstall.ps1` removes the service and its firewall rule while retaining
all recordings, keys, history, notes and binaries. Run `register-tray.ps1
-Unregister` as your normal user to disable tray autostart. For upgrades, stop
and uninstall the service, rebuild/reinstall with the same paths/key, then
restart the tray. No data directory should be deleted during an upgrade.

## Firmware

The last verified deployment is `3.0.22-localwifi.13`: 30-second VOX with PCM
threshold 300, brief wake vibration, button pause/resume, red paused flashes,
and a 20-second setup hold. See [Mac build](omi/firmware/MAC_BUILD.md),
[provisioning](omi/firmware/PROVISIONING.md), and
[validation history](omi/firmware/VALIDATION_MAC.md).

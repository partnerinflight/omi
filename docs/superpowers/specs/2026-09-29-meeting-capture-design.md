# Meeting capture and Omi dedupe — design

Date: 2026-09-29. Status: approved design, not yet implemented.

## Problem

With headphones on, Omi hears only the owner's side of a call. A Mac capture
app records the full meeting (owner mic + remote audio) and uploads it to the
Second Brain receiver. The pipeline publishes one meeting note per capture and
drops the Omi segments that duplicate the owner's side, keeping Omi speech that
is not part of the call (someone in the room).

The owner confirmed that sending meeting audio from this Mac to the Windows
receiver is acceptable under their policy and consent obligations. Browsers are
not captured by default because a browser tap records all of its tabs.

## Decisions

- Meetings happen on one Mac (macOS 14.4+; currently 26).
- Capture starts automatically when an **allowlisted app** uses the microphone.
- Audio goes to the existing Windows receiver over the LAN (approach A: a new
  file-upload protocol version on the existing receiver).
- Dedupe drops matching Omi speech per ASR segment and keeps the rest.
- Meeting notes bypass the ephemeral-conversation filter; scoring still runs and
  highlights decisions, tasks and dates.

## 1. Mac capture app (`mac/SecondBrainCapture/`)

Swift package, menu-bar only (no Dock icon), ad-hoc signed, launched at login
with `SMAppService`. Configuration is `config.json` in the app's Application
Support directory; there is no settings UI.

Components:

1. **MeetingDetector** — observes Core Audio's process list and
   `kAudioProcessPropertyIsRunningInput`. A capture starts when a process whose
   bundle ID is in `allowlist` holds input, and stops 20 s after the last
   allowlisted process releases it. Default allowlist: Amazon Chime, Zoom,
   Microsoft Teams, Slack, Webex.
2. **Recorder** — right channel: a Core Audio process tap on the detected app's
   processes only. Left channel: the input device that app is using
   (`kAudioProcessPropertyDevices`), falling back to the default input device. Both are resampled to
   16 kHz and written as stereo Opus in CAF using Apple's encoder. Data is
   flushed every 5 s. Start/end are recorded as wall-clock milliseconds plus a
   monotonic clock to detect gaps.
3. **Spool** — `~/Library/Application Support/SecondBrainCapture/spool/`,
   mode 0700. Each capture has `<id>.caf` and `<id>.json`: `capture_id`
   (16 random bytes, hex), `start_ms`, `end_ms`, `app` bundle ID,
   `channels: {"L": "mic", "R": "remote"}`, `complete`. A file is deleted only
   after the receiver replies `BYE committed`.
4. **Uploader** — sends `CAPTURE_OPEN` when recording starts (or when the
   receiver next becomes reachable), then uploads completed captures. Retries
   with exponential backoff from 30 s to a 15 min cap; the queue survives
   reboots. Uses `~/.omi-local/upload-secret.hex` and the receiver host/port
   from `config.json`.

Menu states: idle, recording (red), uploads pending (count), error. Actions:
"Skip this meeting" (stop, discard the local capture, send `CAPTURE_CANCEL`),
"Pause for 1 hour", "Open spool folder".

Permissions: Microphone and System Audio Recording Only. If either is missing
or the tap fails to start, nothing is recorded — never a silent mic-only
capture — and the menu and a notification name the problem.

Captures shorter than 60 s are discarded locally and cancelled at the receiver
(voice clips, quick tests). If the spool exceeds 5 GB recording continues and
the menu warns; nothing is deleted automatically. On launch, incomplete
captures left by a crash or sleep are finalized at their last flushed point and
uploaded.

Out of scope: in-app transcription, a settings UI, video, browser capture.

## 2. Receiver protocol v2

Version 1 (the Omi firmware protocol) is unchanged. v2 reuses framing
`[type:u8][len:u32][payload]`, the HELLO/CHALLENGE mutual HMAC handshake, and
the existing secret. In HELLO, `ver=2` identifies a file client; the 6-byte
client ID is derived from a hash of the Mac's hardware UUID. The v2 AUTH carries
only `client_tag:32`. Invalid tags receive `REJECT_AUTH`.

| Message | Direction | Payload | Receiver behavior |
|---|---|---|---|
| `CAPTURE_OPEN` 0x10 | C→S | capture_id:16, start_ms:u64, app (UTF-8, ≤256 B) | Durably write `.captures/<id>.json` state `open`; reply `OK` 0x18 |
| `CAPTURE_CANCEL` 0x11 | C→S | capture_id:16 | Set state `cancelled`; reply `OK` |
| `FILE_BEGIN` 0x12 | C→S | capture_id:16, total_len:u64, sha256:32, metadata JSON (≤4 KiB) | Reply `FILE_START` 0x15 offset:u64 = bytes already persisted in `<id>.partial` |
| `FILE_DATA` 0x13 | C→S | offset:u64, bytes (≤64 KiB) | Accept only at the expected offset; reply `ACK` 0x16 persisted_offset:u64 after fsync |
| `FILE_END` 0x14 | C→S | — | Verify length and SHA-256; atomically rename to `meetings/<id>.caf` with sidecar; publish ready receipt `source: meeting`; set state `closed`; reply `BYE` 0x17 committed:u8=1 |

Payload length limits are enforced before reading a payload. A wrong offset
returns `REJECT_PROTOCOL`. A hash mismatch deletes the partial and returns
`REJECT_PROTOCOL`; the client restarts that file from zero. Startup recovery
recreates missing ready receipts for committed meeting files.

Placement: wire handling in `omi_local/upload_protocol.py` and a separate v2
handler in `omi_local/server.py` behind a `FileSink` interface.
`second_brain/receiver.py` implements `FileSink` (placement, receipts, capture
state). The generic receiver has no pipeline knowledge.

## 3. Pipeline

Enabled by `meetings.enabled` in the service configuration.

### Meeting jobs (`source: meeting`)

- **L (mic)**: ASR only; segments are attributed to the owner's confirmed
  speaker identity. A segment counts only where L speech-band energy exceeds R
  by 6 dB, so remote audio bleeding into the mic is not attributed to the owner.
- **R (remote)**: ASR, diarization and voice matching through the existing
  speaker database and review flow.
- Segments are merged by time into one meeting note per capture: app, start/end,
  matched participants, full transcript. Existing scoring runs and its
  decisions/tasks/dates appear at the top. The ephemeral filter is not applied.
  Vault naming, idempotency and conflict rules are the existing ones.

### Omi jobs overlapping a meeting

After ASR and before conversation windows:

1. Find captures whose `[start_ms, end_ms]` overlaps the recording, ±10 s.
2. `open`: defer the job via `next_attempt` (stage `waiting-for-meeting`). An
   `open` capture older than 24 h becomes `expired`; Omi then proceeds normally.
3. `cancelled` or `expired`: process normally.
4. `closed`: for each Omi ASR segment, compute 50 ms speech-band (300–3400 Hz)
   RMS envelopes of the segment and of the meeting's L and R channels over the
   segment span ±10 s. If the normalized cross-correlation peak against L or R
   is ≥ 0.6, drop the segment. Rebuild windows from the remaining segments.
5. Dropped segments stay in the job manifest with `deduped_by: <capture_id>`,
   `channel` and `score`. No audio is deleted.
6. If no segment in an overlapping recording reaches a correlation peak ≥ 0.3
   anywhere within ±10 s, alignment is considered failed: keep all segments and
   set `alignment: failed` in the job result.

## 4. Operations

The tray status file adds meeting counts (open, waiting, closed) and, per job,
the number of deduped Omi segments. Rollout order: receiver v2 (v1 unaffected),
then pipeline dedupe behind the flag, then the Mac app. The Windows service is
upgraded with the existing `windows/install.ps1`. `SYSTEM.md` is updated for
this flow and for firmware `.14` (threshold 400).

## 5. Testing

- Receiver: real loopback v2 auth, v1 device and v2 client coexisting, resume
  after disconnect at arbitrary offsets, wrong-offset rejection, hash mismatch
  producing no receipt, receipt recovery after a crash between rename and
  receipt, cancel/open state persistence.
- Pipeline: synthetic stereo meeting and mono Omi fixtures with a 3.7 s clock
  offset — owner speech dropped, room speech kept; open → closed deferral,
  cancelled, 24 h expiry, alignment failure, L-vs-R bleed rule, identical output
  on replay.
- Mac app: unit tests for the detector state machine (start, 20 s release
  grace, short-capture discard), spool/sidecar crash recovery, and the uploader
  against a local receiver. Tap/mic capture on real hardware is a manual check.

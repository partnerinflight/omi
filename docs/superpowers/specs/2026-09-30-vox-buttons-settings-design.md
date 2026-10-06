# Speech-selective VOX, button gestures, and Mac settings window — design

Date: 2026-09-30. Status: approved design, not yet implemented.

Three owner-requested changes, split into two independent implementation plans:

- **Plan A (firmware `3.0.22-localwifi.15`)**: sections 1 and 2 — button gestures and
  speech-selective VOX. They ship together because both change device behavior and
  share one OTA image.
- **Plan B (Mac app)**: section 3 — the settings window.

Context: firmware `.14` is installed (VOX threshold 400 on raw amplitude, 30 s hold,
wake vibration only after 5 min of sleep, 500 ms discarded after each mic restart). The
Mac capture app is deployed and records; its receiver upload is currently blocked by a
local network policy issue unrelated to this design.

## 1. Button gestures (firmware)

Today a short click (40–999 ms) toggles pause, releasing between 3 and 5 s powers off,
and holding 20 s opens the Wi-Fi setup portal. Typing-adjacent pocket presses make the
short click too easy to trigger, and pause has no confirmation.

New map, one action per hold, always confirmed by vibration **while still holding**:

| Hold, then release | Vibration at threshold | Action on release |
|---|---|---|
| < 3 s | none | nothing |
| 3–5 s | 1 × 80 ms | pause / resume recording |
| 5–10 s | none | nothing |
| 10–15 s | 2 × 80 ms (120 ms apart) | power off |
| 15–20 s | none | nothing |
| ≥ 20 s | 3 × 80 ms (120 ms apart) | Wi-Fi setup portal |

- Each vibration fires when the hold crosses its threshold, so it always precedes the
  release that commits the action. Releasing in a dead band does nothing; the user can
  keep holding to reach the next window.
- Wi-Fi setup moves from "fires at 20 s while held" to "fires on release at ≥ 20 s",
  making every gesture release-triggered.
- **Resume order**: vibrate, wait for the motor to stop, then power the microphone rail
  and start the PDM. The existing 500 ms startup discard remains as a second layer, so
  motor noise cannot reach a recording.
- **Pause order**: the 3 s confirmation vibrates while the button is still held, so it
  (like the press itself) lands in the last second of the file that the pause then
  closes. No second vibration is played after the microphone stops.
- The red LED pulse every 3 s while paused is unchanged. A failed resume stays paused
  and keeps its error path.

Implementation notes: `button_hold.h` gains the new thresholds and a
`button_hold_feedback(ms)` returning how many pulses are due at a crossing, so hold
policy stays a pure header that native tests exercise directly. `button.c` tracks which
feedback level has already fired for the current press.

## 2. Speech-selective VOX (firmware)

`.14` measures the average absolute amplitude of raw PCM, so keystrokes (which are loud
but concentrated below 200 Hz) reset the silence timer and hold the microphone awake.
Measured on the owner's pulled recordings:

| Blocks that currently trip VOX | Energy < 200 Hz | Energy 300–3400 Hz | Zero-crossing rate |
|---|---|---|---|
| Speech sessions | 12–24 % | 66–75 % | 0.068–0.088 |
| Quiet sessions (typing, rumble) | 74–90 % | 6–24 % | 0.021–0.029 |

Approach: measure the level **inside the speech band** and require it to be
**sustained**, rather than raising the raw threshold again.

New module `src/lib/core/vox_filter.{c,h}` — pure C, no Zephyr dependencies, so the
existing native C test suite compiles it directly:

- Two cascaded second-order Butterworth sections in `float` (the nRF5340 has an FPU):
  high-pass at 250 Hz, then low-pass at 3400 Hz, coefficients fixed for 16 kHz mono.
  Filter state persists across blocks and is cleared on every microphone restart.
- `uint32_t vox_block_level(const int16_t *pcm, size_t frames)` — average absolute value
  of the filtered block.
- `bool vox_voice_detected(uint32_t level)` — pushes the block's verdict
  (`level >= CONFIG_OMI_VAD_ABS_THRESHOLD`) into a 5-entry ring and returns true when at
  least 3 of the last 5 blocks qualified. A single keystroke click cannot reset the
  silence timer; speech spanning ~300 ms can.

`mic.c` `aad_track_silence()` calls these instead of computing raw amplitude. The 30 s
silence hold, hardware acoustic wake, 500 ms startup discard and wake-vibration gate are
unchanged.

New Kconfig options (`CONFIG_OMI_VAD_ABS_THRESHOLD` is re-tuned because it now measures
the filtered signal):

- `CONFIG_OMI_VAD_SUSTAIN_BLOCKS` default 3
- `CONFIG_OMI_VAD_WINDOW_BLOCKS` default 5

**Threshold tuning before flashing.** A host tool
(`python -m omi_local.vox_replay`, which compiles the production filter via ctypes) compiles the production
`vox_filter.c` and replays the owner's pulled `.opus` recordings through it, reporting per
file: awake seconds under the current `.14` rule versus the new rule, and how much
speech-band activity each rule keeps. The threshold is chosen where speech files keep
essentially all speech and typing/rumble files stop holding the microphone awake. The
chosen number and the per-file table go into `VALIDATION_MAC.md`.

**Known limits, unchanged by this work.** Waking from hardware sleep is the T5838's own
75 dB acoustic detector, so loud typing can still wake the device; the difference is that
it returns to sleep after the 30 s hold instead of staying awake, and does not vibrate.
The owner chose to raise the T5838 mode-A wake threshold one step to 80 dB SPL (register
value 0x08; the driver's existing constants give 0x00 = 60 dB and 0x06 = 75 dB, i.e. 2.5 dB
per step), so typing wakes the microphone less often. This needs an on-device check because
the replay cannot model hardware wake.

**Tuning outcome (2026-09-30).** The original selection rule (typing/rumble total awake time
halved) was unreachable: every wake stays awake at least the 30 s hold, and most typing files
sit at that floor. Measured instead as awake time above the floor, threshold 200 halves the
typing/rumble excess (52% kept) while speech keeps 96% of its awake time (worst long session
86%, whose losses are pauses that hardware wake can reopen). A second high-pass section was
tried and rejected: it trades speech for typing at almost the same rate. The owner chose 200.

Native tests: a 100 Hz tone at keystroke level is rejected; simulated keystroke clicks
(single loud blocks) are rejected; a 1 kHz tone at speech level is accepted; 3 of 5
blocks reset the timer but 2 of 5 do not; filter state clears on restart.

## 3. Settings window (Mac app)

The app currently has no UI for configuration; `config.json` must be hand-edited and the
app restarted. This adds a small window so every field is editable in place.

- **Opening**: a `Settings…` item (⌘,) in the menu-bar menu opens a SwiftUI window hosted
  in an `NSWindow` (the app is `LSUIElement`, so it has no Dock icon or standard menu bar).
- **Fields**:
  - *Receiver* — host, port, and a **Test connection** button that runs the authenticated
    v2 handshake only (no capture, no file) and reports "Connected: receiver accepts
    meeting uploads", "Wrong secret", or the transport error.
  - *Pairing secret* — path with a `Choose…` file picker; a status line reports
    "valid (32 bytes)" or the parse error.
  - *Meeting apps* — the allowlist as an editable list with add/remove, plus
    `Add running app…` listing bundle IDs Core Audio currently reports, so the owner need
    not type them.
  - *Capture* — minimum meeting length (s), end-of-call grace (s), spool warning size (GB).
- **Validation**: host non-empty; port 1–65535; minimum length, grace and spool size
  positive. `Save` is disabled while any field is invalid, and each error shows inline.
- **Applying**: `Save` writes `config.json` atomically and the running app applies it
  immediately. The upload client is rebuilt with the new host/port/secret; the detector
  picks up allowlist and grace changes at the next capture; a capture already recording is
  never cut short.
- **Code layout**: `SettingsModel` (edit state, validation, diffing against the saved
  `Config`) lives in `CaptureCore` with unit tests. `SettingsWindow.swift` in the app
  target is thin SwiftUI. `CaptureController` gains `apply(_ config: Config)`.
- **Out of scope**: launch-at-login toggle, theming, advanced receiver options.

Tests: validation accepts and rejects the boundary values above; the model reports which
subsystems a change affects (uploader versus detector); saving round-trips through
`Config.load`; `apply` swaps the upload client without disturbing an active capture.

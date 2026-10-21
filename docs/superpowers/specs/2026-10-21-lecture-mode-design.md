# Lecture mode — design

Date: 2026-10-21. Status: approved design, not yet implemented.

Owner request: a recorder mode for classes and lectures. Triple-clicking the button
toggles it. Audio is recorded as usual, but the service transcribes it verbatim and keeps
the whole transcript next to a summary in the vault, instead of letting the memory gate
reduce it to selected windows.

Owner decisions:

- Entry and exit are confirmed by a double vibration.
- Solid yellow LED while a lecture is recording.
- Lecture mode exits automatically after 4 hours.
- Transcript and summary are two linked notes.
- Lecture audio is deleted after publication, like other Omi audio.
- Lecture content stays in the lectures folder: no knowledge router, no daily notes.

Context: firmware `3.0.22-localwifi.19` is prepared. Every action today is
hold-then-release; short clicks only send BLE tap notifications. Short-click pause was
removed in `.15` because pocket presses triggered it (see
`2026-09-30-vox-buttons-settings-design.md`). A tight triple-click is much harder to
trigger by accident, and a false entry is visible (yellow) and harmless (audio is still
recorded and still published, only as a lecture).

## 1. Firmware

### Gesture

- **Triple-click:** three presses, each released within 300 ms (`TAP_THRESHOLD`), with at
  most 600 ms between a release and the next press.
- A fourth tap within the window cancels the gesture: no action.
- Single- and double-tap BLE notifications are delayed until the tap window closes, so a
  triple-click never emits a double-tap first. Nothing in this fork uses those
  notifications.
- Hold gestures (pause, power off, setup) are unchanged. A press that turns into a hold
  cancels the tap count.

### Mode state

- The mic owner thread changes the mode, following the manual-pause pattern. The button
  handler only flips an atomic and gives `aad_sem`.
- **Entering lecture mode:**
  1. Drain and write an end marker for the current recording, if one is open.
  2. Write a mode marker `lecture`.
  3. Play `play_haptic_pulses(2)`.
  4. If the mic is paused or in acoustic sleep, start it first. The 500 ms startup
     discard still applies.
- **Exiting lecture mode:**
  1. Write an end marker.
  2. Write a mode marker `normal`.
  3. Play `play_haptic_pulses(2)`.
  4. Normal VOX resumes.
- **While lecture mode is on:**
  - The VOX silence timer never requests acoustic sleep, so one lecture is one recording.
  - Manual pause still works and ends the recording. Resuming stays in lecture mode and
    writes the lecture marker again before audio.
- **Auto-exit:** after `CONFIG_OMI_LECTURE_MAX_MS` (default 4 h) the device exits exactly
  as if triple-clicked, with the double vibration.
- **Not persistent:** a reboot or power-off returns to normal mode.
- **Vibration in the audio:** the confirmation vibration falls at the end of the previous
  recording (entry) or the end of the lecture (exit). Accepted; it is not discarded.

### Mode marker record

- A 444-byte record: the 4-byte timestamp, then a payload of `"\0OMIMOD\1"`, a mode byte
  (0 = normal, 1 = lecture), and zero padding.
- It starts with `0x00`, so receivers older than this change treat it as an empty record
  and ignore it.
- It must not reuse `"\0OMIEND\1"` with trailing data: today's receiver rejects that and
  would stall the upload.
- The end marker is unchanged.
- The SD ring format and upload protocol v1 are unchanged.

### LED

- Lecture recording shows **solid yellow** (red + green).
- It sits in the "recording" slot of `set_led_state()`, below the setup, transfer, pause,
  SD-full and RTC warnings. Warnings and pause feedback stay visible during a lecture.
- Use a low PWM level so 4 hours of solid LED has a small battery cost.

### Version and tests

- Bump `CONFIG_BT_DIS_FW_REV_STR` to `3.0.22-localwifi.20` in `omi.conf` and
  `overlay-wifi-upload.conf`.
- Native C tests:
  - Triple-click accept and reject timings, the fourth-tap cancel, a hold after taps,
    delayed single/double-tap notifications.
  - Marker order on entry and exit (end marker, then mode marker).
  - VOX sleep suppressed while in lecture mode.
  - Pause and resume inside lecture mode.
  - Auto-exit.
  - Entry from paused and from acoustic sleep.
  - Every lecture state in the LED selector.
- Update firmware `AGENTS.md` and the setup portal help.

## 2. Receiver (`scripts/omi-local`)

- `protocol.py` parses the mode marker as a marker record.
- `SessionWriter`:
  - A mode marker closes any open recording and sets the mode for the next one.
  - The sidecar JSON gets `"mode": "normal" | "lecture"`, and the Ogg file gets an
    `OMI_MODE` comment.
  - The mode is saved in the open-file checkpoint, so it survives resume across uploads.
  - With no marker, the mode is `normal`, so older firmware keeps working.
- The BLE `pull` path uses the same writer and gets the same behavior.
- Tests: marker parsing, the forced boundary, resume across upload sessions, legacy
  records with no marker.

## 3. Service

### Job kind

- Store the job kind (`omi`, `meeting` or `lecture`) explicitly, through one
  `job_kind(metadata)` helper.
- This replaces the scattered `source == "meeting"` checks in `runtime`, `speakers`,
  `bridge`, `meetings` and `speaker_audio`.
- Audio retention is unchanged: lecture audio is purged after publication.

### Pipeline (`--lecture`)

- Force the MOSS tier: no VibeVoice refinement. This keeps the text verbatim and keeps a
  90-minute lecture within the 4-hour attempt deadline.
- No per-window Hermes scoring and no meeting dedupe.
- Keep windowing and speaker turns; they become timestamped transcript sections.
- `final_transcript` and `final_segments` are still set, because publication reads them.
- Before shipping, check `moss_max_new` (2048 tokens per 240 s chunk) on dense lecture
  speech. If it truncates, raise it or use shorter chunks for lectures.

### Vault

- New config key `lectures_vault_folder`, default `Omi/Lectures`. Add it to
  `config.py`, `service.example.json`, and the installer's defaults and folder ACL grant.
- **Transcript note** `<job>.md`:
  - Written as soon as the job completes.
  - Frontmatter `type: omi-lecture`.
  - A link to the summary note.
  - `## Transcript` with a `### m:ss` heading per section and speaker-labelled lines.
  - The memory gate never filters it. Recordings with no speech publish nothing, like
    meetings.
- **Summary note** `<job> Summary.md`:
  - Frontmatter `type: omi-lecture-summary`.
  - A link back to the transcript note.
  - Produced through `router.chat` in two steps: summarize each transcript section, then
    combine the section summaries.
  - The result is frozen in the job directory as `lecture-summary.json` before the note is
    written, so crash replays are byte-identical.
  - It is retried like routing; a Hermes outage only delays the summary.
  - With Hermes disabled, no summary is produced and the transcript note's link stays
    unresolved.
- Lectures are never sent to the knowledge router and never produce daily notes.

### Tray

- Add `kind` to the queue snapshot and the tray's `JobInfo`, and show lecture jobs with a
  badge. `kind` is metadata only, never content.
- Add `lecture` to the bridge export `kind`.

### Tests

- Mirror `tests/test_meetings.py`:
  - The pipeline run with `--lecture --no-hermes`.
  - Note templates, idempotent republish, `NoteConflict` on a human edit.
  - Summary freezing and replay with a fake Hermes.
  - End to end: upload, exactly one lecture note pair, nothing in Conversations, nothing
    routed.
  - Config validation.

## 4. Rollout

1. Receiver and service first. An old receiver ignores the marker safely but loses the
   lecture tag.
2. Firmware `.20` afterwards. Flashing waits for the owner's explicit request.
3. Update `SYSTEM.md`, `ARCHITECTURE.md`, `README.md` and the gesture docs with the code.

## Not validated by fixture tests

- Real MOSS behavior on long, dense lecture speech, including truncation.
- How readable the summary is.
- Triple-click reliability on the real button.
- The yellow LED's color and brightness on hardware.

These need a recorded lecture and the device.

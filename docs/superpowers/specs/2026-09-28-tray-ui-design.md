# Tray app UI redesign

Date: 2026-09-28 · Status: approved design, pending spec review

## Goal

Replace the tray's plain-text hover card and the separate WinForms speaker
window with one larger, native-looking app window that makes status readable at a
glance and makes speaker identification fast. The user asked for: a larger
window, real UI instead of plain text, and room for speaker identification.

## Constraints (unchanged by this work)

- The tray is an optional, per-user process. Quitting it never stops the service.
- `status.json` stays operational metadata only (no transcripts, paths, secrets).
  The service, `status.json` schema, and speaker request/response protocol are
  not changed. No new listeners, network access, or ACL changes.
- Private clip audio is never copied outside `ProgramData\SecondBrain\review`
  (no temp files).
- No new NuGet packages. Output stays a self-contained `win-x64` publish.

## Technology

- `SecondBrain.Tray` becomes a WPF app (`UseWPF` + `UseWindowsForms`) targeting
  `net10.0-windows`, using WPF's built-in Fluent theme (`ThemeMode="System"`,
  `PresentationFramework.Fluent`, verified present in Microsoft.WindowsDesktop.App
  10.0.12). It follows Windows light/dark mode and the accent color. If the SDK
  flags `ThemeMode` as experimental (WPF0001), that single diagnostic is
  suppressed in the project file.
- The tray icon stays `System.Windows.Forms.NotifyIcon` (WPF has none).
- Semantic colors (healthy / attention / stopped) are defined once as resources
  with light and dark variants; everything else comes from the Fluent theme.

## Components

UI-free logic moves to `SecondBrain.Status` (net10.0) so the existing console
test project covers it. The tray project contains only views and glue.

### `SecondBrain.Status` (library)

| Type | Responsibility |
|---|---|
| `ServiceStatus` | Immutable parsed `status.json`: health (`Running`, `Attention`, `Stopped`, `Unavailable`), stage, time in stage, counts (pending / processing / complete / failed), receiver (listening, port, active uploads, sessions ok/failed, error), discovery error, speaker summary (unidentified, people), events, recent jobs. `Read(path, now)` / `Parse(json, now)`; stale heartbeat (>15 s) or `service != "running"` → `Stopped`; unreadable → `Unavailable`. Missing optional sections default safely. |
| `Snapshot` | Kept for the tray icon: `Title`, `Tooltip` (≤127 chars), `Healthy`, now derived from `ServiceStatus`. The multi-line `Details` text is removed (no remaining consumer). |
| `SpeakerCatalog` | Parsed `review/catalog.json` (people, speakers, clips, heartbeat, `Live` = heartbeat < 15 s). Derived views: `Recordings` (speakers grouped by `job`, ordered newest first, with unidentified count), `PersonSummaries` (per person: confirmed rows, matched rows, distinct recordings), `NextUnidentified(afterId)` (next unidentified row in display order, wrapping, or null). |
| `ReviewClient` | Given the review directory: `LoadCatalog()`, `Send(action, observation, person, name)` → request id, writing `requests/<id>.json` via `CreateNew` temp file + flush + move (same JSON shape as today: `id, action, observation, person, name`), `TryReadResponse(id)`. `ValidateName` mirrors the service rule (1–80 chars after
trimming, no control characters, none of `[]<>\|`) so bad names get immediate
feedback; the service remains authoritative. Validates clip file names (`*.wav`, no path components) before returning a clip path. |
| `WavInfo` | Reads duration from a PCM WAV header (RIFF/`fmt `/`data` chunks) for playback progress; invalid header → falls back to clip `end - start`. |

### `SecondBrain.Tray` (WPF app)

- `App` — single-instance mutex (unchanged name), status path argument (unchanged),
  owns the `NotifyIcon`, a 1 s status timer, the hover card and the main window.
  Left-click opens the main window; hover shows the card; right-click menu:
  **Open Second Brain**, **Review speakers**, **Windows Services**, separator,
  **Quit status app (service continues)**. Icon: information vs warning, as now.
- `HoverCard` — borderless topmost popup (~360 px wide) near the cursor: health
  dot + title, stage, pending / processing / failed, "N speakers to identify".
  Same hide-after-5 s-unless-hovered behavior as today.
- `MainWindow` — default 1100×760, minimum 900×620, resizable, shown in the
  taskbar while open. Closing hides it. Size/position/last page persisted in
  `%LOCALAPPDATA%\SecondBrain\tray.json` (best effort; clamped to a visible
  screen). Left navigation: **Overview**, **Speakers** (badge = unidentified
  count), **People**, **Activity**; footer shows service state.
- Pages are `UserControl`s bound to small view-model classes refreshed from the
  timer (status every 1 s; catalog every 2 s, re-rendered only when changed,
  preserving selection, scroll, and the name being typed).

## Pages

### Overview
- Health banner: Running / Needs attention (failed jobs, receiver not listening,
  or discovery error, with the reason) / Stopped / Unavailable (with the existing
  guidance text).
- Stat cards: Receiver (listening on port, active uploads, sessions ok/failed),
  Queue (pending, processing), Completed, Failed.
- "Now" card: current stage shown in a stage strip — segmentation → transcribing
  → scoring → refining → routing → publishing → speakers (unknown stages such as
  `starting` shown as text before the strip) — with an indeterminate busy
  indicator and time in stage; "Idle" / "Waiting / retrying" when no job runs.
  No percentage (the service does not publish one).
- Speakers card: "N speakers to identify · M people saved" with a **Review
  speakers** button.
- Last 5 events (time, kind, detail).

### Speakers
- Left pane: toggle **Unidentified / All**, search (name or recording date,
  Ctrl+F), list grouped by recording (expander header: recorded date, speaker
  count, unidentified count; expanded by default).
- Right pane for the selected row: title, state chip (confirmed; voice-matched
  with score; unidentified), embedding status note, clips list (each: play/stop
  button, progress bar, `mm:ss–mm:ss`, quality tag, transcript text), name
  combo box with autocomplete over saved people, **Assign** (default button),
  **Clear assignment**.
- Quick naming: in Unidentified mode, after an assignment is acknowledged the
  selection moves to `NextUnidentified`.
- Keys: Space play/stop selected clip, ↑/↓ between clips (when the clip list has
  focus), Enter assign, Ctrl+F search.
- Status line: queued / applying / saved / service error text / service offline
  (changes queued) — same states as today.

### People
- List of people: name, confirmed count, matched count, recordings.
- People with no remaining speaker rows are still listed (counts of zero).
- **Rename** (inline text box + Save) and **Forget** (confirmation dialog, same
  wording as today). Both use the existing `rename` / `forget` requests, which
  the service keys on `person` only (`observation` is sent as null).
- Selecting a person lists their speaker rows; **Open** jumps to that row on the
  Speakers page (switching to All if needed).

### Activity
- Recent events table (time, kind, detail) and recent jobs table (short id — first
  8 chars, state, stage, attempts, error, updated).
- Caption: "The service publishes the 10 most recent events and jobs."

## Playback

Play/stop from memory with `SoundPlayer` (as today): only one clip plays at a
time; progress is a timer against `WavInfo` duration; selecting another row,
leaving the page or closing the window stops playback. No pause/seek — the
alternatives need a temp copy of private audio or a new audio dependency.

## Error handling

- Unreadable/stale status → Stopped/Unavailable UI; the app never crashes on bad
  JSON (all parsing goes through the library with the same exception filters as
  today).
- Unreadable catalog → Speakers/People show the existing "Speaker review is
  unavailable…" guidance; last good catalog stays visible if one was loaded.
- Request write failure → status line message; nothing is marked pending.
- One pending request at a time (as today); actions are disabled while pending.

## Testing

- `SecondBrain.Status.Tests` (console, throw-on-failure, as today), split into
  focused files:
  - `ServiceStatus`: live, stale heartbeat, stopped, missing file, failed jobs →
    Attention, receiver offline, discovery error, missing optional sections,
    unknown stage, tooltip ≤127 chars.
  - `SpeakerCatalog`: grouping/order, unidentified counts, person summaries,
    `NextUnidentified` ordering and wrap, `Live`.
  - `ReviewClient`: request JSON shape matches the Python service's expected keys,
    atomic write (no `.tmp` left), response read, clip name validation rejects
    paths/non-wav, `ValidateName` accepts/rejects the same cases as the service.
  - `WavInfo`: valid header duration, truncated/invalid header fallback.
- `windows/build.ps1` runs the tests and publishes the tray (unchanged commands).
- Manual verification: launch the published tray against the live status and
  catalog, screenshot each page in light and dark mode, exercise assign/next and
  playback. Recorded in the commit message; not claimed as automated coverage.

## Docs

- `docs/speakers.md`: new entry points (tray click / **Review speakers**), quick
  naming flow, keyboard shortcuts, People page for rename/forget, play/stop.
- `README.md`: tray section describes the window and pages.

## Out of scope

Retrying failed jobs from the UI (needs service-side privileges), log viewing,
full job history, pause/seek, waveform display, service control buttons.

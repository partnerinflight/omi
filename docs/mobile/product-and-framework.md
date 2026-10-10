# Product and framework decision

Proposed 2026-10-09; implementation starts on the Mac.

## Product contract

One owner, multiple devices, iOS first; retain an Android path through shared
code. The app works against a user-configured HTTPS server, with durable local
reads/writes and visible sync state. Windows remains the audio/ASR worker and
the tray remains useful for local diagnostics and fallback review during rollout.

MVP navigation:

| Surface | Essential interactions |
|---|---|
| Today / Todos | Inbox, open, due, snoozed, completed; create, edit, complete, reopen, dismiss; explicit optional due date and owner |
| Review | Unknown speakers and missing-context questions; play short clip, choose/create person, confirm or skip; answer/dismiss clarification |
| Memories | Search curated facts, decisions and memories; filter by person/project/date; view evidence; correct, archive, restore |
| People | Stable identities, confirmed vs inferred assignments, rename, clear observation, forget profile with explicit consequence confirmation |
| Settings | Server/account connection, queued changes/conflicts, last successful sync, worker freshness, cache controls, sign out/revoke device |

Task creation never infers a deadline from a vague intention. Keep source text
and normalized date separately; date-only values stay calendar dates with an
IANA timezone for reminders. Render unknown recording time as unknown. Distinguish
an extracted suggestion from a user-confirmed commitment and an inferred speaker
from a human-confirmed identity. Manual items may have no recording source.

Speaker review is a short one-handed loop: listen, choose person, confirm, next.
Offer up to the existing five clips of at most 12 seconds; do not autoplay a
backlog. Clearing and skipping are different; skip leaves the item for later.
Discard/forget must explain loss of references and retained historical attribution.
Current `[AudioBook]` exclusion semantics remain available; do not silently turn
that name convention into a different policy during UI migration.

Every content detail can show source time, excerpt, confidence/provenance and an
archive reference. A desktop filesystem path is not a phone link: provide a
server source-detail screen and optional relative vault reference for desktop use.
Cache selected source excerpts; full archives remain optional, authenticated,
on-demand future work. No full-vault replication is required for mobile search.

Offline edits display immediately with a queued badge, persist across app kills,
and become synchronized only after acknowledgment. Conflicts offer local/server
values and explicit resolution. Missing/expired clips show a reason and allow
text review. Search operates on cached structured records. Empty, loading,
offline, expired-auth, stale-worker and failed-export states must be distinct.
Support Dynamic Type, VoiceOver, dark mode, large touch targets and undo for
ordinary task actions. Audio interruptions/headphones must work on a real phone.

Not MVP: on-phone ASR/voice embedding, wearable BLE capture/provisioning, a full
Obsidian editor, multi-user collaboration, semantic chat, desktop UI replacement,
automatic rewriting of old transcripts, or migration of Windows inference to Linux.
Notifications can follow the core workflow; correct foreground sync cannot
depend on receiving push or getting an iOS background execution slot.

## Framework recommendation

Use **React Native + Expo + TypeScript**, with Expo Router, SQLite for records
and an operation outbox, SecureStore for credentials, and a supported Expo audio
playback module. Pin compatible stable versions at implementation time and
commit lockfiles. Use native controls and a small owned design-token layer first.
Choose specific UI dependencies after the device spike, not from screenshots.
Local Xcode/development builds are supported; hosted EAS builds, Expo push, and
OTA hosting are optional and not required by this design.

This is an engineering recommendation, not a benchmark result: the MVP is mostly
lists, forms, short audio and offline state, so sharing UI/business logic while
retaining native integration is a good fit. SQLite supplies persistence, not
automatic synchronization; the explicit sync contract is in the architecture plan.

| Candidate | Fit / tradeoff | Decision |
|---|---|---|
| React Native + Expo | Shared iOS/Android code and local native builds; native modules still need device validation | Recommended provisional default |
| Flutter | Official iOS/Android and desktop support; adds Dart and a separate UI ecosystem | Strong fallback if prototype or developer preference favors it |
| SwiftUI | Direct Apple integration; separate Android UI needed later | Choose only if cross-platform priority changes |
| Tauri 2 (mobile) | iOS UI is a WKWebView with a Rust core; mobile less mature than desktop; audio session, Keychain and SQLite need plugins or Swift glue | Rejected: web UI without React Native's native controls or modules |
| Snow UI (`snow-ui-rs`) | Pure-Rust 0.0.1 (Apache-2.0); README reports working desktop/web only; no audio, SQLite, secure-storage or accessibility support | Rejected for MVP; recheck once an iOS backend ships |

### Rust UI investigation (updated 2026-10-10)

The supplied URL resolves to [Snow UI](https://snow-ui.dev/)
([repo](https://github.com/snow-ui-rs/snow-ui)), a pure-Rust declarative UI
framework on Tokio, Apache-2.0. On 2026-10-10: crate 0.0.1, repo created
2026-01-06, 3 contributors, 26 stars. Its README states "It works, initially,
on desktop and web"; iOS/Android are listed targets but not demonstrated.
Widgets are Board, Card, Row, Text, Button, TextInput, Switch and Form. It has no
audio playback/session, SQLite, Keychain or VoiceOver/Dynamic Type support, so it
cannot run the milestone 0 spike. Revisit only after a working iOS backend ships.

Tauri 2 mobile renders the UI in WKWebView with Rust commands behind it. Mobile is
less mature than desktop, and the required native boundaries would need plugins
or custom Swift. Other Rust UIs either self-render (weak accessibility) or are
also WebViews (Dioxus mobile). Rust brings no shared-code benefit here: the
server is Python and heavy inference stays on Windows. Decision: proceed with
the Expo stack in milestone 0.

### Primary references checked 2026-10-09

- [Expo local builds](https://docs.expo.dev/guides/local-app-development/):
  Xcode/local CLI workflow; `npx expo run:ios` builds locally on Mac.
- [Expo local-first guide](https://docs.expo.dev/guides/local-first/):
  SQLite persistence and the need for an additional sync solution.
- [Expo SecureStore](https://docs.expo.dev/versions/latest/sdk/securestore/):
  credential storage API; confirm reinstall/backup semantics in the prototype.
- [Expo background tasks](https://docs.expo.dev/versions/latest/sdk/background-task/):
  OS-scheduled execution is opportunistic; foreground sync is the baseline.
- [Flutter supported platforms](https://docs.flutter.dev/reference/supported-platforms):
  official support matrix; recheck minimum OS versions before pinning a toolchain.

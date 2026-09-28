# Validation evidence

The supplied adaptive-v3 policy tests are preserved. The shared runner executes
all policy/service and receiver/native firmware tests. End-to-end contract tests
send authenticated Omi-protocol frames, create real playable Ogg Opus, run ffmpeg
and the production adaptive pipeline with a deterministic MOSS substitute, then
publish and replay an Obsidian note in a temporary vault. They also exercise bad
keys, engine failure/retry, process locks, edited-note conflicts, and crash
checkpoint recovery. These do not establish real-model accuracy.

Both self-contained Windows x64 applications were cross-compiled on macOS with
.NET SDK 10.0.401. The shared status presenter has executable tests. Windows SCM
and installation checks are in `windows/smoke-test.ps1` and the Windows CI job;
live-device, real-model and interactive tray checks are reported separately.

No firmware was deployed by this integration work. No user's existing notes or
recordings are used as test data. Repository cleanup was explicitly approved by
the user; old products remain recoverable from the parent Git history.

Local result on 2026-09-28: 18 pipeline/policy/integration tests and 72
receiver/native-firmware tests pass. The .NET status executable tests pass,
and both Windows x64 self-contained publishes succeed. Firmware source and
images are unchanged by this integration. Actual Windows SCM/interactive tests
are the remaining platform validation.

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

Speaker review adds tests for persistent enrollment, multi-clip agreement,
ambiguous/unknown voices, model-version separation, correction and reference
revocation, request replay, private playable WAV clips and name propagation into
future publication. Existing-note preservation is verified after assigning a name.
The Windows smoke test now also submits a naming request and checks it after a
fresh service restart. Real interactive Windows playback remains unverified.
The expanded suite passes 27 policy/service/speaker tests and 72 receiver/native
tests (99 total); the updated Windows tray cross-compiles successfully.

The real SpeechBrain 1.0.3 / Torch 2.8.0 ECAPA runner was exercised offline on
macOS with two public SpeechBrain test clips. It produced two finite,
unit-normalized 192-dimensional embeddings using local weights, fingerprint
`24df09c705c014a81c5e3c4ea0d5a8f88d978bc27c6b1ee2aeb05c06fc4c0ac4`.
This verifies loading/extraction, not Omi identity accuracy or the default
matching threshold. Test audio, model weights and embeddings remain outside Git.

Windows lock regression (2026-09-28): the second worker read byte zero before
attempting its nonblocking lock. Windows rejects reads through a competing
handle, so this escaped as PermissionError and leaked the handle. Acquisition
now locks without reading or initializing the file (Windows supports locking
past EOF). Tests cover new/legacy files, contention cleanup, reacquisition and
separate-process exclusion/release. All 28 pipeline and 72 receiver/native tests
pass locally on macOS. Pipeline tests now also run in Windows CI, alongside
its existing service smoke test; local success does not establish Windows SCM
success. The earlier Windows CI run at bf996910a failed starting the smoke
service, separately from the reported lock-test failure.

Single-checkout setup audit (2026-09-29): original ZIP SHA-256 verified; all
original pipeline/runner/policy-test definitions remain present and the original
README is byte-identical. All 100 component tests pass. New PowerShell boundary
tests pass on macOS with real temporary Git repositories: repeat setup, dirty
source protection, config preservation/backup, and native command failure.
PowerShell parser checks pass. A clean venv outside the checkout installs only
the main wheel, resolves omi-local from the bundle and declared dependencies,
passes pip check, and runs synthetic audio through installed segmentation,
fixture ASR, memory gate, and review-clip creation. This is packaging validation,
not a real-model test. Engine source/model revisions are pinned; source/model
metadata was verified against upstream. Full multi-GB VibeVoice download and
fresh Windows engine/service setup still require Windows execution evidence.

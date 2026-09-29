# Adaptive audio import

Source supplied by the user: `second-brain-adaptive-audio-v3.zip`.
SHA-256: `6fe44bb6f3b9676f2e947c9392eb4eea457fc9cc9258f41f042f7d45692ca546`.

`adaptive_pipeline.py` is now `src/second_brain/adaptive/pipeline.py` and its
runners are adjacent. The original README is preserved for provenance;
use the root README for current installation. The original policy tests run
under unittest. Service integration adds explicit output directories,
progress events and bounded subprocesses, validates ASR results, and preserves
speaker/time provenance. No model weights, secrets, or recordings are imported.

The starting firmware/receiver commit is
`a5667242b8dfa8bc77dc1c8336cfc7db67169d74`.

The user explicitly approved removal of the obsolete desktop/mobile/cloud/web
products, plugins/SDKs and their CI tooling (12,632 tracked files in the initial
cleanup selection). Focused local/Windows CI and agent guidance replace those
product-specific workflows. New service code is outside the firmware build tree.

## Completeness audit — 2026-09-29

All eight entries of the supplied ZIP are accounted for:

| Original entry | Current repository implementation |
|---|---|
| `adaptive_pipeline.py` | `src/second_brain/adaptive/pipeline.py` |
| `runners/moss_cpp_runner.py` | `src/second_brain/adaptive/runners/moss_cpp_runner.py` |
| `runners/vibe_batch_runner.py` | `src/second_brain/adaptive/runners/vibe_batch_runner.py` |
| `tests/test_policy.py` | `tests/test_adaptive_policy.py` (original cases retained) |
| `config.example.json` | `config/pipeline.example.json` |
| `setup_engines.ps1` | `windows/setup-engines.ps1` + pinned build/download helpers |
| `run_adaptive.ps1` | packaged module entry point + `windows/run-audio.ps1` |
| `README.md` | `docs/adaptive-v3-original.md` (historical); current README/install guide |

Queue, receiver integration, identity memory, Windows service/tray, and Obsidian
writer are added in this repo. Hermes scoring client is in `pipeline.py`; the
external Hermes server and a downstream semantic Knowledge Router are not
included. Third-party engines, runtimes and weights are explicitly provisioned
by setup, not bundled source/model payloads. No executable dependency on the
original ZIP, Downloads folder, or an old personal pipeline checkout remains.
See [single-checkout setup](install-windows.md).

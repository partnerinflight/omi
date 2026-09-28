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

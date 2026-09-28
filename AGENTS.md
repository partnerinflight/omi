# Second Brain local pipeline — agent guide

This fork is now a local Omi-to-Obsidian product. The old upstream desktop,
mobile, web, cloud backend and deployment tooling were removed at the user's
explicit request. Do not reintroduce cloud dependencies or their old CI gates.

- Keep changes on the current feature branch. Never push to or merge `main`
  without explicit instruction. Preserve existing uncommitted user work.
- Firmware: read `omi/firmware/AGENTS.md`. Do not flash the user's device until
  they request deployment. Firmware build artifacts are not source files.
- Pipeline architecture and setup: read root `README.md` and
  `src/second_brain/ARCHITECTURE.md`. The supplied v3 scoring/gating policy is
  authoritative; preserve its tests and explain behavioral changes.
- Test behavior at the real boundary. `python scripts/test.py` runs both the
  pipeline/service and receiver/native-firmware suites. Model fixtures test
  orchestration, not recognition quality. Never claim real ASR validation from
  a deterministic fixture. `windows/build.ps1` builds Windows binaries and
  runs the status renderer tests; live Windows checks are documented separately.
- Keep receiver ACKs behind durable audio and resume state. Only closed,
  checkpointed recordings enter processing. Interrupted jobs recover from
  SQLite. Vault publication is deterministic and preserves human edits.
- No secrets, model weights, recordings or transcripts in Git. Public tray
  status contains operational metadata only. Config and private logs live in
  ProgramData under the service account's ACL.
- The service must run without a logged-in user. The tray is a separate,
  optional process; closing it never stops ingestion. Do not depend on mapped
  drives, interactive prompts, user PATH or runtime model downloads.
- Docs, tests and CI must move with code. Run relevant tests before committing;
  state clearly which hardware/Windows/model checks could not be performed.
- Retain third-party licenses. Keep SDK/toolchain downloads outside Git.

# Firmware (Omi CV1) — Agent Guide

Component guide for `omi/firmware/`. General engineering rules: root `AGENTS.md`.

## Release Workflow

Firmware releases are manual via `.github/workflows/firmware_release.yml`:

1. Bump `CONFIG_BT_DIS_FW_REV_STR` in `omi/firmware/omi/omi.conf` first.
2. `gh workflow run firmware_release.yml -f publish=publish -f changelog="..." -f minimum_app_version_code=...` (omit `publish` for a build-only QA run).
3. The workflow builds via Docker (NCS 2.9.0 sysbuild + MCUboot), names the OTA asset `Omi_CV1_OTA_v<ver>.zip` (the "ota" substring is required), and publishes a `Omi_CV1_v<ver>` GitHub Release with the `KEY_VALUE` body that `backend/routers/firmware.py` serves.

Build logic lives in `omi/firmware/scripts/ci/`.

For the local recorder fork on macOS, use `MAC_BUILD.md` and
`scripts/build-cv1-macos.sh` with NCS 2.9.0. Host protocol tests run with
`python -m unittest discover -s tests -t .` in `scripts/omi-local`.
The Wi-Fi portal uses a documented adaptation of Nordic provisioning; see
`PROVISIONING.md`. Keep its native C tests in the same host test discovery suite.
Keep SDK workspaces and toolchain downloads outside the repository.

## Formatting

C/C++ files: `clang-format -i <files>` (the repo pre-commit hook covers this).

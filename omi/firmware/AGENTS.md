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

The host suite is registered as `cv1-local-recorder-tests` in the shared checks
manifest (local and CI). CV1 NVS stays at `0xf8000`, size `0x2000`; preserve
that persistent ABI when changing SDK dependencies.

Battery reports must stay fresh without a BLE connection. `battery-status`
exposes voltage and sample age; the charging GPIO alone does not prove current
flow. Native tests execute the battery work handler and fractional gauge filter.

VOX recording boundaries must drain PCM and packed frames before SD sleep.
Zero-length codec callbacks are ordered end markers, never Opus encode errors.
Native VOX tests and receiver tests cover the 30-second timer, marker ordering,
resume across upload sessions, and closing audio durably before acknowledging.

Normal recording uses no LEDs; acoustic sleep uses solid red. Preserve the
higher-priority setup/upload/storage/clock warnings. `test_led_state_c.py`
executes the production selector across charging, connection and warning states.

Short button releases (40–999 ms) toggle manual pause through the microphone
owner thread. Manual pause drains an end marker, disables acoustic wake, and
powers down the microphone rail. It overrides other awake LED states with a
200 ms red pulse every 3 seconds; sound cannot resume it. Resume failures stay
paused. Acoustic wake vibrates for 80 ms only after a successful microphone
start. `test_manual_recording_c.py` exercises production button/owner paths,
PCM suppression, LED timing, and failures. `test_disconnect_power_c.py` covers
SD ownership during BLE disconnect, including manual pause.

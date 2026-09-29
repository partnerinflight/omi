# Firmware (Omi CV1) — Agent Guide

Component guide for `omi/firmware/`. General engineering rules: root `AGENTS.md`.

## Local build and deployment

Bump `CONFIG_BT_DIS_FW_REV_STR` in both configuration files for firmware
changes. Use NCS 2.9.0 and the local build scripts; archive the signed OTA ZIP
and verify its signatures. Deployment is a separate, explicitly requested step.
The old cloud firmware release workflow/backend no longer exists in this fork.

For the local recorder fork on macOS, use `MAC_BUILD.md` and
`scripts/build-cv1-macos.sh` with NCS 2.9.0. Host protocol tests run with
`python -m unittest discover -s tests -t .` in `scripts/omi-local`.
The Wi-Fi portal uses a documented adaptation of Nordic provisioning; see
`PROVISIONING.md`. Keep its native C tests in the same host test discovery suite.
Keep SDK workspaces and toolchain downloads outside the repository.

## Formatting

C/C++ files: `clang-format -i <files>` before building.

The host suite runs via root `scripts/test.py` and `.github/workflows/ci.yml`. CV1 NVS stays at `0xf8000`, size `0x2000`; preserve
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
start and at least `CONFIG_OMI_AAD_WAKE_HAPTIC_MIN_SLEEP_MS` (5 min) of
acoustic sleep. Every mic restart drops `CONFIG_OMI_MIC_START_DISCARD_MS`
(500 ms) of PCM before recording or VOX tracking, which hides the PDM startup
transient and the wake vibration. `test_manual_recording_c.py` exercises production button/owner paths,
PCM suppression, LED timing, and failures. `test_disconnect_power_c.py` covers
SD ownership during BLE disconnect, including manual pause.

The Wi-Fi setup hold is 20 seconds from `.12`; it is not a reboot. Keep the
3–5 second power-off release window separate, so an abandoned 5–20 second
hold does nothing. Native hold-policy and button-handler tests cover both
thresholds and ensure one action per hold. Keep the portal help in sync.

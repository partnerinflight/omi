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

Every button gesture is hold-then-release (`button_hold.h`): release at 3–5 s toggles
manual pause (1 pulse at 3 s), 10–15 s powers off (2 pulses at 10 s), and 20 s or more
opens Wi-Fi setup (3 pulses at 20 s; Wi-Fi build only, the BLE-only build caps at 2
pulses). Short clicks and releases in the 5–10 s / 15–20 s dead bands do nothing. The
action is decided by the hold length at the last poll the button was seen pressed, so it
matches the pulse the user felt. Pulses play while held, so resume waits 150 ms for the
motor before powering the mic; the 500 ms startup discard
(`CONFIG_OMI_MIC_START_DISCARD_MS`) still applies. Manual pause runs through the
microphone owner thread: it drains an end marker, disables acoustic wake and powers down
the mic rail; while paused, red pulses 200 ms every 3 s and sound cannot resume it.
Setup and active Wi-Fi/BLE storage transfers override this red pulse; transfers show
solid green with blinking blue. When they finish or stop, paused red feedback returns
without resuming the microphone. Charging or a BLE connection alone does not override pause.
Resume failures stay paused. Acoustic wake vibrates for 80 ms only after a successful mic
start and at least `CONFIG_OMI_AAD_WAKE_HAPTIC_MIN_SLEEP_MS` (5 min) of acoustic sleep.
Short taps still send the BLE tap/double-tap notifications. `test_wifi_config_c.py` and
`test_manual_recording_c.py` execute the production policy, handler and owner paths;
`test_disconnect_power_c.py` covers SD ownership during BLE disconnect, including manual
pause. Keep the portal help in sync.

Recorded PCM passes a 100 Hz 4th-order Butterworth high-pass (`mic_highpass.c`,
`CONFIG_OMI_MIC_HIGHPASS`) before VOX and the codec; discarded startup blocks still run
through it so no stale state reaches a recording. It removes car road/wind boom, not
in-band noise. `test_mic_highpass_c.py` executes the filter and `process_audio_buffer`.
Opus stays CELT-only (`RESTRICTED_LOWDELAY`): the bootloader cannot revert, so do not
switch to SILK/VOIP without measuring codec-thread stack and CPU on hardware first.

The silence timer measures a 250 Hz–3.4 kHz band-passed level (`vox_filter.c`) and needs
3 of the last 5 100 ms blocks at `CONFIG_OMI_VAD_ABS_THRESHOLD` (200), so keystrokes do
not keep the mic awake. Hardware acoustic wake is 80 dB (T5838 register 0x08; 2.5 dB
steps assumed, confirm on device). Tune the threshold with
`python -m omi_local.vox_replay <recordings>`; evidence is in `VALIDATION_MAC.md`.

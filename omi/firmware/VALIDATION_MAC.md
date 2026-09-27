# Mac migration validation — 2026-09-27

**Not flashed: the connected stock device contains 16 LittleFS recordings
(49,920,640 bytes). The custom firmware uses a raw SD ring and would initialize
over that filesystem. Export/preserve these recordings before installing it.
The stock BLE download implementation auto-deletes completed files, so a
backup requires an explicit data-migration decision. No download/delete/erase
or OTA write was performed.**

Recovered `partnerinflight/omi`, branch `feature/local-only-recorder`, commit
`42a433de2d20ff3ab242476baab6a099be8c4cef`. Its three commits preserve offline
Opus recording, authenticated Wi-Fi upload and resume. Current upstream has no
CV1 firmware changes relative to this branch's upstream base. Work continues
in the isolated worktree `feature/wifi-local-upload`; no main push is involved.

## Environment

- SDK: nRF Connect SDK 2.9.0; Zephyr 3.7.99-ncs2.
- Official Apple Silicon Nordic toolchain bundle `b8efef2ad5` in
  `/opt/nordic/ncs/toolchains/b8efef2ad5`.
- nrfutil 8.2.1, toolchain-manager 0.15.0; Zephyr SDK 0.17.0,
  Arm GCC 12.2.0, CMake 3.21.0, Nordic Python 3.12.4, west 1.2.0.
- Native protobuf generator: grpcio-tools 1.62.3, isolated host Python 3.12 venv.
- SDK workspace `/Users/eugenepolonsky/code/SecondBrain/ncs/v2.9.0`.
- NCS source modules match manifest pins; unrelated projects are disabled using
  west's project filter. nRF70 binary blobs match the SDK module's SHA-256 values.

## Exact clean build

From `/Users/eugenepolonsky/code/SecondBrain`:

```bash
OMI_PROTOC_PYTHON=/Users/eugenepolonsky/code/SecondBrain/.tools/host-python/bin/python \
OMI_PROTOC=/Users/eugenepolonsky/code/SecondBrain/omi-wifi/omi/firmware/scripts/protoc-native \
NCS_ROOT=/Users/eugenepolonsky/code/SecondBrain/ncs/v2.9.0 \
NRFUTIL=/Users/eugenepolonsky/code/SecondBrain/.tools/bin/nrfutil \
bash omi-wifi/omi/firmware/scripts/build-cv1-macos.sh --wifi
```

The wrapper invokes `west build --pristine always` with sysbuild for
`omi/nrf5340/cpuapp`. Output directory:
`omi-wifi/omi/firmware/build/local-wifi`.
Use `dfu_application.zip` for OTA. `merged.hex` and `merged_CPUNET.hex` are the
application/network core recovery images. Never use either HEX with BLE DFU.
Build artifacts and SDK files are ignored/untracked, not committed.

## Evidence

The recovered baseline completed a clean native sysbuild **before** feature
implementation: flash 629,928 / 949,760 bytes; RAM 429,200 / 450,560 bytes.
The feature build also completed a pristine sysbuild; exact memory use and
artifact hashes are in the local build logs and final task report. A final
pristine build after commit binds the delivered images to the reported revision.

Host suite: 45 tests passed, including real loopback receiver tests, native
firmware parser/hold/ownership tests and execution of the page JavaScript.
The setup page was also rendered and inspected at a narrow mobile viewport.
The local preview uses the actual HTML; this does not prove AP reachability.

Read-only BLE checks found an Omi CV1, hardware 5.0, firmware 3.0.19, battery
100%, with the MCUboot SMP service. Its active image hash exactly matches the
official `Omi_CV1_OTA_v3.0.19.zip` downloaded for recovery. Stock/custom signed
application images use the same RSA key hash; imgtool verifies the custom
signature and image digest. Both OTA manifests identify app image 0 and
network image 1, with matching load addresses and MCUboot image version 0.0.0.
These checks establish packaging compatibility, not successful hardware boot.

## BLE OTA procedure

1. First preserve the 16 existing stock recordings, or explicitly accept their
   loss. This is an SD-format migration from stock 3.0.19 LittleFS to the
   recovered branch's raw ring. Do not flash before resolving this. Then charge
   Omi, keep it nearby, and close other apps connected to it.
2. In Nordic **nRF Connect for Mobile** (iOS/macOS), connect to Omi and open
   the **DFU / MCU Manager** action. Select `dfu_application.zip` as a
   multi-image MCUboot package; let the manifest select both cores. Do not use
   the legacy Nordic Secure DFU service or pick a raw HEX file.
3. Start the update, keep the app foreground and wait for upload and reboot.
   Reconnect and check firmware revision `3.0.22-localwifi.1`, SMP service,
   and the setup AP. An interrupted upload can be retried before reboot.
   This board uses overwrite-only updates; automatic boot rollback is not
   available. Preserve the known-good OTA package as the recovery option.

The official Mac 2.7.18 DMG downloaded in this session fails strict code-signature
verification after copying. Its protections were not bypassed. Use Nordic's
iOS app or a verified Mac installation for the GUI procedure.

## SWD fallback only

If BLE still works, recover by OTA using the saved official ZIP at
`/Users/eugenepolonsky/code/SecondBrain/.inspection/stock-recovery/Omi_CV1_OTA_v3.0.19.zip`.
If the application cannot expose BLE, connect a supported J-Link probe to the
CV1 SWD pads (3.3 V target reference, GND, SWDIO, SWCLK), power the device, then
from the Nordic SDK shell run:

```bash
west flash -d /absolute/path/to/omi/firmware/build/local-wifi --runner jlink
```

Sysbuild's generated runners/domain metadata selects both core images. Verify
the target serial before executing if multiple probes are attached. Do not add
`--erase`, `recover`, or a chip erase: those may remove settings or stored data.
A protected or otherwise unrecoverable target needs a separately justified
recovery decision. No SWD probe was detected on this Mac.

## Physical validation still required

Successful boot, SD recording growth, audio decode, AP reachability, saved
settings after reboot, STA/DNS/mDNS connectivity, real upload, unreachable-server
queue retention/retry, button gestures and post-update BLE DFU must be measured
on hardware. Build/unit-test results must not be described as those tests.

Device file-list evidence: `/Users/eugenepolonsky/code/SecondBrain/.inspection/stock-file-list.json`.
No personal audio or credentials are committed to Git.

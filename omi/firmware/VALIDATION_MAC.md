# Mac migration validation — 2026-09-27

**Hardware migration authorized and performed over BLE.** The owner explicitly
accepted loss of the stock LittleFS recordings. App and network images were
uploaded, their secondary-slot digests verified, marked permanent and rebooted.
No chip erase or SWD was used. Offline recording and repeated BLE OTA work.
The setup AP `OMI-Setup-CE3B` now broadcasts and macOS successfully associates
with it. Temporary diagnostic images are not release artifacts.

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

Host suite: 49 tests passed, including real loopback receiver tests, native
firmware parser/hold/ownership tests and execution of the page JavaScript.
The setup page was also rendered and inspected at a narrow mobile viewport.
The local preview uses the actual HTML; this does not prove AP reachability.

Read-only BLE checks found an Omi CV1, hardware 5.0, firmware 3.0.19, battery
100%, with the MCUboot SMP service. Its active image hash exactly matches the
official `Omi_CV1_OTA_v3.0.19.zip` downloaded for recovery. Stock/custom signed
application images use the same RSA key hash; imgtool verifies the custom
signature and image digest. Both OTA manifests identify app image 0 and
network image 1, with matching load addresses and MCUboot image version 0.0.0.
The new firmware boots and reports `3.0.22-localwifi.2`. BLE readback measured
SD sequence growth with no dropped records. An inherited packing bug initially
produced truncated Opus frames; commit `7ede982d7` fixes whole-frame boundaries
and zero padding. A fresh 50-record sample contained 255 Opus frames (5.1 seconds)
and decoded with ffmpeg without errors. The BLE MCUboot service remained
usable through repeated updates.

Hardware startup exposed an all-zero OTP MAC read, CV1 power-switch timing that
differs from the DK, and an undersized socket poll table that terminated Nordic's
supplicant. The branch contains corresponding fixes and regression guards.
A retained crash log identified a subsequent stack overflow in
`mgmt_work_q_obj`; increasing its 4200-byte stack to 8192 bytes eliminated that
reset. The AP stays active with BLE responsive; measured system heap high-water
usage was 86,076 / 120,000 bytes. Native tests execute the production power/MAC/
lifecycle code, and negative compile tests reject the measured bad limits.
Temporary disabled synthetic settings survived reboot without opening an AP.
They contained no user credentials and were cleared before AP testing.

## BLE OTA procedure

1. For another stock device, export recordings or accept their loss before the
   LittleFS-to-raw-ring migration. This decision is already resolved for the
   connected CV1. Charge Omi, keep it nearby, and close other connected apps.
2. In Nordic **nRF Connect for Mobile** (iOS/macOS), connect to Omi and open
   the **DFU / MCU Manager** action. Select `dfu_application.zip` as a
   multi-image MCUboot package; let the manifest select both cores. Do not use
   the legacy Nordic Secure DFU service or pick a raw HEX file.
3. Start the update, keep the app foreground and wait for upload and reboot.
   Reconnect and check firmware revision `3.0.22-localwifi.2`, SMP service,
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

## Hardware validation status

Verified: boot, microphone/Opus decode, persistent SD ring growth, BLE control,
repeated app OTA, initial two-core OTA, configuration survival after reset,
setup AP broadcasting and macOS association.
Queue samples were read without advancing their read pointer.

Still required: real portal HTTP requests, station Wi-Fi association,
DNS/mDNS, real receiver upload, failed destination/retry on hardware, and physical
button gestures. Host tests cover authentication, disconnect/resume, queue safety,
portal JavaScript and button timing, but do not establish those hardware results.

The local `.inspection/` directory contains OTA logs, status snapshots, decoded
audio evidence and temporary diagnostics. No personal audio or credentials are
committed to Git. Known-good stock recovery is retained separately.

A clean configure of the untouched recovered branch placed NVS at `0xf8000`
(size `0x2000`). New dependencies initially reordered dynamic partitions.
Both persistent partitions are now pinned in the board static layout, and
compiler assertions plus a negative compile test prevent accidental NVS drift.

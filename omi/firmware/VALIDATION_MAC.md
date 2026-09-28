# Mac migration validation — 2026-09-27

**Hardware migration authorized and performed over BLE.** The owner explicitly
accepted loss of the stock LittleFS recordings. App and network images were
uploaded, their secondary-slot digests verified, marked permanent and rebooted.
No chip erase or SWD was used. Offline recording and repeated BLE OTA work.
The setup AP `OMI-Setup-CE3B` now broadcasts and macOS successfully associates
with it. DHCP assigns the Mac `192.168.4.2`; the real page renders in Safari.
Temporary diagnostic images are not release artifacts.

## Follow-up: battery freshness and Windows uploads

The original Bluetooth battery value was only refreshed while connected,
which made a reconnect expose an old 1% before the next sample. The gauge also
rounded away small percentage increases. Regression tests now execute the
disconnected work handler and fractional filter. The diagnostic characteristic
reports sample age and voltage; hardware samples refreshed normally, initially
4190–4209 mV with charging asserted, later 4250–4276 mV with it deasserted.
The inherited voltage calibration changes with the charger flag; these are
firmware estimates, not an independent measurement of charging current.

The router's DHCP offer exposed an unterminated DNS list in the pinned NCS 2.9
DHCP client when mDNS is enabled. On-device logs showed EAFNOSUPPORT/EALREADY
while applying DNS. A hash-checked generated source correction, leaving the SDK
unchanged, now gets a bound lease at 192.168.1.40 and authenticates to the user's
Windows receiver at 192.168.1.85:7331. The host can also reach that TCP port.

The upload path now bounds socket writes and budgets its transmit memory.
A successful association clears the previous session's disconnect event,
preventing that stale event from aborting a retry. Native tests exercise this
event sequence and real later disconnects. Receiver commit `d14eb687f` fixes
Windows progress-file flushing through a writable handle. The user updated
and restarted the Windows receiver before the successful follow-up below.

On firmware `.7`, the receiver resume pointer advanced from 6944 to 6980, but
no DATA acknowledgements or complete sessions were observed. Retained storage
continued growing with zero dropped records.

On 2026-09-28, after the Windows receiver restart, a manual `.8` upload received
28,584 DATA packet acknowledgements (12,691,296 record bytes) and kept uploading.
The SD read sequence advanced to 35,564, with write sequence 139,996 and zero
dropped records: 104,432 records remained at that snapshot. This verifies real
device-to-Windows transfer and release of acknowledged records. The complete
backlog/session finish was not yet observed. `last_result=link lost` and errno
`-5` still describe the previous failed session while the current state is
`uploading`; those fields update when the active session finishes.
Automatic upload is still gated by active charging; a full battery may clear
that signal while externally powered. Manual `upload-now` bypasses the gate.

The host suite now passes 59 tests, including loopback receiver, native C,
Windows flush-permission seams, DHCP bounds, and TCP backpressure tests.
Clean Wi-Fi and BLE-only builds passed for `.6`; the final clean Wi-Fi build
`.8` uses 871208 flash bytes and 428720 RAM bytes. Its application digest is
`f43b0b92295bd4b011429bdf5518026c2e2972c3817cca86add266e1fcdb74dc`,
identical to the signed application installed over BLE; the network core was
left unchanged. The clean ZIP SHA-256 is
`505c9861e96437a12e63f5a9d1b0cd67dd8249082368a06d3ed8ac5ea9f709d2`.
The broad monorepo preflight
is unavailable in this sparse checkout because unrelated registered artifacts
are absent; scoped firmware tests and builds are the validation evidence.

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
artifact hashes are in the local build logs and final task report. The final
pristine Wi-Fi build at `c96c906c40dc7b6098bb702d79b246b6420cf4d8` uses
870,676 / 949,760 flash bytes and 424,536 / 450,560 RAM bytes. The BLE-only
recorder also passed a pristine build (246,964 flash; 335,336 RAM bytes).
Both final OTA images pass imgtool RSA-signature/digest verification. The
Wi-Fi ZIP SHA-256 is
`878fe98c38b1050f26aaa2d9049ddf186b736f42624f0987f35874a445a82055`.

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
usable through repeated updates, including the final two-image production OTA.
Both staged image digests matched the signed ZIP before activation. A transient
image-list read immediately after reconnect omitted the external-flash slots;
a fresh read recovered them and activation proceeded only after re-verification.

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

A real Safari Save test accepted a disposable SSID/password, receiver hostname,
port 17331 and pairing key. The page displayed its Saved confirmation and the
AP closed. BLE reported configured with no configuration error. The nonexistent
SSID timed out, left the read sequence at zero and did not drop queued records.
After reboot, configuration remained valid, the AP stayed off and SD write
sequence grew from 84,744 to 84,792. The complete fresh-page-to-Save sequence was repeated successfully on the
final production image with its original socket pool sizes. Disposable settings
were cleared after validation, and the Mac was restored to its normal network.

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
setup AP broadcasting, macOS association, DHCP, HTTP 200 for the actual HTML
and Nordic scan protobuf, and browser Save with disposable settings.
Queue samples were read without advancing their read pointer. After validation,
the damaged pre-fix bring-up prefix before sequence 6944 was deliberately
discarded; the remaining valid audio was retained. The final 50-record sample
contained 247 Opus frames and decoded with ffmpeg without errors.

Still required: station Wi-Fi association with the owner’s network, DNS/mDNS,
real receiver upload, receiver failure followed by successful retry, and physical
button gestures. Failed association with a nonexistent test SSID retained the queue. Host tests cover authentication, disconnect/resume, queue safety,
portal JavaScript and button timing, but do not establish those hardware results.

The final setup configuration is empty, ready for the owner to enter Wi-Fi and
receiver settings. The temporary network was removed from the Mac’s preferred
network list, its normal Wi-Fi restored, and the Safari test tab closed.

The local `.inspection/` directory contains OTA logs, status snapshots, decoded
audio evidence and temporary diagnostics. No personal audio or credentials are
committed to Git. Known-good stock recovery is retained separately.

A clean configure of the untouched recovered branch placed NVS at `0xf8000`
(size `0x2000`). New dependencies initially reordered dynamic partitions.
Both persistent partitions are now pinned in the board static layout, and
compiler assertions plus a negative compile test prevent accidental NVS drift.

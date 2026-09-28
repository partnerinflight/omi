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

## VOX recording boundaries — 2026-09-28

The user selected 30 seconds of continuous silence. Firmware
`3.0.22-localwifi.9` drains PCM/Opus/packed records before writing an explicit
recording-end marker and entering acoustic sleep. Receiver boundaries are now
independent of upload boundaries, including across process restart and an
unknown RTC. Boundary-closed audio is flushed before its chunk is acknowledged.
The wire format and timing behavior are documented in `PROVISIONING.md`.

Validation: `python -m unittest discover -s tests -t .` in `scripts/omi-local`
passes 64 tests. New native tests execute the production 30-second timer,
codec drain/padding/error path and frame-packer marker/retry path. Receiver tests
cover markers alone, identical or missing timestamps, separate files after
restart, and continuity across successful upload sessions. The existing full
upload test now includes a recorder marker to request EOS; the documented wire
contract, rather than upload DONE, owns recording closure.

Both clean NCS 2.9 builds pass using the build command above (omit `--wifi` for
BLE-only). Wi-Fi application: 871736 flash / 428784 RAM bytes. BLE-only:
247816 flash / 335472 RAM bytes. The installed application digest is
`1b92c9041629620b6b83ea73c1c4cd2eaa6cc50687d1b4b978b64c7aecee4eca`;
OTA ZIP SHA-256:
`046ccfd4d1ef516fd30a6f650a6430fde598567b0129a85c91e14a4536360ae5`.
The app signature and staged image digest were verified before reboot; BLE
readback reports `.9`. Settings and queued recordings are preserved.

Before the update, the Windows backlog upload completed successfully: 132934
packets acknowledged, one successful session, last result OK/errno 0. The updated
receiver must be pulled/restarted to recognize VOX boundaries. Real acoustic
wake sensitivity and first-syllable capture have not been calibrated; hardware
wake has no pre-roll. Previous recordings are not retrospectively re-segmented.

Live silence check: `.9` recording sequence stopped at 140334 and remained
unchanged over repeated 5-second polls through 61 seconds, with zero dropped
records. A non-destructive BLE pull found the recording-end marker at sequence
140333. The receiver parser consumed it and closed the captured file with EOS.
All three captured files (including pre-update buffered audio) decode through
ffmpeg without errors. Controlled sound-to-wake/new-recording testing remains
pending physical test conditions; automated tests cover the split independently
of timestamps. Evidence is retained locally in `.inspection/vox-hardware.log`
and `.inspection/vox-audio/`.

## Recorder LED meanings — 2026-09-28

Firmware candidate `3.0.22-localwifi.10` turns all normal-state LEDs off while
recording and shows solid red while the microphone is in VOX silence sleep.
Normal charging/BLE colours no longer override those two states. Existing off,
boot, setup, upload, full-storage and invalid-clock indications retain priority.
The existing main loop refreshes the indicator once per second.

Validation: 65 host tests pass. The new native test executes the production
`set_led_state` function across recording/sleep transitions, both charge and
connection states, low/full battery, and each existing higher-priority status.
Both Wi-Fi and BLE-only NCS 2.9 incremental sysbuilds pass. The signed Wi-Fi app
uses 871788 flash bytes and 428784 RAM bytes and passes imgtool verification.
App digest: `b7b24dcf37379aab85e1df798bf78f46dffaf1247b65a77c2faba0b1c929bfde`.
ZIP SHA-256: `a696a7f16a0934409fc43f3bc13cc12989d608fc2e20d15f3c8c28e078432ba4`.

Deployment is pending: a 30-second scan briefly connected and confirmed idle,
configured firmware, but OTA then timed out connecting. A retry using a fresh
scan found no advertising device. Neither attempt began the image upload; the
last deployed firmware remains `.9`. Physical LED appearance has not been
observed. Bring Omi within this Mac's BLE range and disconnect other BLE clients
before retrying. The Windows receiver needs no change for these LED meanings.

## Manual recording pause and acoustic wake feedback — 2026-09-28

Candidate `3.0.22-localwifi.11` adds an 80 ms vibration after successful acoustic
wake and short-click pause/resume. Manual pause disables PDM, releases the AAD
clock pins, powers off the microphone rail, and ignores acoustic wake. It drains
a recording-end marker before idle storage sleep; resume starts a new recording.
A failed boundary or microphone start leaves the device paused. While paused,
red pulses for 200 ms every 3 seconds and green/blue stay off. The normal dark
recording / solid-red VOX indications and 30-second silence timer are retained.
Pause does not persist across restart. Existing long holds retain their actions.

Validation: `python -m unittest discover -s tests -t .` in `scripts/omi-local`
passes 69 tests. Native tests execute production owner transitions, click/hold
handling, PCM suppression, sound wake, failed resume/boundary retry, LED phase
including uptime wrap, and SD ownership when BLE disconnects during an upload.
The latter revealed an adjacent SD-power bug, corrected in its own commit.
Tests use controlled GPIO/DMIC/time seams; they do not measure physical power,
vibration strength, or LED timing on a device.

Clean NCS 2.9 Wi-Fi and BLE-only sysbuilds pass; the Wi-Fi build was then rebuilt
incrementally after the final microphone pin-release change. Final Wi-Fi app:
872476 flash / 428800 RAM bytes. BLE-only: 248588 flash / 335488 RAM bytes.
Both files in the Wi-Fi OTA ZIP pass MCUboot imgtool signature verification.
App digest: `959b0656916e211f9373257555cf2f226631bafc8518473f0471a08f248200bb`.
Network digest: `334a2e2e8cb94c36f86e8d52c477afd2235f72d0a6edebf84f485880d74d1f8c`.
OTA ZIP SHA-256: `93a2ddab62b128750665e73795228a402036ee3039ad1712caa1fb907fe4a6de`.
Local archive: `.inspection/Omi_CV1_OTA_3.0.22-localwifi.11.zip` in the parent
workspace. Existing Kconfig/deprecation and unrelated C warnings remain.
`scripts/pr-preflight --suggest` reports no affected product invariants, but
its failure-class metadata validation fails on absent artifacts in this sparse
checkout; this is not a full-repository CI pass.

**Built only, not deployed, at the user's request.** Omi is at work; physical
click, sound, vibration, LED and upload checks wait until the user brings it
home. No BLE scan, OTA upload, reset or receiver connection was attempted.
Last confirmed installed firmware remains `.9`; this package includes `.10`'s
LED change. The existing marker-aware Windows receiver needs no update.

## Longer setup hold — 2026-09-28

Candidate `3.0.22-localwifi.12` moves the existing Wi-Fi setup hold from 5 to
20 seconds, following the user's request to lengthen the five-second action.
Code inspection confirmed that action opens setup, not a reboot; this distinction
was explained to the user. Power-off remains a release at 3–5 seconds; releasing
at 5–20 seconds does nothing. Short recording clicks are unchanged. Portal help
and provisioning documentation now show 20 seconds.

Validation: all 69 host tests pass. Production hold-policy and button-handler
tests assert no action at 5 seconds or just below 20 seconds, setup at exactly
20 seconds, no repeat while held/released, and preserved short-click/power-off
behavior. These expectations follow the user's requested duration. Wi-Fi and
BLE-only NCS 2.9 incremental sysbuilds pass. Wi-Fi: 872472 flash / 428800 RAM
bytes; BLE-only: 248588 flash / 335488 RAM bytes. Both OTA images pass imgtool
signature verification. App digest:
`5af68c8bff1424cbf5c454d664721078eb12a200dee3cfb88575e1333413ac59`.
ZIP SHA-256: `d793d9ea94e8c1155be5a9445359a28bc8040205b3afc56a77c1c4faa3565fc4`.
Archive: `.inspection/Omi_CV1_OTA_3.0.22-localwifi.12.zip` in the parent workspace.
No deployment or hardware test was attempted; the device remains at work.
The earlier sparse-checkout preflight limitation remains; component tests and
builds are verified, not full-repository CI.


## .12 deployment completed — 2026-09-28

The owner brought Omi back and explicitly requested the update. BLE preflight
confirmed `.9`; the archived `.12` ZIP checksum and both MCUboot signatures
matched the build evidence above. Both images uploaded successfully, both staged
digests were checked, then both were marked permanent and the device reset.
A fresh BLE connection reported `3.0.22-localwifi.12` with the expected application
digest active and confirmed:
`5af68c8bff1424cbf5c454d664721078eb12a200dee3cfb88575e1333413ac59`.

Post-boot checks: Wi-Fi configuration present, clock synchronized/valid, SD ring
readable (`read_seq=139986`, `write_seq=399993`, 260007 queued, zero dropped).
A fresh battery diagnostic reported 3967 mV / 71%, charging signal inactive,
sample age 8533 ms, error 0. The initial standard GATT battery read immediately
after boot showed 100% before the fresh diagnostic; it is not a charging claim.
No settings or recordings were erased, and no upload was explicitly triggered.
Physical click/pause/resume, vibration and LED timing remain user-test checks;
BLE/version/image/clock/storage checks are complete.

Local evidence in the parent workspace: `.inspection/firmware12-upload.log`,
`firmware12-activate.log`, `firmware12-postflash-probe.log`, and
`firmware12-health.log`. This supersedes the earlier built-only deployment notes.

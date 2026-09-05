# Omi CV1 — local-only recorder firmware

This fork turns the consumer Omi (CV1, nRF5340, `omi/firmware/omi`) into an
**offline audio recorder**. Microphone audio has exactly two possible
destinations:

1. the SD ring on the device (always, whenever the mic hears sound), and
2. a computer running `omi/firmware/scripts/omi-local` that *explicitly* asks
   for it over BLE.

There is no live audio streaming, no automatic sync, no automatic deletion and
no Wi-Fi/HTTP/cloud path for audio. BLE stays enabled for DFU (MCUmgr), battery
and status, settings, and the local dump service.

## Before / after

| | upstream 3.0.21 | local-only recorder |
|---|---|---|
| powered on, no phone | encode → SD ring | encode → SD ring |
| BLE client connects | encode → BLE audio notifications (ring not written) | encode → SD ring (unchanged) |
| BLE client subscribes to audio | live stream starts | no audio characteristic exists |
| Omi app connects | app finds storage service `30295780-…`, reads the ring, firmware **auto-advances** (deletes) as data is acknowledged, app sends ADVANCE at the end | app finds no storage service; nothing is transferred or deleted |
| storage full | oldest audio silently overwritten | recording pauses, nothing overwritten, LED alternates red/blue + one haptic pulse |
| clock never set | nothing is recorded | records with timestamp 0 until the CLI sets the clock |
| deletion | automatic during/after sync | only `CMD_RING_ADVANCE` / `CMD_RING_CLEAR` from the dump client |

## What changed (call-graph level)

* `src/lib/core/transport.c`
  * The BLE **audio service** (`19B10000-…`, data/notify + codec + speaker
    characteristics), `push_to_gatt()`, `test_pusher()` and the pusher's
    "connected & subscribed → stream" branch are deleted. `pusher()` now has a
    single path: `read_from_tx_queue()` → `write_to_storage()` → `write_to_file()`.
  * The advertisement carries the local storage service UUID instead of the Omi
    audio service UUID.
  * The bulk TX throttle (`bulk_tx_sem`) is kept for the dump stream.
  * Build-time `#error` if offline storage is disabled or the speaker is enabled.
* `src/lib/core/storage.c` (dump protocol)
  * New service/characteristic UUIDs `7D2C0001/2/3-9A6B-4E2F-B1C3-5A0F0C41ED10`
    (the Omi app looks for `30295780-…` and finds nothing).
  * All auto-advance code (`sync_checkpoint_advance`, TX-completion
    accounting, advance-on-disconnect, advance-on-DONE) removed. `READ` is now
    pure and repeatable; only explicit `ADVANCE`/`CLEAR` move the read pointer.
  * `INFO` gained a trailing codec-id byte (21 = Opus).
* `src/sd_card.c`
  * `ring_batch_slot_free()`: a new 16 KiB batch may only be started in a slot
    whose previous contents have been deleted by the host. When no slot is free
    the packet is dropped and counted, `ring_full` is raised, and recording
    resumes automatically after an explicit delete. `flush_current_batch()` has
    a second guard and returns `-ENOSPC` instead of ever moving `read_seq`.
  * Recording no longer requires a valid RTC (timestamp 0 when unknown).
  * `sd_ring_is_full()` accessor.
* `src/main.c`: storage-full LED pattern (red/blue alternating, 1 Hz) + one
  300 ms haptic pulse on entering the full state.
* `omi.conf`: `CONFIG_BT_DIS_FW_REV_STR="3.0.21-local.1"` so the CLI/nRF
  Connect can confirm which firmware is running.

Not changed: mic capture (`mic.c`, incl. T5838 hardware AAD sleep), Opus
parameters (`config.h`: 16 kHz mono, 20 ms frames, 32 kbit/s VBR, complexity 3),
the on-SD ring layout, MCUboot/sysbuild/signing, DFU, battery, button, LEDs,
settings, time-sync service.

## Storage budget (from the configured codec)

* 32 kbit/s × 20 ms = 80 bytes per frame; `write_to_storage()` packs 5 frames
  (81 bytes each incl. length byte) per 440-byte payload → one 444-byte record
  per 100 ms, 36 records per 16 KiB batch.
* **≈ 16.4 MB (15.6 MiB) per hour of captured audio; ≈ 61 hours per GB
  (65 h per GiB).** `omi-local info` prints the real capacity of the card.
* Silence is not stored: after 10 s below the AAD threshold the mic sleeps
  (existing behaviour, `CONFIG_OMI_VAD_HOLD_MS`), so wall-clock days last longer
  than the audio-hours figure. Set `CONFIG_OMI_ENABLE_T5838_AAD=n` for
  literally continuous capture at a battery cost.

## Build

Same toolchain as upstream CI (`omi/firmware/scripts/ci/build-cv1.sh`):
nRF Connect SDK **v2.9.0**, board `omi/nrf5340/cpuapp`, sysbuild + MCUboot.

```bash
# one-time: NCS workspace + Zephyr SDK 0.17.0 (arm-zephyr-eabi), or use
# `nrfutil toolchain-manager launch --ncs-version v2.9.0 --shell`
west build -b omi/nrf5340/cpuapp omi/firmware/omi --sysbuild -d build-local --pristine always \
  -- -DBOARD_ROOT="$PWD/omi/firmware" -DCONF_FILE=omi.conf
```

Outputs: `build-local/dfu_application.zip` (OTA), `build-local/merged.hex`
(app core, J-Link), `build-local/merged_CPUNET.hex` (net core, J-Link).

## Flash

* **OTA (no cable):** nRF Connect for Mobile → connect to "Omi" → DFU tab →
  select `dfu_application.zip` → Start. Or from a Mac with a BLE `mcumgr`
  client. See `omi/firmware/BUILD_AND_OTA_FLASH.md`.
* **J-Link:** `omi/firmware/FLASH_3.0.8/README.md` with `merged_CPUNET.hex`
  first, then `merged.hex`.

The firmware reports `3.0.21-local.1` in the Device Information Service once it
is running (`omi-local info`).

## Dump from a Mac

See `omi/firmware/scripts/omi-local/README.md`.

## Optional: Wi-Fi upload to a local receiver while charging

The CV1 carries an nRF7002 Wi-Fi companion that upstream never enabled. The
Wi-Fi build (`CONFIG_OMI_WIFI_UPLOAD`, `src/wifi_upload.c`) uses it for one
thing only: while the device is on the charger and the ring holds at least a
minute of unread audio, it joins the provisioned network, connects to ONE
provisioned receiver (`omi-local serve` on any Windows/macOS/Linux machine on
the LAN), proves the receiver knows the shared secret, streams the unread
records, and deletes each chunk only after the receiver has ACKed that it wrote
and fsync'ed it. The nRF7002 is powered down again after the session (or when
the charger is removed, which aborts the session). Off the charger nothing
changes.

### Build

```bash
west build -b omi/nrf5340/cpuapp omi/firmware/omi --sysbuild -d build-wifi --pristine always \
  -- -DBOARD_ROOT="$PWD/omi/firmware" -DCONF_FILE=omi.conf \
     -DEXTRA_CONF_FILE=overlay-wifi-upload.conf -DSB_EXTRA_CONF_FILE=sysbuild-wifi.conf
```

`sysbuild-wifi.conf` is mandatory: NCS sysbuild silently forces
`CONFIG_WIFI_NRF70=n` unless `SB_CONFIG_WIFI_NRF70=y` is set.

### Provision and run

```bash
omi-local serve ~/omi-recordings --port 7331        # on the receiver machine; prints/creates the secret
omi-local wifi-setup --ssid MyWifi --password '...' --host 192.168.1.20 --port 7331   # over BLE, same secret file
omi-local wifi-status                               # configured? last result? heap headroom?
omi-local upload-now --watch 30                     # force a session without waiting for the charger
```

The secret lives in `~/.omi-local/upload-secret.hex` on the machine that ran
`wifi-setup`; copy that file to the receiver machine (or pass `--secret-file`
on both sides). Give the receiver a fixed LAN IP (DHCP reservation): the
firmware has no DNS.

### Protocol

See the header of `src/wifi_upload.c` (TCP, framed messages, mutual
HMAC-SHA256 challenge/response, ACK-after-persist). `src/lib/core/local_auth.c`
is a self-contained SHA-256/HMAC that is compiled natively and cross-checked
against Python's hashlib in the CLI test-suite.

### Memory budget (measured)

| Build | app-core RAM | flash |
|---|---|---|
| BLE-only recorder | 335 KB / 440 KB (74%) | 247 KB |
| Wi-Fi upload | 429 KB / 440 KB (95%) | 630 KB |

Zephyr allocates everything at link time, so the recorder cannot "free" RAM
during an upload; the Wi-Fi build instead shrinks two recorder buffers
(`SD_REQ_QUEUE_MSGS` 100→40, `AUDIO_BUFFER_SAMPLES` 1 s→0.3 s) and lends the
16 KiB BLE bulk buffer to the uploader. The system heap is 120 KB (Nordic's
default ask is 150 KB); `omi-local wifi-status` reports free/max-used heap so
this can be tuned on real hardware. **Heap sufficiency, association and
throughput have not been validated on a device yet.**

## Remaining ways audio could leave the device

* The Wi-Fi upload path, in the Wi-Fi build only, and only to the receiver
  that proves knowledge of the provisioned 32-byte secret. Wi-Fi credentials
  and the secret are stored in the settings partition of internal flash.
* The local dump service itself, if a BLE client that knows the protocol
  connects and explicitly issues `READ`. BLE is unauthenticated in upstream
  firmware and this fork does not change that (pairing/bonding can be added on
  top: `BT_GATT_PERM_WRITE_ENCRYPT` on the control characteristic).
* Physical access to the SD NAND / SWD debug port (unchanged from upstream).
* A future OTA that re-adds streaming: MCUboot signature checking is unchanged,
  so only an image signed with the repo key can be installed.

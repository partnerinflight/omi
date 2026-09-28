# CV1 local Wi-Fi recorder

Build `--wifi` with NCS 2.9.0 using [MAC_BUILD.md](MAC_BUILD.md). The
application remains an offline Opus recorder. Audio stays in the existing SD
ring (444-byte sequence-numbered records), not individual FAT files. The local
receiver converts it into Ogg Opus recordings and JSON metadata. No Omi cloud
account or service is involved.

## Before flashing stock firmware

The recovered custom branch uses a different SD format from stock 3.0.19.
A device already running the recovered raw-ring firmware keeps its recordings.
A stock LittleFS device needs its recordings exported, or their loss explicitly
accepted, before this image boots: raw-ring initialization overwrites filesystem
metadata. The owner accepted that migration for the connected CV1; see
[VALIDATION_MAC.md](VALIDATION_MAC.md).
Do not interpret "no chip erase" as preservation of an incompatible SD layout.

## Setup

1. Start the receiver on a computer reachable over your Wi-Fi:
   `omi-local serve /absolute/path/to/SecondBrain/audio --port 7331`.
   This creates `~/.omi-local/upload-secret.hex` if missing. Keep it private.
2. On the first unconfigured boot, join `OMI-Setup-XXXX`. Open
   **http://192.168.4.1** explicitly; captive-portal popups are not required.
   The setup network is open and uses HTTP. Configure in a trusted place:
   nearby clients can observe credentials or change setup during this window.
3. Select a scanned network (up to five cached results), or enter its SSID.
   Enter its WPA2 password (blank for an open network), receiver hostname or
   IPv4 address, port, and the receiver's 64-character hex pairing key.
4. Press **Save & connect**. Omi persists the configuration, closes the AP,
   joins Wi-Fi, resolves the receiver, authenticates it and uploads queued
   audio. An empty queue still tests the authenticated connection.
   Rejoin your normal network on the phone/computer.
5. Use `omi-local wifi-status` over BLE to see connection/authentication errors
   and upload counters. The portal disappears when saved; it cannot report
   the subsequent STA result over the disconnected AP.

Setup blinks blue and expires after five minutes. Hold the button for **20
seconds** to reopen it; release after the haptic acknowledgement. Existing
settings remain until a successful save. A short press never resets settings.
To power off the Wi-Fi build, **release between three and five seconds**.
The BLE-only build retains its three-second power-off hold. An active upload
stops at a safe record boundary before setup; an outstanding network operation
may delay entry until its timeout. `omi-local wifi-forget` explicitly clears
the selected configuration; reboot or hold 20 seconds to start setup again.

Configured boot does not expose an AP, including when Wi-Fi or the receiver is
down. Normal uploads require charging, at least roughly one minute of queued
audio, and a five-minute retry interval. `omi-local upload-now` also works on
battery. Wi-Fi is brought down after setup/upload; BLE control and MCUboot
SMP DFU remain available. Recording continues on its existing threads.

## Persistence and protocol

The radio owner supplies a stable local unicast MAC derived from the nRF5340
hardware ID before powering Wi-Fi up; it does not depend on the nRF7002 OTP
address and never writes OTP. It waits for Nordic's supplicant to register and
unregister the interface before AP/STA transitions. Socket polling supports all
16 descriptors; a compiler guard rejects fewer than the six entries required
by NCS 2.9's supplicant configuration. A four-entry limit terminated the
supplicant with `select: Not enough space` on the actual CV1. The network
management event thread has an 8 KiB stack, also guarded at compile time:
retained fault logs identified a real stack overflow in `mgmt_work_q_obj`
with the SDK's 4200-byte default during interface setup.

CV1's U11 I/O supply switch is a **TPS22916CYFPR**, unlike the DK's TCK106AG
assumed by NCS. The board startup hook discharges the rails, asserts BUCKEN,
then enables IOVDD and waits 10 ms before QSPI access. TI specifies 1.7 ms typical
turn-on at 3.6 V for the C variant, longer than NCS's 1 ms delay. See the
[TI datasheet, section 6.6](https://www.ti.com/lit/ds/symlink/tps22916.pdf) and the
consumer schematic's nRF7002 sheet. SDK sources remain untouched.

Nordic `wifi_credentials` with its Settings/NVS backend owns the Wi-Fi password.
This backend is **not encrypted at rest** on this non-TF-M build. The app's
`omi/wifi_upload` setting contains SSID, hostname, port and receiver pairing
key; its legacy password field is zeroed on disk. The previous version-1 blob
is migrated after loading, with credentials saved before replacing the old
blob. The original raw-ring SD format is unchanged. CV1 NVS is pinned at flash
`0xf8000..0xfa000` and compile-time assertions reject layout drift. This upgrade preserves the old
IPv4 BLE TLV for already-installed external clients; the in-tree CLI sends the
new hostname TLV. Hostnames including `secondbrain.local` use DNS/mDNS IPv4
resolution. URLs, paths, IPv6 and enterprise/WPA3-only networks are not supported.

The recovered receiver protocol is retained: mutual HMAC-SHA256 authentication,
sequence-based resume, and ACK only after audio/state persistence. The recorder
only stores complete Opus frames and zero-pads unused record tails; native C
tests exercise overflow, exact-fit records and buffer reuse with the host decoder. Firmware
advances its read pointer only to acknowledged or previously persisted
sequences. A wrong key, disconnect, timeout or unavailable receiver leaves
unacknowledged records queued. Payloads are authenticated at connection setup,
not encrypted; use a trusted LAN. A full SD ring never overwrites unread audio;
new audio is dropped with the existing full-storage indication until space is
freed. BLE and Wi-Fi bulk transfers have one atomic owner.

## Nordic provisioning integration

`src/provisioning.c` is a licensed adaptation of Nordic's NCS **v2.9.0**
`nrf/subsys/net/lib/softap_wifi_provision/softap_wifi_provision.c`.
CMake retains the Nordic `softap_wifi_provision` library target and its generated
`proto/common.proto`/nanopb messages. The browser uses the original
`GET /prov/networks` and `POST /prov/configure` wire format. The small
`POST /omi/destination` extension stages hostname/port/pairing key; it does not
persist until the Wi-Fi configuration arrives and all fields validate.

The SDK's sample lifecycle is one-shot, refuses existing credentials and resets
by deleting credentials/rebooting; it also has no browser page hook. This
adaptation runs synchronously in the existing Wi-Fi owner thread, supports
non-destructive reentry, limits setup/client time, handles short writes/closed
sockets and bounded fragmented HTTP parsing, and cleans up DHCP/AP/static IP
before STA use. It uses HTTP instead of the sample's self-signed HTTPS to avoid
adding TLS configuration and heap demand to the CV1 recorder. The native
library's TLS/SMF Kconfig dependencies remain enabled, but its sample TLS server
and extra state-machine thread are not linked. SDK source files are not edited.
The Nordic license is in `omi/licenses/LicenseRef-Nordic-5-Clause.txt`.

## Validation

Run `python -m unittest discover -s tests -t .` in `scripts/omi-local`.
Tests cover real receiver sockets, authentication, interrupted upload/resume,
corrupt-record rejection, Opus/CRC output, and native C configuration parsing,
button hold policy and transfer ownership. A clean CV1 sysbuild checks all
images and partition sizes. Hardware validation results and exact artifacts
are recorded separately in `VALIDATION_MAC.md`; unit tests do not establish
radio, microphone, SD or power performance.

## NCS 2.9 DHCP DNS-list correction

The pinned SDK DHCP client allocates exactly `CONFIG_DNS_RESOLVER_MAX_SERVERS`
pointers without a terminator, while its DNS resolver iterates over that count
plus multicast slots. With mDNS enabled and a full DNS option, it reads beyond
the list. On the actual CV1 this rejected real router offers with DNS errors
`-106` (unsupported address family), followed by `-120` (already registered),
and surfaced as an upload DHCP timeout.

The Wi-Fi CMake integration generates a corrected `omi_dhcpv4.c` in the build
directory. `scripts/patch-ncs290-dhcp.py` accepts only the reviewed NCS 2.9 source
SHA256, adds a zero-initialized terminating entry, and supplies mDNS addresses
alongside the DHCP DNS servers. The installed SDK is never edited. An SDK update
must review this patch; a source mismatch fails the build. Native sanitizer
regressions cover full one- and two-server lists with the multicast slot enabled.

## Sound-activated recordings (VOX)

From firmware `3.0.22-localwifi.10`, normal recording turns all LEDs off;
VOX silence (after the 30-second timeout) shows solid red. This overrides normal
charging/BLE connection colours. Boot, shutdown, setup, upload, storage-full,
and invalid-clock indications retain their existing behavior and priority.
The indicator refreshes within one second of the microphone transition.

Firmware `3.0.22-localwifi.9` ends a recording after **30 continuous seconds**
below its sound threshold. Sound during the countdown resets it. The trailing
30 seconds remain in the recording so ordinary pauses do not cut a conversation.
The microphone then uses its existing T5838 acoustic wake mode; sound wakes it
and recording resumes. The Windows receiver creates a new UTC-timestamped
`.opus` file (plus JSON metadata) for that recording. Files are assembled on the
receiver; Omi stores sequence-numbered records on its SD card.

The firmware pauses the PCM producer, drains the codec and frame packer, writes
an explicit end marker, then permits microphone/SD sleep. The marker is a normal
444-byte record: a big-endian UTC timestamp followed by the eight payload bytes
`00 4f 4d 49 45 4e 44 01` and 432 zero bytes. It contains no audio. The receiver
closes and flushes the file before acknowledging the marker, without creating an
empty file. Sequence numbering and upload resume include the marker. Markers
work even without a valid clock; unknown-time filenames still contain a unique
sequence number. Upload completion itself does not end a recording.

Update/restart the receiver when installing this firmware. Earlier receivers
ignore marker payloads as padding and do not implement the explicit split.
Existing stored audio retains the previous timestamp-gap splitting behavior;
this change does not retrospectively detect silence inside old recordings.

`CONFIG_OMI_VAD_HOLD_MS=30000` sets the silence interval and
`CONFIG_OMI_VAD_ABS_THRESHOLD=250` sets the PCM threshold. The hardware wake
threshold is separately defined in `t5838_aad.c`; room noise and distance affect
what counts as sound. Hardware acoustic wake has startup latency and no pre-roll.
BLE bulk downloads defer microphone sleep until the download ends; Wi-Fi
uploads can continue while the microphone sleeps.

### Manual pause and wake feedback

Firmware `3.0.22-localwifi.11` adds a brief **80 ms vibration** when sound wakes
the microphone from VOX silence and recording successfully starts. Starting at
boot or resuming with the button does not add this sound-wake vibration.

**Click and release the button** (less than one second) to pause recording;
click again to resume. Pausing closes the current recording and turns the
microphone power rail off, including acoustic detection: sound cannot restart
recording while manually paused. Resume starts a new recording and returns to
the normal 30-second VOX behavior. Manual pause lasts until the next click or
restart; it is not saved across power cycles. A failed resume leaves the mic off
and paused so another click can retry.

While manually paused, the red LED flashes for **200 ms every 3 seconds**, with
green and blue off. This indication takes priority over setup, upload and warning
LEDs while the device is awake. Existing uploads may continue during pause.
Ordinary acoustic silence still shows solid red, and normal recording is dark.
The 3–5 second power-off release is unchanged. From `.12`, reopening setup
requires a continuous 20-second hold (previously 5 seconds). Releasing between
5 and 20 seconds does nothing. This opens setup rather than rebooting; long
holds do not toggle recording. The receiver from `.9` already understands
these recording boundaries and needs no additional update for `.11`.

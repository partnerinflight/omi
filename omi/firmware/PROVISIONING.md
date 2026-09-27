# CV1 local Wi-Fi recorder

Build `--wifi` with NCS 2.9.0 using [MAC_BUILD.md](MAC_BUILD.md). The
application remains an offline Opus recorder. Audio stays in the existing SD
ring (444-byte sequence-numbered records), not individual FAT files. The local
receiver converts it into Ogg Opus recordings and JSON metadata. No Omi cloud
account or service is involved.

## Before flashing stock firmware

The recovered custom branch uses a different SD format from stock 3.0.19.
A device already running the recovered raw-ring firmware keeps its recordings.
A stock LittleFS device must have its recordings preserved before this image
boots: initialization of the raw ring overwrites the filesystem metadata.
See the connected-device finding and flashing gate in [VALIDATION_MAC.md](VALIDATION_MAC.md).
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

Setup blinks blue and expires after five minutes. Hold the button for **five
seconds** to reopen it; release after the haptic acknowledgement. Existing
settings remain until a successful save. A short press never resets settings.
To power off the Wi-Fi build, **release between three and five seconds**.
The BLE-only build retains its three-second power-off hold. An active upload
stops at a safe record boundary before setup; an outstanding network operation
may delay entry until its timeout. `omi-local wifi-forget` explicitly clears
the selected configuration; reboot or hold five seconds to start setup again.

Configured boot does not expose an AP, including when Wi-Fi or the receiver is
down. Normal uploads require charging, at least roughly one minute of queued
audio, and a five-minute retry interval. `omi-local upload-now` also works on
battery. Wi-Fi is brought down after setup/upload; BLE control and MCUboot
SMP DFU remain available. Recording continues on its existing threads.

## Persistence and protocol

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

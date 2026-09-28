# omi-local — local-only BLE dump tool for the Omi CV1

Companion CLI for the **local-only recorder firmware** in `omi/firmware/omi`
(see `omi/firmware/omi/LOCAL_ONLY_RECORDER.md`). It talks BLE directly to the
device from your Mac (or Linux box). There is no Omi app, backend, cloud,
Supabase, HTTP or transcription service anywhere in this tool.

```
omi-local scan                       # find recorders nearby
omi-local info                       # firmware, battery, clock, ring usage / free space
omi-local list [DEST]                # what is on the device (+ what DEST already holds)
omi-local pull DEST                  # download everything not yet in DEST (resumable) — NEVER deletes
omi-local pull DEST --all            # re-download everything on the device
omi-local delete --downloaded DEST   # delete ONLY what was verified-downloaded into DEST
omi-local delete --through SEQ       # delete every record before SEQ
omi-local delete --all               # delete everything
omi-local time-sync                  # set the device clock (also done automatically on every connect)
omi-local verify FILE.opus           # check page CRCs / completeness of a pulled file
```

## Install (macOS)

```bash
cd omi/firmware/scripts/omi-local
python3 -m venv .venv && source .venv/bin/activate
pip install -e .            # pulls in bleak (CoreBluetooth backend on macOS)
omi-local --version
```

The first BLE scan will make macOS ask for Bluetooth permission for your
terminal app — allow it.

## Typical dump session

```bash
omi-local info
omi-local pull ~/omi-recordings          # prints progress; writes one .opus per recording session
omi-local list ~/omi-recordings          # confirm what was pulled
omi-local delete --downloaded ~/omi-recordings   # optional: free the device, only what is verified on disk
```

Output files are playable Ogg Opus (`omi_<UTC start>_seq<first record>.opus`,
e.g. `omi_20260905-143012_seq000000012345.opus`), each with a `.json` sidecar
(seq range, timestamps, frame count, seconds of audio). A new file starts
whenever the device's timestamps jump by more than `--gap-seconds` (default
60 s: the microphone was in hardware sleep, or the device was off) or when the
clock state changes. Records written before the clock was ever set are named
`omi_unknown-time_seq…`.

`DEST/.omi-local/state.json` remembers how far each device has been pulled.
`pull` resumes from there (continuing the same `.opus` file after an
interrupted run) and `delete --downloaded` will never delete past it.

## How it maps onto the device

The firmware stores audio as a **ring of 444-byte records** addressed by a
64-bit sequence number (`[read_seq, write_seq)` is what is on the device), not
as files. Consequently:

* "list" shows the ring range plus oldest/newest timestamps; the per-session
  split happens on the host as data arrives.
* "delete" is prefix-only: the device can only drop everything **before** a
  sequence number. `delete --downloaded` uses the verified contiguous prefix
  the tool has on disk; `delete --through SEQ` and `delete --all` are explicit.
* Reading never changes the ring. A failed or interrupted pull leaves the
  device untouched; re-running `pull` resumes.

Protocol details live in `omi_local/protocol.py` and in the header comment of
`omi/firmware/omi/src/lib/core/storage.c`.

## Wi-Fi upload receiver (Windows, macOS or Linux)

With the Wi-Fi firmware build the device uploads by itself whenever it is on
the charger. Run the receiver on the machine that should hold the recordings:

```powershell
# Windows PowerShell (Python 3.10+ from python.org)
cd omi\firmware\scripts\omi-local
py -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -e .
omi-local serve C:\omi-recordings --port 7331
```

Allow the port through Windows Firewall (Private network) when prompted, and
give the machine a fixed LAN IP. Then, from any machine with Bluetooth and the
same secret file (`~/.omi-local/upload-secret.hex`, or `%USERPROFILE%\.omi-local\`
on Windows; `wifi-setup` creates it):

```bash
omi-local wifi-setup --ssid MyWifi --password '...' --host 192.168.1.20 --port 7331
omi-local wifi-status
omi-local upload-now --watch 30
```

The receiver writes the same per-session `.opus` + `.json` files as `pull`,
ACKs a chunk only after it is written and fsync'ed, and the device deletes
only ACKed chunks. An interrupted upload resumes from what the receiver holds.

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

The tests run against a software model of the firmware ring/BLE link
(`tests/fake_device.py`), including mid-transfer disconnects, and check that
the tool never sends a delete command unless asked.

## Battery and network diagnostics

Firmware `3.0.22-localwifi.3` and later keeps the standard Bluetooth battery
value current even while disconnected. The gauge retains fractional changes
instead of rounding every update to a whole percent. It remains a voltage-based
estimate, not a measured charge counter.

```powershell
python -m omi_local.cli battery-status
python -m omi_local.cli wifi-status
```

`battery-status` reports measured millivolts, estimated percent, the charger
status input, age of the last successful sample, and the latest sampling error.
Samples normally refresh every 5 seconds while connected and 10 seconds when
disconnected. An age of `4294967295` with error `-11` means no valid sample yet.
The charger flag is not a current measurement.
Automatic uploads currently require this active-charging flag; a full battery
can deassert it even on the charger. Use `upload-now` for an explicit transfer
when the flag is off.

`wifi-status` includes the DHCP state, retry count and requested IPv4 address
on diagnostic-capable firmware. States are 0 disabled, 1 initializing,
2 selecting (awaiting offer), 3 requesting, 4 renewing, 5 rebinding, 6 bound,
and 7 declining. After teardown these fields retain the last session state.
A DHCP timeout happens before contacting the receiver; opening port 7331 on
Windows cannot fix that stage. The receiver needs inbound **TCP** port 7331
on the active Windows Firewall profile and normally listens on `0.0.0.0`.
After updating the receiver package, restart the running `serve` process.
The Windows progress flush requires a writable file handle (fixed in
`d14eb687f`); an older process can disconnect before acknowledging a chunk.

Firmware `3.0.22-localwifi.8` also clears old-session disconnect events when a
new association succeeds. TCP writes handle temporary buffer pressure, and the
build checks that the transmit pool covers the configured send window.

The read-only battery diagnostic characteristic is
`19b10014-e8f2-537e-4f6c-d104768a1214`: little-endian `u16 millivolts`,
`u8 percent`, `u8 charging`, `u32 sample_age_ms`, `i32 sample_error`.
Upload status retains its original 28-byte prefix and appends `u8 dhcp_state`,
`u8 attempts`, two reserved bytes, then four IPv4 bytes in network order.

## Sound-activated files

With firmware `3.0.22-localwifi.9`, 30 seconds of continuous silence ends a
recording. Sound resumes recording into a new timestamped file on the receiver.
Update and restart `serve` to recognize the firmware's recording-end markers.
An upload ending leaves an unfinished recording resumable; only its marker or
a timestamp/sequence boundary closes it. Existing recordings are not re-split.
Firmware `3.0.22-localwifi.11` also vibrates briefly on sound-triggered wake.
A short button click pauses/resumes recording: paused means microphone power
off and a brief red flash every 3 seconds; sound cannot resume it. Each pause
ends the current file. No receiver update beyond the `.9` boundary support is
needed. See [VOX behavior](../../PROVISIONING.md#sound-activated-recordings-vox).

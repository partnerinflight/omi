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

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

The tests run against a software model of the firmware ring/BLE link
(`tests/fake_device.py`), including mid-transfer disconnects, and check that
the tool never sends a delete command unless asked.

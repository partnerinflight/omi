"""omi-local command line: list / pull / delete recordings over BLE, locally.

    omi-local scan
    omi-local info
    omi-local list
    omi-local pull <dest>            # everything not yet downloaded (resumable)
    omi-local pull <dest> --all      # re-download everything on the device
    omi-local delete --downloaded <dest>
    omi-local delete --through SEQ
    omi-local delete --all
    omi-local time-sync
    omi-local verify <file.opus>
    omi-local wifi-setup --ssid S --password P --host IP [--port N]   # provision Wi-Fi upload (Wi-Fi firmware)
    omi-local wifi-status / wifi-forget / upload-now
    omi-local serve <dest> [--port N]                                 # the local receiver for Wi-Fi uploads

Nothing here talks to any cloud. BLE, or TCP on your own LAN for `serve`.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import random
import sys
import time
from pathlib import Path

from . import __version__
from . import protocol as P
from . import upload_protocol as U
from .device import BleakTransport, DeviceError, DumpClient, Progress, TransferError
from .oggopus import MuxState, OggOpusWriter, iter_pages
from .state import DeviceState, OpenFile, StateStore

log = logging.getLogger("omi_local")


# --- helpers -------------------------------------------------------------------
def fmt_ts(ts: int) -> str:
    if ts == 0:
        return "unknown-time"
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def fmt_ts_name(ts: int) -> str:
    if ts == 0:
        return "unknown-time"
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y%m%d-%H%M%S")


def fmt_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return f"{n:.1f} GiB"


def fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:d}h{m:02d}m{s:02d}s" if h else f"{m:d}m{s:02d}s"


# Each 440-byte payload holds ~5 Opus frames of 20 ms at 32 kbit/s (see docs).
APPROX_MS_PER_RECORD = 100.0


def approx_seconds(packets: int) -> float:
    return packets * APPROX_MS_PER_RECORD / 1000.0


# --- session writer ---------------------------------------------------------------
class SessionWriter:
    """Turns the record stream into one playable .opus (+ .json) file per session.

    A session ends when a record is not the successor of the previous one, when
    the clock state flips (unknown <-> known), or when the timestamp jumps by
    more than `gap_s` (the mic was in hardware sleep or the device was off).
    """

    def __init__(self, dest: Path, device_id: str, state: DeviceState, store: StateStore, gap_s: int = 60,
                 keep_raw: bool = False) -> None:
        self.dest = Path(dest)
        self.device_id = device_id
        self.state = state
        self.store = store
        self.gap_s = gap_s
        self.keep_raw = keep_raw
        self._writer: OggOpusWriter | None = None
        self._fp = None
        self._raw_fp = None
        self._cur: dict | None = None
        self._prev: P.Record | None = None
        self.files_written: list[str] = []
        self._resume_open_file()

    # -- file management
    def _resume_open_file(self) -> None:
        of = self.state.open_file
        if not of:
            return
        path = Path(of.path)
        if not path.exists():
            self.state.open_file = None
            return
        self._fp = open(path, "ab")
        self._writer = OggOpusWriter(self._fp, serial=0, resume_state=MuxState.from_dict(of.mux))
        sidecar = path.with_suffix(".json")
        self._cur = json.loads(sidecar.read_text()) if sidecar.exists() else {"file": str(path)}
        self._prev = P.Record(of.last_seq, of.last_timestamp, ())
        if self.keep_raw:
            self._raw_fp = open(path.with_suffix(".omiring"), "ab")
        self.files_written.append(str(path))
        log.info("resuming %s after seq %d", path.name, of.last_seq)

    def _file_name(self, rec: P.Record) -> Path:
        stem = f"omi_{fmt_ts_name(rec.timestamp)}_seq{rec.seq:012d}"
        return self.dest / f"{stem}.opus"

    def _open(self, rec: P.Record) -> None:
        self.dest.mkdir(parents=True, exist_ok=True)
        path = self._file_name(rec)
        serial = random.getrandbits(32)
        self._fp = open(path, "wb")
        self._writer = OggOpusWriter(
            self._fp, serial=serial, sample_rate=P.OPUS_SAMPLE_RATE, channels=P.OPUS_CHANNELS,
            comments=[f"ENCODER=omi-local {__version__}", f"OMI_DEVICE={self.device_id}",
                      f"OMI_START_SEQ={rec.seq}", f"OMI_START_UTC={fmt_ts(rec.timestamp)}"],
        )
        self._cur = {
            "file": str(path), "device": self.device_id, "start_seq": rec.seq, "end_seq": rec.seq,
            "first_timestamp": rec.timestamp, "last_timestamp": rec.timestamp,
            "first_utc": fmt_ts(rec.timestamp), "last_utc": fmt_ts(rec.timestamp),
            "packets": 0, "frames": 0, "audio_seconds": 0.0, "complete": False,
        }
        if self.keep_raw:
            self._raw_fp = open(path.with_suffix(".omiring"), "wb")
        self.files_written.append(str(path))

    def _sidecar(self) -> None:
        if self._cur:
            Path(self._cur["file"]).with_suffix(".json").write_text(json.dumps(self._cur, indent=2))

    def _close_file(self, eos: bool) -> None:
        if not self._writer:
            return
        self._writer.close(eos=eos)
        self._fp.close()
        if self._raw_fp:
            self._raw_fp.close()
            self._raw_fp = None
        if self._cur:
            self._cur["complete"] = bool(eos)
            self._sidecar()
            if eos:
                self.state.files.append({k: self._cur[k] for k in ("file", "start_seq", "end_seq", "first_utc",
                                                                   "last_utc", "audio_seconds")})
        self._writer = None
        self._fp = None
        self._cur = None

    # -- record ingestion
    def add(self, seq: int, data: bytes) -> None:
        for rec in P.iter_records(seq, data):
            if P.session_boundary(self._prev, rec, self.gap_s):
                self._close_file(eos=True)
                self._open(rec)
            if self._raw_fp:
                self._raw_fp.write(data[(rec.seq - seq) * P.RECORD_SIZE:(rec.seq - seq + 1) * P.RECORD_SIZE])
            for frame in rec.frames:
                self._writer.write_packet(frame)
            c = self._cur
            c["end_seq"] = rec.seq + 1
            c["last_timestamp"] = rec.timestamp
            c["last_utc"] = fmt_ts(rec.timestamp)
            c["packets"] += 1
            c["frames"] += len(rec.frames)
            c["audio_seconds"] = round(c["audio_seconds"] + sum(P.opus_packet_duration_ms(f) for f in rec.frames) / 1000.0, 3)
            self._prev = rec
        # Persist progress after every delivered chunk so an interrupted run resumes exactly.
        if self._writer:
            self._writer.flush()
            self._fp.flush()
            self._sidecar()
            self.state.open_file = OpenFile(self._cur["file"], self._prev.seq, self._prev.timestamp,
                                            self._writer.state.to_dict())
        # `downloaded_through` is the verified CONTIGUOUS prefix from the device's
        # read_seq. Only extend it when this chunk continues that prefix, so an
        # explicit `--from` range can never make `delete --downloaded` delete
        # something that was skipped.
        chunk_end = seq + len(data) // P.RECORD_SIZE
        if seq == self.state.downloaded_through:
            self.state.downloaded_through = chunk_end
        self.store.put(self.state)

    def fsync(self) -> None:
        """Force written pages to disk (the Wi-Fi receiver ACKs only after this)."""
        import os

        for fp in (self._fp, self._raw_fp):
            if fp is not None and not fp.closed:
                fp.flush()
                os.fsync(fp.fileno())
        self.store.fsync()

    def finish(self, final: bool) -> None:
        """final=True closes the current file with EOS (device fully drained)."""
        if final:
            self._close_file(eos=True)
            self.state.open_file = None
        else:
            # Keep the file resumable; nothing else to do (pages already flushed).
            if self._writer:
                self._close_file(eos=False)
        self.store.put(self.state)


# --- commands ---------------------------------------------------------------------
async def _connect(args) -> tuple[DumpClient, BleakTransport]:
    t = BleakTransport(address=args.address, name=args.name, scan_timeout=args.scan_timeout)
    client = DumpClient(t)
    await client.open()
    if not args.no_time_sync:
        epoch = await client.sync_time()
        log.info("device clock set to %s", fmt_ts(epoch))
    return client, t


async def cmd_scan(args) -> int:
    from bleak import BleakScanner

    print(f"scanning for {args.scan_timeout:.0f}s ...")
    found = await BleakScanner.discover(timeout=args.scan_timeout, return_adv=True)
    n = 0
    for dev, adv in found.values():
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        if P.LOCAL_STORAGE_SERVICE_UUID in uuids:
            n += 1
            print(f"  {dev.address}  name={dev.name or adv.local_name!r}  rssi={adv.rssi}")
    if n == 0:
        print("no local-only recorder advertising nearby")
    return 0 if n else 1


async def cmd_info(args) -> int:
    client, t = await _connect(args)
    try:
        info = await client.info()
        st = await client.status()
        fw = await t.read_firmware_rev()
        batt = await t.read_battery()
        print(f"device        : {t.device_id}")
        print(f"firmware      : {fw or '?'}")
        print(f"battery       : {batt if batt is not None else '?'}%")
        print(f"clock set     : {'yes' if st.rtc_valid else 'NO (timestamps will be 0 until synced)'}")
        codec = "opus" if info.codec_id == P.CODEC_ID_OPUS else f"codec {info.codec_id}"
        print(f"codec         : {codec} ({P.OPUS_SAMPLE_RATE} Hz mono, {info.packet_size}-byte records)")
        print(f"ring          : read_seq={info.read_seq} write_seq={info.write_seq} capacity={info.capacity_packets} pkts")
        print(f"unread        : {info.unread_packets} pkts = {fmt_bytes(info.unread_bytes)} ≈ {fmt_duration(approx_seconds(info.unread_packets))}")
        print(f"free          : {info.free_packets} pkts ≈ {fmt_duration(approx_seconds(info.free_packets))}"
              + ("   ** STORAGE FULL, recording paused **" if info.free_packets == 0 else ""))
        print(f"dropped pkts  : {info.dropped_packets} (audio lost because storage was full or busy)")
        return 0
    finally:
        await client.close()


async def cmd_list(args) -> int:
    client, t = await _connect(args)
    try:
        info = await client.info()
        print(f"device {t.device_id}: {info.unread_packets} unread packets, {fmt_bytes(info.unread_bytes)}, "
              f"≈{fmt_duration(approx_seconds(info.unread_packets))}, seq [{info.read_seq}, {info.write_seq})")
        if info.unread_packets == 0:
            print("nothing recorded (or everything already deleted)")
            return 0
        first = next(P.iter_records(info.read_seq, await client.read_range(info.read_seq, 1)))
        last = next(P.iter_records(info.write_seq - 1, await client.read_range(info.write_seq - 1, 1)))
        print(f"oldest record : seq {first.seq}  {fmt_ts(first.timestamp)}")
        print(f"newest record : seq {last.seq}  {fmt_ts(last.timestamp)}")
        if args.dest:
            st = StateStore(Path(args.dest)).get(t.device_id)
            dl = st.downloaded_through
            if info.read_seq <= dl <= info.write_seq:
                print(f"downloaded    : through seq {dl} ({dl - info.read_seq} pkts) into {args.dest}; "
                      f"{info.write_seq - dl} pkts still to pull")
            else:
                print(f"downloaded    : (no valid download state for this device in {args.dest})")
            for f in st.files[-10:]:
                print(f"   {Path(f['file']).name}  {f['first_utc']} .. {f['last_utc']}  {fmt_duration(f['audio_seconds'])}")
        return 0
    finally:
        await client.close()


async def cmd_pull(args) -> int:
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)
    client, t = await _connect(args)
    try:
        info = await client.info()
        if info.codec_id not in (None, P.CODEC_ID_OPUS):
            print(f"unsupported codec id {info.codec_id}", file=sys.stderr)
            return 2
        store = StateStore(dest)
        state = store.get(t.device_id)
        # Decide the range.
        if args.from_seq is not None:
            start = args.from_seq
            end = min(info.write_seq, start + args.count) if args.count else info.write_seq
            state.open_file = None  # explicit ranges never append to a resumable file
        elif args.all:
            start, end = info.read_seq, info.write_seq
            state.open_file = None
        else:
            dl = state.downloaded_through
            if dl < info.read_seq or dl > info.write_seq:
                if dl:
                    log.warning("download state (through seq %d) does not match device ring [%d, %d); starting over",
                                dl, info.read_seq, info.write_seq)
                dl = info.read_seq
                state.downloaded_through = dl
                state.open_file = None
            start, end = dl, info.write_seq
        if start >= end:
            print("nothing new to pull")
            return 0
        state.write_seq_seen = info.write_seq
        writer = SessionWriter(dest, t.device_id, state, store, gap_s=args.gap_seconds, keep_raw=args.keep_raw)
        total = end - start
        print(f"pulling {total} packets ({fmt_bytes(total * P.RECORD_SIZE)}, ≈{fmt_duration(approx_seconds(total))}) "
              f"seq [{start}, {end}) -> {dest}")
        last_print = [0.0]

        def progress(p: Progress) -> None:
            now = time.monotonic()
            if now - last_print[0] >= 1.0 or p.packets_done == p.packets_total:
                last_print[0] = now
                pct = 100.0 * p.packets_done / max(1, p.packets_total)
                print(f"\r  {p.packets_done}/{p.packets_total} pkts  {pct:5.1f}%  {p.rate_kbps:6.1f} KiB/s", end="", flush=True)

        ok = False
        try:
            await client.pull(start, end, writer.add, chunk_packets=args.chunk_packets, progress=progress)
            ok = True
        finally:
            print()
            writer.finish(final=ok and end == info.write_seq)
        if writer.files_written:
            print("files:")
            for f in writer.files_written:
                print(f"  {f}")
        print(f"verified {end - start} packets; device recordings were NOT deleted "
              f"(run `omi-local delete --downloaded {dest}` to free them)")
        return 0
    except TransferError as e:
        print(f"\ntransfer failed: {e}. Partial data was saved; re-run `omi-local pull` to resume.", file=sys.stderr)
        return 3
    finally:
        await client.close()


async def cmd_delete(args) -> int:
    client, t = await _connect(args)
    try:
        info = await client.info()
        if args.all:
            target = None
            what = f"ALL {info.unread_packets} recorded packets"
        elif args.through is not None:
            target = args.through
            what = f"packets before seq {target} ({target - info.read_seq} pkts)"
        else:
            st = StateStore(Path(args.downloaded)).get(t.device_id)
            target = st.downloaded_through
            if not (info.read_seq <= target <= info.write_seq):
                print(f"download state in {args.downloaded} (through seq {target}) does not match the device ring "
                      f"[{info.read_seq}, {info.write_seq}); refusing to delete.", file=sys.stderr)
                return 2
            what = f"the {target - info.read_seq} packets already downloaded into {args.downloaded} (through seq {target})"
        if target is not None and target <= info.read_seq:
            print("nothing to delete")
            return 0
        if not args.yes:
            ans = input(f"Delete {what} from the device? This cannot be undone. [y/N] ").strip().lower()
            if ans != "y":
                print("aborted")
                return 1
        if target is None:
            await client.clear()
        else:
            await client.advance(target)
        after = await client.info()
        print(f"deleted. ring now [{after.read_seq}, {after.write_seq}), {after.unread_packets} pkts remain")
        return 0
    finally:
        await client.close()


async def cmd_time_sync(args) -> int:
    args.no_time_sync = True
    client, t = await _connect(args)
    try:
        epoch = await client.sync_time()
        print(f"device clock set to {fmt_ts(epoch)}")
        return 0
    finally:
        await client.close()


async def _read_upload_status(t: BleakTransport) -> U.UploadStatus:
    return U.parse_upload_status(await t.read_gatt(U.UPLOAD_CONFIG_UUID))


def _print_upload_status(st: U.UploadStatus) -> None:
    print(f"wifi upload    : {'configured' if st.configured else 'NOT configured'}")
    print(f"state          : {st.state_name}")
    print(f"last result    : {st.result_name} (errno {st.last_errno})")
    if st.last_config_err:
        print(f"last config    : rejected (err {st.last_config_err})")
    print(f"sessions ok    : {st.sessions_ok}, packets uploaded: {st.packets_uploaded}")
    if st.last_attempt_uptime_s:
        print(f"last attempt   : {st.last_attempt_uptime_s}s after boot")
    if st.heap_free or st.heap_max_used:
        print(f"heap           : {st.heap_free} B free, {st.heap_max_used} B max used (Wi-Fi stack tuning)")


async def cmd_wifi_setup(args) -> int:
    from .server import load_or_create_secret

    secret = load_or_create_secret(Path(args.secret_file) if args.secret_file else None)
    blob = U.encode_wifi_config(ssid=args.ssid, password=args.password, host=args.host, port=args.port,
                                secret=secret, enabled=not args.disabled)
    client, t = await _connect(args)
    try:
        await t.write_gatt(U.UPLOAD_CONFIG_UUID, blob)
        await asyncio.sleep(1.0)  # the device persists on its work queue
        st = await _read_upload_status(t)
        if st.last_config_err:
            print(f"device rejected the configuration (err {st.last_config_err})", file=sys.stderr)
            return 2
        print(f"provisioned: ssid={args.ssid!r} receiver={args.host}:{args.port} "
              f"secret={'from ' + args.secret_file if args.secret_file else 'in ~/.omi-local/upload-secret.hex'}")
        _print_upload_status(st)
        print("run `omi-local serve <dest>` on the receiver machine with the same secret file")
        return 0
    finally:
        await client.close()


async def cmd_wifi_status(args) -> int:
    client, t = await _connect(args)
    try:
        _print_upload_status(await _read_upload_status(t))
        return 0
    finally:
        await client.close()


async def cmd_wifi_forget(args) -> int:
    client, t = await _connect(args)
    try:
        await t.write_gatt(U.UPLOAD_CONFIG_UUID, U.encode_wifi_config(forget=True))
        await asyncio.sleep(1.0)
        st = await _read_upload_status(t)
        print("Wi-Fi upload configuration erased" if not st.configured else "device still reports configured")
        return 0 if not st.configured else 2
    finally:
        await client.close()


async def cmd_upload_now(args) -> int:
    client, t = await _connect(args)
    try:
        await client.t.write_control(bytes([U.CMD_UPLOAD_NOW]))
        ack = await client._ack()
        if ack.status != P.STATUS_OK:
            print(f"device refused: {'Wi-Fi upload not configured' if ack.status == 11 else P.status_name(ack.status)}",
                  file=sys.stderr)
            return 2
        print("upload session requested; the device connects to the receiver by itself.")
        if args.watch:
            for _ in range(int(args.watch)):
                await asyncio.sleep(2.0)
                st = await _read_upload_status(t)
                print(f"  state={st.state_name} last={st.result_name} packets={st.packets_uploaded}")
                if st.state == 0 and st.last_attempt_uptime_s:
                    break
        return 0
    finally:
        await client.close()


async def cmd_serve(args) -> int:
    from .server import UploadServer, load_or_create_secret

    secret = load_or_create_secret(Path(args.secret_file) if args.secret_file else None)
    server = UploadServer(secret, Path(args.dest), host=args.bind, port=args.port)
    print(f"receiver: listening on {args.bind}:{args.port}, saving to {args.dest} (Ctrl-C to stop)")
    print("provision the device with: omi-local wifi-setup --ssid ... --password ... --host <this machine's IP> "
          f"--port {args.port}")
    await server.serve_forever()
    return 0


def cmd_verify(args) -> int:
    data = Path(args.file).read_bytes()
    pages = list(iter_pages(data))
    bad = [p.page_seq for p in pages if not p.crc_ok]
    packets = sum(len(p.packets) for p in pages[2:])
    granule = pages[-1].granule if pages else 0
    print(f"{args.file}: {len(pages)} pages, {packets} opus packets, {granule / 48000.0:.2f}s, "
          f"{'EOS' if pages and pages[-1].header_type & 0x04 else 'no EOS (resumable/incomplete)'}, "
          f"{'CRC ok' if not bad else f'BAD CRC on pages {bad}'}")
    return 0 if not bad else 1


# --- main ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="omi-local", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--address", help="BLE address / CoreBluetooth UUID of the device (default: scan for the service)")
    p.add_argument("--name", help="only accept a device with this BLE name")
    p.add_argument("--scan-timeout", type=float, default=10.0)
    p.add_argument("--no-time-sync", action="store_true", help="do not set the device clock on connect")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"omi-local {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("scan", help="find local-only recorders nearby")
    sub.add_parser("info", help="device / storage status")
    s = sub.add_parser("list", help="what is on the device (and what has been pulled)")
    s.add_argument("dest", nargs="?", help="destination directory to compare download state against")
    s = sub.add_parser("pull", help="download recordings (never deletes)")
    s.add_argument("dest")
    s.add_argument("--all", action="store_true", help="re-download everything on the device, ignoring local state")
    s.add_argument("--from", dest="from_seq", type=int, help="start seq (advanced)")
    s.add_argument("--count", type=int, default=0, help="packet count with --from (0 = to end)")
    s.add_argument("--gap-seconds", type=int, default=60, help="time gap that starts a new file (default 60)")
    s.add_argument("--chunk-packets", type=int, default=400, help="packets per BLE read request (default 400)")
    s.add_argument("--keep-raw", action="store_true", help="also keep the raw ring records (.omiring)")
    s = sub.add_parser("delete", help="EXPLICITLY delete recordings on the device")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--downloaded", metavar="DEST", help="delete only what was verified-downloaded into DEST")
    g.add_argument("--through", type=int, metavar="SEQ", help="delete every record before SEQ")
    g.add_argument("--all", action="store_true", help="delete everything")
    s.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    sub.add_parser("time-sync", help="set the device clock from this computer")
    s = sub.add_parser("verify", help="check an .opus file written by pull")
    s.add_argument("file")

    s = sub.add_parser("wifi-setup", help="provision Wi-Fi upload (requires the Wi-Fi firmware build)")
    s.add_argument("--ssid", required=True)
    s.add_argument("--password", default="", help="WPA2 password (omit for an open network)")
    s.add_argument("--host", required=True, help="IPv4 address of the machine running `omi-local serve`")
    s.add_argument("--port", type=int, default=7331)
    s.add_argument("--secret-file", help="shared secret file (default ~/.omi-local/upload-secret.hex, created if missing)")
    s.add_argument("--disabled", action="store_true", help="store the config but keep uploads off")
    sub.add_parser("wifi-status", help="show Wi-Fi upload status / last result / heap headroom")
    sub.add_parser("wifi-forget", help="erase the Wi-Fi upload configuration from the device")
    s = sub.add_parser("upload-now", help="trigger an upload session immediately (ignores the charger)")
    s.add_argument("--watch", type=int, default=0, metavar="N", help="poll status N times (2 s apart)")
    s = sub.add_parser("serve", help="run the local receiver for Wi-Fi uploads")
    s.add_argument("dest")
    s.add_argument("--port", type=int, default=7331)
    s.add_argument("--bind", default="0.0.0.0")
    s.add_argument("--secret-file", help="shared secret file (default ~/.omi-local/upload-secret.hex)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")
    if args.cmd == "verify":
        return cmd_verify(args)
    handler = {"scan": cmd_scan, "info": cmd_info, "list": cmd_list, "pull": cmd_pull, "delete": cmd_delete,
               "time-sync": cmd_time_sync, "wifi-setup": cmd_wifi_setup, "wifi-status": cmd_wifi_status,
               "wifi-forget": cmd_wifi_forget, "upload-now": cmd_upload_now, "serve": cmd_serve}[args.cmd]
    try:
        return asyncio.run(handler(args))
    except DeviceError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted (device recordings untouched)", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

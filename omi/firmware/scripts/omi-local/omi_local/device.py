"""Dump client: talks the local storage protocol over an abstract transport.

`Transport` is implemented by `BleakTransport` (real device, macOS/Linux) and by
the fake in tests/. The client never deletes anything on its own: `advance()`
and `clear()` are only reachable from the explicit `delete` CLI command.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

from . import protocol as P

log = logging.getLogger("omi_local")

READ_BEGIN_TIMEOUT_S = 12.0  # SD may still be remounting for up to 5 s after connect
DATA_TIMEOUT_S = 10.0
ACK_TIMEOUT_S = 8.0


class TransferError(Exception):
    """A READ did not complete. `partial` holds the bytes received so far."""

    def __init__(self, msg: str, partial: bytes = b"", retryable: bool = True) -> None:
        super().__init__(msg)
        self.partial = partial
        self.retryable = retryable


class DeviceError(Exception):
    pass


class Transport(Protocol):
    """Minimal async surface the dump client needs."""

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...
    @property
    def is_connected(self) -> bool: ...
    @property
    def device_id(self) -> str: ...
    async def start_notify(self, cb: Callable[[bytes], None]) -> None: ...
    async def write_control(self, data: bytes) -> None: ...
    async def read_status(self) -> bytes: ...
    async def write_time(self, data: bytes) -> None: ...
    async def read_time(self) -> bytes: ...
    async def read_battery(self) -> int | None: ...
    async def read_firmware_rev(self) -> str | None: ...


@dataclass
class Progress:
    bytes_done: int = 0
    packets_done: int = 0
    packets_total: int = 0
    started: float = field(default_factory=time.monotonic)

    @property
    def rate_kbps(self) -> float:
        dt = time.monotonic() - self.started
        return (self.bytes_done / 1024.0) / dt if dt > 0.5 else 0.0


ProgressCb = Callable[[Progress], None]


class DumpClient:
    def __init__(self, transport: Transport) -> None:
        self.t = transport
        self._queue: asyncio.Queue[P.Notification] = asyncio.Queue()
        self._started = False

    # --- lifecycle -----------------------------------------------------------
    async def open(self) -> None:
        if not self.t.is_connected:
            await self.t.connect()
        if not self._started:
            await self.t.start_notify(self._on_notify)
            self._started = True
        # A previous (crashed) session may have left a transfer running.
        await self.t.write_control(P.encode_stop())
        await self._drain(0.3)

    async def close(self) -> None:
        try:
            if self.t.is_connected:
                await self.t.write_control(P.encode_stop())
        except Exception:  # noqa: BLE001 - best effort on the way out
            pass
        await self.t.disconnect()

    async def reconnect(self, attempts: int = 5, backoff_s: float = 2.0) -> None:
        last: Exception | None = None
        for i in range(1, attempts + 1):
            try:
                await self.t.disconnect()
            except Exception:  # noqa: BLE001
                pass
            try:
                log.info("reconnecting (attempt %d/%d)...", i, attempts)
                await self.t.connect()
                self._started = False
                await self.open()
                return
            except Exception as e:  # noqa: BLE001
                last = e
                await asyncio.sleep(backoff_s * i)
        raise DeviceError(f"could not reconnect after {attempts} attempts: {last}")

    def _on_notify(self, data: bytes) -> None:
        try:
            self._queue.put_nowait(P.parse_notification(data))
        except P.ProtocolError as e:
            log.warning("ignoring bad notification: %s", e)

    async def _drain(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(self._queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return

    async def _next(self, timeout: float) -> P.Notification:
        deadline = time.monotonic() + timeout
        while True:
            if not self.t.is_connected:
                raise TransferError("disconnected")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransferError(f"no response from device within {timeout:.0f}s")
            try:
                return await asyncio.wait_for(self._queue.get(), timeout=min(0.5, remaining))
            except asyncio.TimeoutError:
                continue

    # --- queries -------------------------------------------------------------
    async def info(self) -> P.Info:
        await self._drain(0)
        await self.t.write_control(P.encode_info())
        deadline = time.monotonic() + READ_BEGIN_TIMEOUT_S
        while True:
            n = await self._next(max(0.1, deadline - time.monotonic()))
            if isinstance(n, P.Info):
                return n
            if isinstance(n, P.Ack):
                raise DeviceError(f"INFO refused: {P.status_name(n.status)}")

    async def status(self) -> P.Status:
        return P.parse_status(await self.t.read_status())

    async def sync_time(self, epoch_s: int | None = None) -> int:
        epoch_s = int(time.time()) if epoch_s is None else int(epoch_s)
        await self.t.write_time(P.encode_time_sync(epoch_s))
        return epoch_s

    # --- reading (never deletes) ----------------------------------------------
    async def read_range(self, start_seq: int, count: int, on_chunk: Callable[[bytes], None] | None = None) -> bytes:
        """READ `count` records from `start_seq`. Verifies completeness; raises TransferError otherwise."""
        await self._drain(0)
        await self.t.write_control(P.encode_read(start_seq, count))

        begin: P.ReadBegin | None = None
        deadline = time.monotonic() + READ_BEGIN_TIMEOUT_S
        while begin is None:
            n = await self._next(max(0.1, deadline - time.monotonic()))
            if isinstance(n, P.ReadBegin):
                begin = n
            elif isinstance(n, P.Ack):
                retry = n.status == P.STATUS_NOT_READY
                raise TransferError(f"READ refused: {P.status_name(n.status)}", retryable=retry)
            elif isinstance(n, P.Done):
                raise TransferError(f"READ ended before it began: {P.status_name(n.status)}")
        if begin.start_seq != start_seq:
            raise TransferError(f"device started at seq {begin.start_seq}, asked for {start_seq}", retryable=False)
        expected = begin.packet_count
        if count and expected > count:
            raise TransferError("device returned more packets than requested", retryable=False)

        buf = bytearray()
        want = expected * P.RECORD_SIZE
        while True:
            try:
                n = await self._next(DATA_TIMEOUT_S)
            except TransferError as e:
                e.partial = bytes(buf)
                raise
            if isinstance(n, P.Data):
                buf += n.payload
                if on_chunk:
                    on_chunk(n.payload)
            elif isinstance(n, P.Done):
                if n.status != P.STATUS_OK:
                    raise TransferError(f"READ failed: {P.status_name(n.status)}", partial=bytes(buf))
                if len(buf) != want or n.next_seq != start_seq + expected:
                    raise TransferError(
                        f"incomplete transfer: got {len(buf)} of {want} bytes, next_seq {n.next_seq}",
                        partial=bytes(buf),
                    )
                return bytes(buf)
            elif isinstance(n, P.Ack):
                raise TransferError(f"unexpected ACK during READ: {P.status_name(n.status)}", partial=bytes(buf))

    async def pull(
        self,
        start_seq: int,
        end_seq: int,
        deliver: Callable[[int, bytes], Awaitable[None] | None],
        chunk_packets: int = 400,
        progress: ProgressCb | None = None,
        max_reconnects: int = 5,
    ) -> int:
        """Download [start_seq, end_seq) in chunks, resuming after disconnects.

        `deliver(seq, data)` receives whole records only, in order, exactly once.
        Returns the next seq to fetch (== end_seq on success).
        """
        prog = Progress(packets_total=end_seq - start_seq)
        seq = start_seq
        reconnects = 0
        while seq < end_seq:
            n = min(chunk_packets, end_seq - seq)
            try:
                data = await self.read_range(seq, n)
            except TransferError as e:
                whole = (len(e.partial) // P.RECORD_SIZE) * P.RECORD_SIZE
                if whole:
                    r = deliver(seq, e.partial[:whole])
                    if r is not None:
                        await r
                    got = whole // P.RECORD_SIZE
                    seq += got
                    prog.packets_done += got
                    prog.bytes_done += whole
                    if progress:
                        progress(prog)
                if not e.retryable or reconnects >= max_reconnects:
                    raise
                reconnects += 1
                log.warning("transfer interrupted at seq %d (%s); resuming", seq, e)
                if not self.t.is_connected:
                    await self.reconnect()
                else:
                    await self.t.write_control(P.encode_stop())
                    await self._drain(0.5)
                continue
            r = deliver(seq, data)
            if r is not None:
                await r
            seq += n
            prog.packets_done += n
            prog.bytes_done += len(data)
            if progress:
                progress(prog)
        return seq

    # --- explicit deletion ---------------------------------------------------
    async def _ack(self) -> P.Ack:
        deadline = time.monotonic() + ACK_TIMEOUT_S
        while True:
            n = await self._next(max(0.1, deadline - time.monotonic()))
            if isinstance(n, P.Ack):
                return n

    async def advance(self, seq: int) -> None:
        """EXPLICIT DELETE of every record before `seq`."""
        await self._drain(0)
        await self.t.write_control(P.encode_advance(seq))
        ack = await self._ack()
        if ack.status != P.STATUS_OK:
            raise DeviceError(f"delete refused: {P.status_name(ack.status)}")

    async def clear(self) -> None:
        """EXPLICIT DELETE of everything."""
        await self._drain(0)
        await self.t.write_control(P.encode_clear())
        ack = await self._ack()
        if ack.status != P.STATUS_OK:
            raise DeviceError(f"clear refused: {P.status_name(ack.status)}")


# --- bleak transport ------------------------------------------------------------
class BleakTransport:
    """Real BLE transport (imported lazily so tests never need bleak)."""

    def __init__(self, address: str | None = None, name: str | None = None, scan_timeout: float = 10.0) -> None:
        self.address = address
        self.name = name
        self.scan_timeout = scan_timeout
        self._client = None
        self._device = None
        self._notify_cb: Callable[[bytes], None] | None = None

    @property
    def device_id(self) -> str:
        return self.address or (self._device.address if self._device else "unknown")

    @property
    def is_connected(self) -> bool:
        return bool(self._client and self._client.is_connected)

    async def find(self):
        from bleak import BleakScanner

        if self._device is not None:
            return self._device
        if self.address:
            dev = await BleakScanner.find_device_by_address(self.address, timeout=self.scan_timeout)
        else:
            def match(d, adv):
                uuids = [u.lower() for u in (adv.service_uuids or [])]
                if P.LOCAL_STORAGE_SERVICE_UUID in uuids:
                    return self.name is None or (d.name or adv.local_name or "") == self.name
                return False

            dev = await BleakScanner.find_device_by_filter(match, timeout=self.scan_timeout)
        if dev is None:
            raise DeviceError(
                "no Omi local-only recorder found (is it powered on, in range, and running the local-only firmware?)"
            )
        self._device = dev
        self.address = dev.address
        return dev

    async def connect(self) -> None:
        from bleak import BleakClient

        dev = await self.find()

        def on_disconnect(_client):
            log.info("BLE disconnected")

        self._client = BleakClient(dev, disconnected_callback=on_disconnect, timeout=20.0)
        await self._client.connect()
        if self._notify_cb:
            await self.start_notify(self._notify_cb)

    async def disconnect(self) -> None:
        if self._client:
            try:
                await self._client.disconnect()
            finally:
                self._client = None

    async def start_notify(self, cb: Callable[[bytes], None]) -> None:
        self._notify_cb = cb
        if not self._client:
            return
        await self._client.start_notify(P.LOCAL_STORAGE_CONTROL_UUID, lambda _h, data: cb(bytes(data)))

    async def write_control(self, data: bytes) -> None:
        await self._client.write_gatt_char(P.LOCAL_STORAGE_CONTROL_UUID, data, response=True)

    async def read_status(self) -> bytes:
        return bytes(await self._client.read_gatt_char(P.LOCAL_STORAGE_STATUS_UUID))

    async def write_time(self, data: bytes) -> None:
        await self._client.write_gatt_char(P.TIME_SYNC_WRITE_UUID, data, response=True)

    async def read_time(self) -> bytes:
        return bytes(await self._client.read_gatt_char(P.TIME_SYNC_READ_UUID))

    async def read_battery(self) -> int | None:
        try:
            return int((await self._client.read_gatt_char(P.BATTERY_LEVEL_UUID))[0])
        except Exception:  # noqa: BLE001
            return None

    async def read_firmware_rev(self) -> str | None:
        try:
            return bytes(await self._client.read_gatt_char(P.DIS_FIRMWARE_REV_UUID)).decode(errors="replace")
        except Exception:  # noqa: BLE001
            return None

"""A software model of the local-only firmware's storage service + BLE link.

Mirrors omi/firmware/omi/src/lib/core/storage.c and sd_card.c closely enough to
exercise the host client: ring bounded by capacity, READ never moves read_seq,
ADVANCE/CLEAR are the only deletion paths, and a scripted mid-transfer
disconnect drops the link (which aborts the transfer) without touching the ring.
"""

from __future__ import annotations

import asyncio
import struct
from typing import Callable

from omi_local import protocol as P

OPUS_TOC_CELT_WB_20MS = 0xB8  # config 23 (CELT WB 20 ms), mono, one frame


def make_frame(seed: int, length: int = 80) -> bytes:
    body = bytes(((seed * 31 + i * 7) & 0xFF) for i in range(length - 1))
    return bytes([OPUS_TOC_CELT_WB_20MS]) + body


def make_record(seq: int, timestamp: int, frames_per_record: int = 5) -> bytes:
    payload = bytearray()
    for k in range(frames_per_record):
        f = make_frame(seq * 16 + k)
        payload += bytes([len(f)]) + f
    payload += bytes(P.AUDIO_PAYLOAD_BYTES - len(payload))
    return struct.pack(">I", timestamp) + bytes(payload)


class FakeRing:
    """The SD ring as the firmware exposes it: seq-addressed, never overwrites."""

    def __init__(self, capacity: int = 100000) -> None:
        self.capacity = capacity
        self.read_seq = 0
        self.write_seq = 0
        self.dropped = 0
        self.records: dict[int, bytes] = {}
        self.full = False

    def record(self, timestamp: int) -> bool:
        if self.write_seq - self.read_seq >= self.capacity:
            self.full = True
            self.dropped += 1
            return False
        self.full = False
        self.records[self.write_seq] = make_record(self.write_seq, timestamp)
        self.write_seq += 1
        return True

    def record_session(self, start_ts: int, n: int, step_s: float = 0.1) -> None:
        for k in range(n):
            self.record(int(start_ts + k * step_s) if start_ts else 0)

    def advance(self, seq: int) -> int:
        if seq < self.read_seq or seq > self.write_seq:
            return P.STATUS_SEQ_OUT_OF_RANGE
        for s in range(self.read_seq, seq):
            self.records.pop(s, None)
        self.read_seq = seq
        self.full = False
        return P.STATUS_OK

    def clear(self) -> None:
        self.records.clear()
        self.read_seq = self.write_seq = 0
        self.dropped = 0
        self.full = False


class FakeTransport:
    """Implements omi_local.device.Transport against a FakeRing."""

    def __init__(self, ring: FakeRing, mtu_payload: int = 494, disconnect_after_bytes: list[int] | None = None,
                 rtc_valid: bool = True) -> None:
        self.ring = ring
        self.mtu_payload = mtu_payload
        self.connected = False
        self.connect_count = 0
        self._cb: Callable[[bytes], None] | None = None
        self.commands: list[bytes] = []
        self.time_writes: list[int] = []
        self.rtc_valid = rtc_valid
        # Each entry: after this many DATA bytes of a transfer, drop the link once.
        self.disconnect_after_bytes = list(disconnect_after_bytes or [])
        self._task: asyncio.Task | None = None
        self._stop = False

    # -- Transport
    @property
    def device_id(self) -> str:
        return "FAKE-DEVICE-0001"

    @property
    def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True
        self.connect_count += 1

    async def disconnect(self) -> None:
        self.connected = False
        self._stop = True
        if self._task and not self._task.done():
            await asyncio.sleep(0)

    async def start_notify(self, cb: Callable[[bytes], None]) -> None:
        self._cb = cb

    async def write_control(self, data: bytes) -> None:
        assert self.connected, "write on a disconnected link"
        self.commands.append(bytes(data))
        op = data[0]
        if op == P.CMD_STOP:
            self._stop = True
            self._notify(bytes([P.NOTIFY_ACK, P.STATUS_OK]))
        elif op == P.CMD_INFO:
            self._notify(struct.pack(">BQQIQHB", P.NOTIFY_INFO, self.ring.read_seq, self.ring.write_seq,
                                     self.ring.capacity, self.ring.dropped, P.RECORD_SIZE, P.CODEC_ID_OPUS))
        elif op == P.CMD_READ:
            _, start, count = struct.unpack(">BQI", data)
            self._stop = False
            self._task = asyncio.create_task(self._serve_read(start, count))
        elif op == P.CMD_ADVANCE:
            _, seq = struct.unpack(">BQ", data)
            self._notify(bytes([P.NOTIFY_ACK, self.ring.advance(seq)]))
        elif op == P.CMD_CLEAR:
            self.ring.clear()
            self._notify(bytes([P.NOTIFY_ACK, P.STATUS_OK]))
        else:
            self._notify(bytes([P.NOTIFY_ACK, P.STATUS_INVALID_COMMAND]))

    async def read_status(self) -> bytes:
        unread = self.ring.write_seq - self.ring.read_seq
        return struct.pack("<IIII", unread * P.RECORD_SIZE, unread,
                           (self.ring.capacity - unread) * P.RECORD_SIZE, 1 if self.rtc_valid else 0)

    async def write_time(self, data: bytes) -> None:
        self.time_writes.append(struct.unpack("<I", data)[0])
        self.rtc_valid = True

    async def read_time(self) -> bytes:
        return struct.pack("<I", self.time_writes[-1] if self.time_writes else 0)

    async def read_battery(self) -> int | None:
        return 77

    async def read_firmware_rev(self) -> str | None:
        return "3.0.21-local.1"

    # -- firmware model
    def _notify(self, data: bytes) -> None:
        if self.connected and self._cb:
            self._cb(data)

    async def _serve_read(self, start: int, count: int) -> None:
        r = self.ring
        if start < r.read_seq or start > r.write_seq:
            self._notify(bytes([P.NOTIFY_ACK, P.STATUS_SEQ_OUT_OF_RANGE]))
            return
        avail = r.write_seq - start
        n = avail if (count == 0 or count > avail) else count
        self._notify(struct.pack(">BQI", P.NOTIFY_READ_BEGIN, start, n))
        stream = b"".join(r.records[s] for s in range(start, start + n))
        sent = 0
        while sent < len(stream):
            await asyncio.sleep(0)
            if self._stop or not self.connected:
                return  # firmware: storage_stop_transfer(); ring untouched
            chunk = stream[sent:sent + self.mtu_payload]
            self._notify(bytes([P.NOTIFY_DATA]) + chunk)
            sent += len(chunk)
            if self.disconnect_after_bytes and sent >= self.disconnect_after_bytes[0]:
                self.disconnect_after_bytes.pop(0)
                self.connected = False  # link drop; the client will time out and reconnect
                return
        self._notify(struct.pack(">BBQ", P.NOTIFY_DONE, P.STATUS_OK, start + n))

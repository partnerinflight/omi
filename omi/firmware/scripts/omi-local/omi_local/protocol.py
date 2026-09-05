"""Wire protocol of the local-only recorder firmware (omi/firmware/omi/src/lib/core/storage.c).

Pure data helpers: no BLE, no I/O, fully unit-testable.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Iterable, Iterator

# --- GATT UUIDs -------------------------------------------------------------
# The storage service UUIDs are specific to the local-only firmware and are NOT
# the Omi app's storage service (30295780-...), so the app never finds them.
LOCAL_STORAGE_SERVICE_UUID = "7d2c0001-9a6b-4e2f-b1c3-5a0f0c41ed10"
LOCAL_STORAGE_CONTROL_UUID = "7d2c0002-9a6b-4e2f-b1c3-5a0f0c41ed10"  # write cmds / notify
LOCAL_STORAGE_STATUS_UUID = "7d2c0003-9a6b-4e2f-b1c3-5a0f0c41ed10"  # read 16-byte status

# Unchanged upstream services that the CLI also uses.
TIME_SYNC_WRITE_UUID = "19b10031-e8f2-537e-4f6c-d104768a1214"  # u32 LE epoch seconds
TIME_SYNC_READ_UUID = "19b10032-e8f2-537e-4f6c-d104768a1214"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
DIS_FIRMWARE_REV_UUID = "00002a26-0000-1000-8000-00805f9b34fb"
DIS_MODEL_UUID = "00002a24-0000-1000-8000-00805f9b34fb"

# --- Commands / notifications ----------------------------------------------
CMD_STOP = 0x03
CMD_INFO = 0x10
CMD_READ = 0x11
CMD_ADVANCE = 0x12  # explicit delete up to seq
CMD_CLEAR = 0x13  # explicit delete everything

NOTIFY_ACK = 0x01
NOTIFY_INFO = 0x02
NOTIFY_DATA = 0x03
NOTIFY_DONE = 0x04
NOTIFY_READ_BEGIN = 0x05

STATUS_OK = 0
STATUS_INVALID_COMMAND = 6
STATUS_NOT_READY = 9
STATUS_SEQ_OUT_OF_RANGE = 10
STATUS_NAMES = {
    STATUS_OK: "ok",
    STATUS_INVALID_COMMAND: "invalid command",
    STATUS_NOT_READY: "storage not ready",
    STATUS_SEQ_OUT_OF_RANGE: "sequence out of range",
}

# --- Ring record layout -----------------------------------------------------
TIMESTAMP_BYTES = 4
AUDIO_PAYLOAD_BYTES = 440
RECORD_SIZE = TIMESTAMP_BYTES + AUDIO_PAYLOAD_BYTES  # 444

CODEC_ID_OPUS = 21
OPUS_SAMPLE_RATE = 16000
OPUS_CHANNELS = 1


def status_name(status: int) -> str:
    return STATUS_NAMES.get(status, f"status {status}")


# --- Encoders ----------------------------------------------------------------
def encode_info() -> bytes:
    return bytes([CMD_INFO])


def encode_read(start_seq: int, count: int = 0) -> bytes:
    """READ `count` packets from `start_seq`; count 0 means up to write_seq."""
    if start_seq < 0 or count < 0 or count > 0xFFFFFFFF:
        raise ValueError("invalid read range")
    return struct.pack(">BQI", CMD_READ, start_seq, count)


def encode_advance(seq: int) -> bytes:
    """Explicit delete of everything before `seq` (moves the ring read pointer)."""
    if seq < 0:
        raise ValueError("invalid seq")
    return struct.pack(">BQ", CMD_ADVANCE, seq)


def encode_clear() -> bytes:
    return bytes([CMD_CLEAR])


def encode_stop() -> bytes:
    return bytes([CMD_STOP])


def encode_time_sync(epoch_s: int) -> bytes:
    """The firmware memcpy()s the 4 bytes into a uint32 on a little-endian MCU."""
    return struct.pack("<I", int(epoch_s) & 0xFFFFFFFF)


# --- Decoders ----------------------------------------------------------------
@dataclass(frozen=True)
class Ack:
    status: int


@dataclass(frozen=True)
class Info:
    read_seq: int
    write_seq: int
    capacity_packets: int
    dropped_packets: int
    packet_size: int
    codec_id: int | None = None

    @property
    def unread_packets(self) -> int:
        return max(0, self.write_seq - self.read_seq)

    @property
    def unread_bytes(self) -> int:
        return self.unread_packets * self.packet_size

    @property
    def free_packets(self) -> int:
        return max(0, self.capacity_packets - self.unread_packets)


@dataclass(frozen=True)
class Data:
    payload: bytes


@dataclass(frozen=True)
class Done:
    status: int
    next_seq: int


@dataclass(frozen=True)
class ReadBegin:
    start_seq: int
    packet_count: int


@dataclass(frozen=True)
class Status:
    used_bytes: int
    unread_packets: int
    free_bytes: int
    rtc_valid: bool


Notification = Ack | Info | Data | Done | ReadBegin


class ProtocolError(ValueError):
    pass


def parse_notification(value: bytes | bytearray) -> Notification:
    if not value:
        raise ProtocolError("empty notification")
    op = value[0]
    if op == NOTIFY_ACK:
        if len(value) < 2:
            raise ProtocolError("short ACK")
        return Ack(value[1])
    if op == NOTIFY_INFO:
        if len(value) < 31:
            raise ProtocolError("short INFO")
        read_seq, write_seq, cap, dropped, pkt = struct.unpack_from(">QQIQH", value, 1)
        codec = value[31] if len(value) >= 32 else None
        return Info(read_seq, write_seq, cap, dropped, pkt, codec)
    if op == NOTIFY_DATA:
        return Data(bytes(value[1:]))
    if op == NOTIFY_DONE:
        if len(value) < 10:
            raise ProtocolError("short DONE")
        status, next_seq = struct.unpack_from(">BQ", value, 1)
        return Done(status, next_seq)
    if op == NOTIFY_READ_BEGIN:
        if len(value) < 13:
            raise ProtocolError("short READ_BEGIN")
        start, count = struct.unpack_from(">QI", value, 1)
        return ReadBegin(start, count)
    raise ProtocolError(f"unknown notification opcode 0x{op:02x}")


def parse_status(value: bytes | bytearray) -> Status:
    if len(value) < 16:
        raise ProtocolError("short status read")
    used, unread, free, rtc = struct.unpack_from("<IIII", value, 0)
    return Status(used, unread, free, bool(rtc))


# --- Records -----------------------------------------------------------------
@dataclass(frozen=True)
class Record:
    seq: int
    timestamp: int  # UTC epoch seconds, 0 = clock was not set when recorded
    frames: tuple[bytes, ...]

    @property
    def has_time(self) -> bool:
        return self.timestamp != 0


def parse_record(seq: int, raw: bytes) -> Record:
    """Parse one 444-byte ring record: [u32 BE timestamp][len:u8][opus]... zero padded."""
    if len(raw) != RECORD_SIZE:
        raise ProtocolError(f"record {seq}: expected {RECORD_SIZE} bytes, got {len(raw)}")
    timestamp = struct.unpack_from(">I", raw, 0)[0]
    frames: list[bytes] = []
    i = TIMESTAMP_BYTES
    end = RECORD_SIZE
    while i < end:
        n = raw[i]
        if n == 0:
            break  # padding
        i += 1
        if i + n > end:
            raise ProtocolError(f"record {seq}: frame length {n} overruns record")
        frames.append(bytes(raw[i : i + n]))
        i += n
    return Record(seq, timestamp, tuple(frames))


def iter_records(start_seq: int, data: bytes) -> Iterator[Record]:
    """Split a byte stream (as delivered by READ) into records. Trailing partial bytes are ignored."""
    whole = len(data) // RECORD_SIZE
    for k in range(whole):
        yield parse_record(start_seq + k, data[k * RECORD_SIZE : (k + 1) * RECORD_SIZE])


# --- Opus packet timing -------------------------------------------------------
_SILK_MS = (10.0, 20.0, 40.0, 60.0)
_HYBRID_MS = (10.0, 20.0)
_CELT_MS = (2.5, 5.0, 10.0, 20.0)


def opus_packet_duration_ms(packet: bytes) -> float:
    """Audio duration of an Opus packet from its TOC byte (RFC 6716 §3.1)."""
    if not packet:
        return 0.0
    toc = packet[0]
    config = toc >> 3
    code = toc & 0x03
    if config < 12:
        frame_ms = _SILK_MS[config % 4]
    elif config < 16:
        frame_ms = _HYBRID_MS[config % 2]
    else:
        frame_ms = _CELT_MS[config % 4]
    if code == 0:
        n = 1
    elif code in (1, 2):
        n = 2
    else:
        n = packet[1] & 0x3F if len(packet) > 1 else 0
    return frame_ms * n


def opus_packet_samples_48k(packet: bytes) -> int:
    return int(round(opus_packet_duration_ms(packet) * 48))


# --- Sessions ------------------------------------------------------------------
@dataclass
class Session:
    """A run of consecutive records with no large time gap (one 'recording')."""

    start_seq: int
    end_seq: int  # exclusive
    first_timestamp: int
    last_timestamp: int
    frames: int = 0
    audio_ms: float = 0.0
    seqs_missing: int = 0

    @property
    def packets(self) -> int:
        return self.end_seq - self.start_seq

    @property
    def has_time(self) -> bool:
        return self.first_timestamp != 0


def session_boundary(prev: Record | None, cur: Record, gap_s: int) -> bool:
    """True if `cur` should start a new session after `prev`."""
    if prev is None:
        return True
    if cur.seq != prev.seq + 1:
        return True
    if prev.has_time != cur.has_time:
        return True
    if cur.has_time and (cur.timestamp - prev.timestamp > gap_s or cur.timestamp < prev.timestamp - 5):
        return True
    return False


def split_sessions(records: Iterable[Record], gap_s: int = 60) -> list[Session]:
    sessions: list[Session] = []
    prev: Record | None = None
    for rec in records:
        if session_boundary(prev, rec, gap_s):
            sessions.append(Session(rec.seq, rec.seq, rec.timestamp, rec.timestamp))
        s = sessions[-1]
        s.end_seq = rec.seq + 1
        s.last_timestamp = rec.timestamp
        s.frames += len(rec.frames)
        s.audio_ms += sum(opus_packet_duration_ms(f) for f in rec.frames)
        prev = rec
    return sessions

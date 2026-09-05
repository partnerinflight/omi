"""Minimal Ogg Opus muxer (RFC 3533 + RFC 7845) so dumps are directly playable.

Written from scratch on purpose: no external audio libraries, no network.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import BinaryIO, Iterator

from .protocol import opus_packet_samples_48k

_CRC_TABLE: list[int] = []


def _crc_table() -> list[int]:
    if not _CRC_TABLE:
        for i in range(256):
            r = i << 24
            for _ in range(8):
                r = ((r << 1) ^ 0x04C11DB7) if (r & 0x80000000) else (r << 1)
                r &= 0xFFFFFFFF
            _CRC_TABLE.append(r)
    return _CRC_TABLE


def ogg_crc(data: bytes) -> int:
    """Ogg's CRC-32: poly 0x04c11db7, init 0, no reflection, no final xor."""
    table = _crc_table()
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ table[((crc >> 24) & 0xFF) ^ b]
    return crc


def _lacing(length: int) -> bytes:
    full, rest = divmod(length, 255)
    return bytes([255] * full + [rest])


def build_page(serial: int, page_seq: int, granule: int, packets: list[bytes], header_type: int = 0) -> bytes:
    segments = b"".join(_lacing(len(p)) for p in packets)
    if len(segments) > 255:
        raise ValueError("too many segments for one Ogg page")
    body = b"".join(packets)
    header = struct.pack("<4sBBqIII", b"OggS", 0, header_type, granule, serial, page_seq, 0)
    header += bytes([len(segments)]) + segments
    crc = ogg_crc(header + body)
    return header[:22] + struct.pack("<I", crc) + header[26:] + body


def opus_head(channels: int, input_rate: int, pre_skip: int = 0) -> bytes:
    return b"OpusHead" + struct.pack("<BBHIhB", 1, channels, pre_skip, input_rate, 0, 0)


def opus_tags(vendor: str = "omi-local", comments: list[str] | None = None) -> bytes:
    v = vendor.encode()
    out = b"OpusTags" + struct.pack("<I", len(v)) + v
    comments = comments or []
    out += struct.pack("<I", len(comments))
    for c in comments:
        cb = c.encode()
        out += struct.pack("<I", len(cb)) + cb
    return out


@dataclass
class MuxState:
    serial: int
    page_seq: int
    granule: int  # samples at 48 kHz completed so far

    def to_dict(self) -> dict:
        return {"serial": self.serial, "page_seq": self.page_seq, "granule": self.granule}

    @staticmethod
    def from_dict(d: dict) -> "MuxState":
        return MuxState(int(d["serial"]), int(d["page_seq"]), int(d["granule"]))


class OggOpusWriter:
    """Streams Opus packets into an Ogg container.

    `resume_state` lets a writer continue an existing file (appending pages with
    correct sequence numbers and granule positions) after an interrupted dump.
    """

    MAX_PACKETS_PER_PAGE = 50

    def __init__(
        self,
        fp: BinaryIO,
        serial: int,
        sample_rate: int = 16000,
        channels: int = 1,
        comments: list[str] | None = None,
        resume_state: MuxState | None = None,
    ) -> None:
        self._fp = fp
        self._pending: list[bytes] = []
        self._closed = False
        if resume_state is not None:
            self.state = resume_state
        else:
            self.state = MuxState(serial=serial, page_seq=0, granule=0)
            self._write_page([opus_head(channels, sample_rate)], header_type=0x02)  # BOS
            self._write_page([opus_tags(comments=comments)])

    def _write_page(self, packets: list[bytes], header_type: int = 0) -> None:
        self._fp.write(build_page(self.state.serial, self.state.page_seq, self.state.granule, packets, header_type))
        self.state.page_seq += 1

    def write_packet(self, packet: bytes) -> None:
        if self._closed:
            raise RuntimeError("writer closed")
        if not packet:
            return
        self._pending.append(packet)
        if len(self._pending) >= self.MAX_PACKETS_PER_PAGE:
            self.flush()

    def flush(self, eos: bool = False) -> None:
        if self._pending or eos:
            self.state.granule += sum(opus_packet_samples_48k(p) for p in self._pending)
            self._write_page(self._pending, header_type=0x04 if eos else 0)
            self._pending = []

    def close(self, eos: bool = True) -> MuxState:
        """Flush. With eos=False the file stays resumable (no EOS page)."""
        if not self._closed:
            self.flush(eos=eos)
            self._closed = True
        return self.state


# --- Reader (used by tests and `omi-local verify`) ----------------------------
@dataclass(frozen=True)
class OggPage:
    header_type: int
    granule: int
    serial: int
    page_seq: int
    packets: tuple[bytes, ...]
    crc_ok: bool


def iter_pages(data: bytes) -> Iterator[OggPage]:
    pos = 0
    while pos + 27 <= len(data):
        if data[pos : pos + 4] != b"OggS":
            raise ValueError(f"bad Ogg capture pattern at {pos}")
        _, version, htype, granule, serial, seq, crc, nseg = struct.unpack_from("<4sBBqIIIB", data, pos)
        seg_table = data[pos + 27 : pos + 27 + nseg]
        body_len = sum(seg_table)
        end = pos + 27 + nseg + body_len
        page = bytearray(data[pos:end])
        page[22:26] = b"\0\0\0\0"
        crc_ok = ogg_crc(bytes(page)) == crc
        packets: list[bytes] = []
        cur = bytearray()
        off = pos + 27 + nseg
        for lace in seg_table:
            cur += data[off : off + lace]
            off += lace
            if lace < 255:
                packets.append(bytes(cur))
                cur = bytearray()
        yield OggPage(htype, granule, serial, seq, tuple(packets), crc_ok)
        pos = end

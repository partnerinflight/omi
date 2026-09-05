"""Wire protocol of the Wi-Fi upload path (omi/firmware/omi/src/wifi_upload.c).

TCP, big-endian, each message is [type:u8][len:u32][payload]:
  C->S HELLO     0x01  "OMIL" ver:u8=1 device_id:6 client_nonce:16
  S->C CHALLENGE 0x02  server_nonce:16 server_tag:32
  C->S AUTH      0x03  client_tag:32 read:u64 write:u64 cap:u32 dropped:u64 pkt:u16 codec:u8
  S->C START     0x04  start_seq:u64            (or REJECT 0x7F reason:u8)
  C->S DATA      0x05  seq:u64 count:u16 records[count*444]
  S->C ACK       0x06  next_seq:u64             (receiver has PERSISTED < next_seq)
  C->S DONE      0x07  next_seq:u64
  S->C BYE       0x08  next_seq:u64
Tags are HMAC-SHA256(secret, label || client_nonce || server_nonce) with label
"omi-local-srv" (receiver proves itself first) / "omi-local-cli".

Pure data helpers + BLE provisioning TLVs; no sockets here.
"""

from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass

from . import protocol as P

MAGIC = b"OMIL"
VERSION = 1
NONCE_LEN = 16
TAG_LEN = 32
SECRET_LEN = 32
DEVICE_ID_LEN = 6
HEADER_LEN = 5
MAX_DATA_PAYLOAD = 10 + 36 * P.RECORD_SIZE  # one 16 KiB device chunk
MAX_CTRL_PAYLOAD = 64

MSG_HELLO = 0x01
MSG_CHALLENGE = 0x02
MSG_AUTH = 0x03
MSG_START = 0x04
MSG_DATA = 0x05
MSG_ACK = 0x06
MSG_DONE = 0x07
MSG_BYE = 0x08
MSG_REJECT = 0x7F

REJECT_AUTH = 1
REJECT_PROTOCOL = 2
REJECT_BUSY = 3

LABEL_SERVER = b"omi-local-srv"
LABEL_CLIENT = b"omi-local-cli"

# BLE provisioning characteristic (local storage service, 7D2C0004)
UPLOAD_CONFIG_UUID = "7d2c0004-9a6b-4e2f-b1c3-5a0f0c41ed10"
CMD_UPLOAD_NOW = 0x20
TLV_SSID = 0x01
TLV_PSK = 0x02
TLV_HOST = 0x03
TLV_PORT = 0x04
TLV_SECRET = 0x05
TLV_ENABLE = 0x06
TLV_FORGET = 0x7F


class UploadProtocolError(ValueError):
    pass


def frame(msg_type: int, payload: bytes = b"") -> bytes:
    return struct.pack(">BI", msg_type, len(payload)) + payload


def parse_header(hdr: bytes) -> tuple[int, int]:
    if len(hdr) != HEADER_LEN:
        raise UploadProtocolError("short frame header")
    return struct.unpack(">BI", hdr)


def auth_tag(secret: bytes, label: bytes, client_nonce: bytes, server_nonce: bytes) -> bytes:
    return hmac.new(secret, label + client_nonce + server_nonce, hashlib.sha256).digest()


@dataclass(frozen=True)
class Hello:
    version: int
    device_id: bytes
    client_nonce: bytes

    @property
    def device_id_str(self) -> str:
        return "-".join(f"{b:02X}" for b in self.device_id)


def parse_hello(payload: bytes) -> Hello:
    if len(payload) != 4 + 1 + DEVICE_ID_LEN + NONCE_LEN or payload[:4] != MAGIC:
        raise UploadProtocolError("bad HELLO")
    ver = payload[4]
    if ver != VERSION:
        raise UploadProtocolError(f"unsupported protocol version {ver}")
    return Hello(ver, bytes(payload[5:5 + DEVICE_ID_LEN]), bytes(payload[5 + DEVICE_ID_LEN:]))


def encode_hello(device_id: bytes, client_nonce: bytes) -> bytes:
    return MAGIC + bytes([VERSION]) + device_id + client_nonce


def encode_challenge(server_nonce: bytes, server_tag: bytes) -> bytes:
    return server_nonce + server_tag


@dataclass(frozen=True)
class Auth:
    client_tag: bytes
    info: P.Info


def parse_auth(payload: bytes) -> Auth:
    if len(payload) != TAG_LEN + 31:
        raise UploadProtocolError("bad AUTH")
    tag = bytes(payload[:TAG_LEN])
    read_seq, write_seq, cap, dropped, pkt, codec = struct.unpack_from(">QQIQHB", payload, TAG_LEN)
    return Auth(tag, P.Info(read_seq, write_seq, cap, dropped, pkt, codec))


def encode_auth(client_tag: bytes, info: P.Info) -> bytes:
    return client_tag + struct.pack(">QQIQHB", info.read_seq, info.write_seq, info.capacity_packets,
                                    info.dropped_packets, info.packet_size, info.codec_id or 0)


def encode_u64(v: int) -> bytes:
    return struct.pack(">Q", v)


def parse_u64(payload: bytes) -> int:
    if len(payload) != 8:
        raise UploadProtocolError("expected 8-byte payload")
    return struct.unpack(">Q", payload)[0]


@dataclass(frozen=True)
class DataChunk:
    seq: int
    count: int
    records: bytes


def parse_data(payload: bytes) -> DataChunk:
    if len(payload) < 10:
        raise UploadProtocolError("short DATA")
    seq, count = struct.unpack_from(">QH", payload, 0)
    records = bytes(payload[10:])
    if len(records) != count * P.RECORD_SIZE:
        raise UploadProtocolError(f"DATA count {count} does not match {len(records)} bytes")
    return DataChunk(seq, count, records)


def encode_data(seq: int, records: bytes) -> bytes:
    if len(records) % P.RECORD_SIZE:
        raise ValueError("records must be whole")
    return struct.pack(">QH", seq, len(records) // P.RECORD_SIZE) + records


# --- provisioning TLVs ----------------------------------------------------------
def encode_wifi_config(*, ssid: str | None = None, password: str | None = None, host: str | None = None,
                       port: int | None = None, secret: bytes | None = None, enabled: bool | None = None,
                       forget: bool = False) -> bytes:
    """Build the TLV blob for the BLE config characteristic (firmware wifi_upload_apply_tlv)."""
    out = bytearray()

    def tlv(t: int, v: bytes) -> None:
        if len(v) > 255:
            raise ValueError("TLV too long")
        out.extend(bytes([t, len(v)]) + v)

    if forget:
        tlv(TLV_FORGET, b"")
        return bytes(out)
    if ssid is not None:
        b = ssid.encode()
        if not 1 <= len(b) <= 32:
            raise ValueError("SSID must be 1..32 bytes")
        tlv(TLV_SSID, b)
    if password is not None:
        b = password.encode()
        if b and not 8 <= len(b) <= 64:
            raise ValueError("WPA2 password must be 8..64 bytes (or empty for an open network)")
        tlv(TLV_PSK, b)
    if host is not None:
        parts = host.split(".")
        if len(parts) != 4 or not all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
            raise ValueError("host must be a dotted IPv4 address (the firmware has no DNS)")
        tlv(TLV_HOST, bytes(int(p) for p in parts))
    if port is not None:
        if not 1 <= port <= 65535:
            raise ValueError("port out of range")
        tlv(TLV_PORT, struct.pack(">H", port))
    if secret is not None:
        if len(secret) != SECRET_LEN:
            raise ValueError("secret must be 32 bytes")
        tlv(TLV_SECRET, secret)
    if enabled is not None:
        tlv(TLV_ENABLE, bytes([1 if enabled else 0]))
    if not out:
        raise ValueError("nothing to configure")
    return bytes(out)


UPLOAD_STATES = ["idle", "wait-sd", "wifi-up", "connecting", "dhcp", "tcp", "auth", "uploading", "teardown"]
UPLOAD_RESULTS = ["ok", "not configured", "sd not ready", "wifi connect failed", "dhcp timeout",
                  "tcp connect failed", "receiver auth failed", "protocol error", "ring read error",
                  "link lost", "aborted (charger removed / busy)", "busy", "nothing to upload"]


@dataclass(frozen=True)
class UploadStatus:
    configured: bool
    state: int
    last_result: int
    last_config_err: int
    last_errno: int
    sessions_ok: int
    packets_uploaded: int
    last_attempt_uptime_s: int
    heap_free: int
    heap_max_used: int

    @property
    def state_name(self) -> str:
        return UPLOAD_STATES[self.state] if self.state < len(UPLOAD_STATES) else f"state {self.state}"

    @property
    def result_name(self) -> str:
        return UPLOAD_RESULTS[self.last_result] if self.last_result < len(UPLOAD_RESULTS) else f"result {self.last_result}"


def parse_upload_status(value: bytes) -> UploadStatus:
    if len(value) < 28:
        raise UploadProtocolError("short upload status")
    configured, state, result, cfg_err, errno_, ok, pkts, last, heap_free, heap_max = struct.unpack_from(
        "<BBBbiIIIII", value, 0)
    return UploadStatus(bool(configured), state, result, cfg_err, errno_, ok, pkts, last, heap_free, heap_max)

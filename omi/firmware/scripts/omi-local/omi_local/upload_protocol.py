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

Protocol v2 (file clients such as the Mac meeting-capture app): HELLO ver=2,
the same CHALLENGE, then AUTH carrying only client_tag:32, answered by OK 0x18.
  C->S CAPTURE_OPEN   0x10  capture_id:16 start_ms:u64 app:utf8(<=256)   -> OK
  C->S CAPTURE_CANCEL 0x11  capture_id:16                                -> OK
  C->S FILE_BEGIN     0x12  capture_id:16 total_len:u64 sha256:32 json   -> FILE_START 0x15 offset:u64
  C->S FILE_DATA      0x13  offset:u64 bytes(<=64 KiB)                   -> FILE_ACK 0x16 persisted:u64
  C->S FILE_END       0x14                                               -> FILE_BYE 0x17 committed:u8
FILE_BEGIN json must contain start_ms and end_ms (int ms since epoch, start <=
end) and app (bundle id); other keys (e.g. channels) are kept in the sidecar.
Offsets must equal the durable length; violations are REJECT_PROTOCOL.
FILE_START offset == total_len means the file is already committed; the client
still sends FILE_END to get FILE_BYE. FILE_DATA beyond the declared length is
rejected (partial kept); FILE_END with fewer bytes than declared is rejected
(partial kept, resumable); only a SHA-256 mismatch at FILE_END discards the
partial. Any REJECT closes the connection; to resume, reconnect and send
FILE_BEGIN again. A cancelled capture is final: its partial is deleted and a
later FILE_BEGIN for it is REJECT_PROTOCOL. Only one v2 connection per client
id at a time (otherwise REJECT_BUSY; clients should retry). Idle connections
are dropped after 60 s, so open one only when there is work. The client closes
the socket when done.

Pure data helpers + BLE provisioning TLVs; no sockets here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import struct
from dataclasses import dataclass

from . import protocol as P

MAGIC = b"OMIL"
VERSION = 1
VERSION_FILE = 2
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

# Protocol v2 (file client, e.g. the Mac meeting-capture app)
MSG_CAPTURE_OPEN = 0x10
MSG_CAPTURE_CANCEL = 0x11
MSG_FILE_BEGIN = 0x12
MSG_FILE_DATA = 0x13
MSG_FILE_END = 0x14
MSG_FILE_START = 0x15
MSG_FILE_ACK = 0x16
MSG_FILE_BYE = 0x17
MSG_OK = 0x18

CAPTURE_ID_LEN = 16
SHA256_LEN = 32
MAX_APP_LEN = 256
MAX_METADATA_LEN = 4096
MAX_FILE_CHUNK = 64 * 1024
MAX_FILE_PAYLOAD = 8 + MAX_FILE_CHUNK  # largest v2 frame (FILE_DATA); FILE_BEGIN is smaller

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
TLV_HOSTNAME = 0x07
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
    if ver not in (VERSION, VERSION_FILE):
        raise UploadProtocolError(f"unsupported protocol version {ver}")
    return Hello(ver, bytes(payload[5:5 + DEVICE_ID_LEN]), bytes(payload[5 + DEVICE_ID_LEN:]))


def encode_hello(device_id: bytes, client_nonce: bytes, version: int = VERSION) -> bytes:
    return MAGIC + bytes([version]) + device_id + client_nonce


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


# --- protocol v2: file client -----------------------------------------------------
@dataclass(frozen=True)
class FileAuth:
    client_tag: bytes


def parse_file_auth(payload: bytes) -> FileAuth:
    if len(payload) != TAG_LEN:
        raise UploadProtocolError("bad v2 AUTH")
    return FileAuth(bytes(payload))


@dataclass(frozen=True)
class CaptureOpen:
    capture_id: bytes
    start_ms: int
    app: str


def encode_capture_open(capture_id: bytes, start_ms: int, app: str) -> bytes:
    if len(capture_id) != CAPTURE_ID_LEN:
        raise ValueError("capture_id must be 16 bytes")
    app_b = app.encode("utf-8")
    if len(app_b) > MAX_APP_LEN:
        raise ValueError("app too long")
    return capture_id + struct.pack(">Q", start_ms) + app_b


def parse_capture_open(payload: bytes) -> CaptureOpen:
    head = CAPTURE_ID_LEN + 8
    if len(payload) < head or len(payload) - head > MAX_APP_LEN:
        raise UploadProtocolError("bad CAPTURE_OPEN")
    try:
        app = bytes(payload[head:]).decode("utf-8")
    except UnicodeDecodeError as e:
        raise UploadProtocolError("CAPTURE_OPEN app is not UTF-8") from e
    (start_ms,) = struct.unpack_from(">Q", payload, CAPTURE_ID_LEN)
    return CaptureOpen(bytes(payload[:CAPTURE_ID_LEN]), start_ms, app)


def parse_capture_id(payload: bytes) -> bytes:
    if len(payload) != CAPTURE_ID_LEN:
        raise UploadProtocolError("expected 16-byte capture id")
    return bytes(payload)


@dataclass(frozen=True)
class FileBegin:
    capture_id: bytes
    total_len: int
    sha256: bytes
    metadata: dict


def encode_file_begin(capture_id: bytes, total_len: int, sha256: bytes, metadata: dict) -> bytes:
    if len(capture_id) != CAPTURE_ID_LEN:
        raise ValueError("capture_id must be 16 bytes")
    if len(sha256) != SHA256_LEN:
        raise ValueError("sha256 must be 32 bytes")
    meta = json.dumps(metadata, sort_keys=True, allow_nan=False).encode("utf-8")
    if len(meta) > MAX_METADATA_LEN:
        raise ValueError("metadata too large")
    return capture_id + struct.pack(">Q", total_len) + sha256 + meta


def _reject_constant(name: str):
    raise ValueError(f"non-finite JSON constant {name}")


def parse_file_begin(payload: bytes) -> FileBegin:
    head = CAPTURE_ID_LEN + 8 + SHA256_LEN
    if len(payload) < head or len(payload) - head > MAX_METADATA_LEN:
        raise UploadProtocolError("bad FILE_BEGIN")
    try:
        metadata = json.loads(bytes(payload[head:]).decode("utf-8"), parse_constant=_reject_constant)
        json.dumps(metadata, ensure_ascii=False).encode("utf-8")  # rejects lone surrogates
    except (UnicodeError, ValueError, RecursionError) as e:
        raise UploadProtocolError("FILE_BEGIN metadata is not JSON") from e
    if not isinstance(metadata, dict):
        raise UploadProtocolError("FILE_BEGIN metadata must be an object")
    (total_len,) = struct.unpack_from(">Q", payload, CAPTURE_ID_LEN)
    return FileBegin(bytes(payload[:CAPTURE_ID_LEN]), total_len,
                     bytes(payload[CAPTURE_ID_LEN + 8:head]), metadata)


@dataclass(frozen=True)
class FileData:
    offset: int
    data: bytes


def encode_file_data(offset: int, data: bytes) -> bytes:
    if len(data) > MAX_FILE_CHUNK:
        raise ValueError("chunk too large")
    return struct.pack(">Q", offset) + data


def parse_file_data(payload: bytes) -> FileData:
    if len(payload) < 8 or len(payload) - 8 > MAX_FILE_CHUNK:
        raise UploadProtocolError("bad FILE_DATA")
    (offset,) = struct.unpack_from(">Q", payload, 0)
    return FileData(offset, bytes(payload[8:]))


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
        import re
        if len(host) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                                      for label in host.split(".")):
            raise ValueError("host must be an IPv4 address or DNS hostname (no URL/port)")
        tlv(TLV_HOSTNAME, host.encode("ascii"))
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


UPLOAD_STATES = ["idle", "wait-sd", "wifi-up", "connecting", "dhcp", "tcp", "auth", "uploading", "teardown", "setup"]
UPLOAD_RESULTS = ["ok", "not configured", "sd not ready", "wifi connect failed", "dhcp timeout",
                  "tcp connect failed", "receiver auth failed", "protocol error", "ring read error",
                  "link lost", "aborted", "busy", "nothing to upload"]


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
    dhcp_state: int | None = None
    dhcp_attempts: int | None = None
    ipv4: str | None = None
    last_stage: int | None = None
    retries_remaining: int | None = None

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
    dhcp_state = dhcp_attempts = ipv4 = None
    if len(value) >= 36:
        dhcp_state, dhcp_attempts = struct.unpack_from("<BB", value, 28)
        ipv4 = ".".join(str(b) for b in value[32:36])
    last_stage, retries = struct.unpack_from("<BB", value, 36) if len(value) >= 38 else (None, None)
    return UploadStatus(bool(configured), state, result, cfg_err, errno_, ok, pkts, last, heap_free, heap_max,
                        dhcp_state, dhcp_attempts, ipv4, last_stage, retries)

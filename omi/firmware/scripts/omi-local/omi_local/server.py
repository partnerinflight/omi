"""`omi-local serve`: the local receiver for the device's Wi-Fi upload path.

Runs on any machine with Python (Windows included). Speaks the protocol in
upload_protocol.py, authenticates the device with the shared secret, writes the
same per-session .opus files as `omi-local pull`, and only ACKs a chunk after it
has been written and fsync'ed. The device deletes only what was ACKed.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import secrets as _secrets
import time
from pathlib import Path

from . import protocol as P
from . import upload_protocol as U
from .state import StateStore

log = logging.getLogger("omi_local.serve")

CTRL_TIMEOUT_S = 30.0
DATA_TIMEOUT_S = 60.0


class SessionWriterFactory:
    """Indirection so tests can inject a writer; default is cli.SessionWriter."""

    def __init__(self, dest: Path, gap_s: int = 60, keep_raw: bool = False) -> None:
        self.dest = Path(dest)
        self.gap_s = gap_s
        self.keep_raw = keep_raw

    def make(self, device_id: str):
        from .cli import SessionWriter  # local import: cli pulls in argparse/bleak-free code only

        store = StateStore(self.dest)
        state = store.get(device_id)
        return SessionWriter(self.dest, device_id, state, store, gap_s=self.gap_s, keep_raw=self.keep_raw)


class UploadServer:
    def __init__(self, secret: bytes, dest: Path, host: str = "0.0.0.0", port: int = 7331,
                 writer_factory: SessionWriterFactory | None = None) -> None:
        if len(secret) != U.SECRET_LEN:
            raise ValueError("secret must be 32 bytes")
        self.secret = secret
        self.dest = Path(dest)
        self.host = host
        self.port = port
        self.writers = writer_factory or SessionWriterFactory(self.dest)
        self._server: asyncio.base_events.Server | None = None
        self._busy: set[str] = set()
        self.sessions_ok = 0
        self.sessions_failed = 0

    async def start(self) -> None:
        self.dest.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        addrs = ", ".join(str(s.getsockname()[:2]) for s in self._server.sockets or [])
        log.info("omi-local receiver listening on %s, writing to %s", addrs, self.dest)

    async def serve_forever(self) -> None:
        await self.start()
        assert self._server
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    @property
    def bound_port(self) -> int:
        assert self._server and self._server.sockets
        return self._server.sockets[0].getsockname()[1]

    # --- protocol -------------------------------------------------------------
    async def _read_frame(self, reader: asyncio.StreamReader, max_payload: int, timeout: float) -> tuple[int, bytes]:
        hdr = await asyncio.wait_for(reader.readexactly(U.HEADER_LEN), timeout)
        msg_type, length = U.parse_header(hdr)
        if length > max_payload:
            raise U.UploadProtocolError(f"frame too large ({length} bytes)")
        payload = await asyncio.wait_for(reader.readexactly(length), timeout) if length else b""
        return msg_type, payload

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, msg_type: int, payload: bytes = b"") -> None:
        writer.write(U.frame(msg_type, payload))
        await writer.drain()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        device = "?"
        session_writer = None
        ok = False
        try:
            msg_type, payload = await self._read_frame(reader, U.MAX_CTRL_PAYLOAD, CTRL_TIMEOUT_S)
            if msg_type != U.MSG_HELLO:
                raise U.UploadProtocolError("expected HELLO")
            hello = U.parse_hello(payload)
            device = hello.device_id_str
            server_nonce = _secrets.token_bytes(U.NONCE_LEN)
            await self._send(writer, U.MSG_CHALLENGE,
                             U.encode_challenge(server_nonce, U.auth_tag(self.secret, U.LABEL_SERVER,
                                                                         hello.client_nonce, server_nonce)))
            msg_type, payload = await self._read_frame(reader, U.MAX_CTRL_PAYLOAD, CTRL_TIMEOUT_S)
            if msg_type != U.MSG_AUTH:
                raise U.UploadProtocolError("expected AUTH")
            auth = U.parse_auth(payload)
            expected = U.auth_tag(self.secret, U.LABEL_CLIENT, hello.client_nonce, server_nonce)
            if not hmac.compare_digest(auth.client_tag, expected):
                log.warning("device %s from %s failed authentication", device, peer)
                await self._send(writer, U.MSG_REJECT, bytes([U.REJECT_AUTH]))
                return
            if device in self._busy:
                await self._send(writer, U.MSG_REJECT, bytes([U.REJECT_BUSY]))
                return
            self._busy.add(device)
            info = auth.info
            log.info("device %s connected from %s: ring [%d, %d), %d unread packets", device, peer,
                     info.read_seq, info.write_seq, info.unread_packets)

            session_writer = self.writers.make(device)
            state = session_writer.state
            start = info.read_seq
            if info.read_seq <= state.downloaded_through <= info.write_seq:
                start = max(start, state.downloaded_through)
            else:
                state.downloaded_through = info.read_seq
                state.open_file = None
            await self._send(writer, U.MSG_START, U.encode_u64(start))

            expected_seq = start
            t0 = time.monotonic()
            total = 0
            while True:
                msg_type, payload = await self._read_frame(reader, U.MAX_DATA_PAYLOAD, DATA_TIMEOUT_S)
                if msg_type == U.MSG_DATA:
                    chunk = U.parse_data(payload)
                    if chunk.seq != expected_seq:
                        raise U.UploadProtocolError(f"DATA seq {chunk.seq}, expected {expected_seq}")
                    # Validate before persisting: a corrupt record is never ACKed.
                    for _ in P.iter_records(chunk.seq, chunk.records):
                        pass
                    await asyncio.to_thread(self._persist, session_writer, chunk)
                    expected_seq += chunk.count
                    total += chunk.count
                    await self._send(writer, U.MSG_ACK, U.encode_u64(expected_seq))
                elif msg_type == U.MSG_DONE:
                    done_seq = U.parse_u64(payload)
                    if done_seq != expected_seq:
                        log.warning("device DONE at %d but we are at %d", done_seq, expected_seq)
                    await self._send(writer, U.MSG_BYE, U.encode_u64(expected_seq))
                    ok = True
                    break
                else:
                    raise U.UploadProtocolError(f"unexpected message 0x{msg_type:02x}")
            dt = time.monotonic() - t0
            rate = (total * P.RECORD_SIZE / 1024.0) / dt if dt > 0 else 0.0
            log.info("device %s: %d packets (%.1f KiB/s), files: %s", device, total, rate,
                     ", ".join(Path(f).name for f in session_writer.files_written) or "-")
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError) as e:
            log.warning("device %s: connection ended early (%s); nothing un-ACKed was deleted on the device",
                        device, type(e).__name__)
        except U.UploadProtocolError as e:
            log.error("device %s: protocol error: %s", device, e)
            try:
                await self._send(writer, U.MSG_REJECT, bytes([U.REJECT_PROTOCOL]))
            except Exception:  # noqa: BLE001
                pass
        finally:
            self._busy.discard(device)
            if session_writer is not None:
                session_writer.finish(final=ok)
            if ok:
                self.sessions_ok += 1
            else:
                self.sessions_failed += 1
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _persist(session_writer, chunk: U.DataChunk) -> None:
        """Write + fsync so an ACK really means 'on disk'."""
        session_writer.add(chunk.seq, chunk.records)
        session_writer.fsync()


# --- secret management --------------------------------------------------------------
def default_secret_path() -> Path:
    base = Path(os.environ.get("OMI_LOCAL_HOME", Path.home() / ".omi-local"))
    return base / "upload-secret.hex"


def load_or_create_secret(path: Path | None = None, create: bool = True) -> bytes:
    path = path or default_secret_path()
    if path.exists():
        data = bytes.fromhex(path.read_text().strip())
        if len(data) != U.SECRET_LEN:
            raise ValueError(f"{path}: secret must be 32 bytes (64 hex chars)")
        return data
    if not create:
        raise FileNotFoundError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _secrets.token_bytes(U.SECRET_LEN)
    path.write_text(data.hex() + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    log.info("created new upload secret at %s", path)
    return data

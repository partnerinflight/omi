from __future__ import annotations
import asyncio
import json
import secrets
import struct
import sys
from pathlib import Path
from omi_local import protocol as P, upload_protocol as U
from second_brain.config import Config
from second_brain.io import write_json

ROOT = Path(__file__).resolve().parents[1]
SECRET = bytes(range(32))
DEVICE = bytes.fromhex("112233445566")


def configuration(root: Path):
    for name in ["vault", "incoming", "data"]:
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "secret.hex").write_text(SECRET.hex())
    pipe = json.loads((ROOT / "config/pipeline.example.json").read_text())
    pipe.update(
        moss_command=[sys.executable, str(ROOT / "tests/fixtures/fake_moss.py")],
        scan_vault_for_novelty=False,
        long_silence_seconds=999,
        memory_gate_min_words=1,
    )
    write_json(root / "pipeline.json", pipe)
    cfg = Config(
        root / "data",
        root / "incoming",
        root / "secret.hex",
        root / "pipeline.json",
        root / "vault",
        root / "status.json",
        host="127.0.0.1",
        port=0,
        poll_seconds=0.05,
        retry_seconds=0.05,
        max_attempts=2,
        skip_vibe7=True,
        no_hermes=True,
    )
    return cfg


def records(count=20, marker=True):
    # Valid 20 ms Opus silence packets; ffmpeg decodes the actual uploaded container.
    payload = (b"\x03\xf8\xff\xfe" * 5).ljust(P.AUDIO_PAYLOAD_BYTES, b"\0")
    data = [struct.pack(">I", 1700000000 + i // 10) + payload for i in range(count)]
    if marker:
        data.append(struct.pack(">I", 1700000002) + P.RECORD_END_MAGIC.ljust(P.AUDIO_PAYLOAD_BYTES, b"\0"))
    return data


async def receive(reader):
    header = await reader.readexactly(U.HEADER_LEN)
    kind, length = U.parse_header(header)
    return kind, await reader.readexactly(length)


async def upload(port, data, secret=SECRET):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        cn = secrets.token_bytes(16)
        writer.write(U.frame(U.MSG_HELLO, U.encode_hello(DEVICE, cn)))
        await writer.drain()
        kind, challenge = await receive(reader)
        assert kind == U.MSG_CHALLENGE
        sn = challenge[:16]
        info = P.Info(0, len(data), 10000, 0, P.RECORD_SIZE, P.CODEC_ID_OPUS)
        writer.write(U.frame(U.MSG_AUTH, U.encode_auth(U.auth_tag(secret, U.LABEL_CLIENT, cn, sn), info)))
        await writer.drain()
        kind, response = await receive(reader)
        if kind == U.MSG_REJECT:
            return kind
        assert kind == U.MSG_START
        start = U.parse_u64(response)
        for index in range(start, len(data), 10):
            chunk = data[index : index + 10]
            writer.write(U.frame(U.MSG_DATA, U.encode_data(index, b"".join(chunk))))
            await writer.drain()
            kind, ack = await receive(reader)
            assert kind == U.MSG_ACK and U.parse_u64(ack) == index + len(chunk)
        writer.write(U.frame(U.MSG_DONE, U.encode_u64(len(data))))
        await writer.drain()
        kind, _ = await receive(reader)
        assert kind == U.MSG_BYE
        return kind
    finally:
        writer.close()
        await writer.wait_closed()

"""Loopback protocol-v2 receiver for the Swift integration test. Not for production.

usage: test_receiver.py DEST SECRET_HEX PORT_FILE
Writes the bound port to PORT_FILE, then serves until killed.
"""
import asyncio
import os
import sys
from pathlib import Path

from omi_local.file_store import FileStore
from omi_local.server import UploadServer


async def main(dest: Path, secret_hex: str, port_file: Path) -> None:
    server = UploadServer(bytes.fromhex(secret_hex), dest, host="127.0.0.1", port=0,
                          file_store=FileStore(dest / "meetings"))
    await server.start()
    tmp = port_file.with_name(port_file.name + ".tmp")
    tmp.write_text(str(server.bound_port))
    os.replace(tmp, port_file)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])))

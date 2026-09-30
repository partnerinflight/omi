"""BLE SMP flasher for the Omi CV1 (Windows counterpart of ~/omi-firmware/flash_omi.py on the Mac).
See omi/firmware/WINDOWS_BUILD.md. Needs: pip install "smpclient[ble]" imgtool

  python flash_omi.py reset

  python flash_omi.py status
  python flash_omi.py stage   <dfu_application.zip> --key <root-rsa-2048.pem> [--net-digest-installed HEX]
  python flash_omi.py confirm <app digest hex>

stage:   verifies every image's MCUboot signature, uploads the application image to its
         secondary slot, checks the digest the device reports, marks it for a TEST boot and
         resets. If the new image fails to boot, MCUboot reverts to the previous one.
         The network-core image has no fallback: it is only uploaded if its digest differs
         from --net-digest-installed, and then only with --allow-net.
confirm: after checking that the new image runs, makes it permanent.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import zipfile
from pathlib import Path

from bleak import BleakScanner
from imgtool import image as imgtool_image, keys as imgtool_keys
from smpclient import SMPClient
from smpclient.generics import error, success
from smpclient.requests.image_management import ImageStatesRead, ImageStatesWrite
from smpclient.requests.os_management import ResetWrite
from smpclient.transport.ble import SMPBLETransport

def omi_service_uuid() -> str:
    # The same service filter omi-local uses (omi/firmware/scripts/omi-local).
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "omi-local"))
    from omi_local import protocol

    return protocol.LOCAL_STORAGE_SERVICE_UUID


async def find_omi(timeout: float = 20.0) -> str:
    uuid = omi_service_uuid()
    dev = await BleakScanner.find_device_by_filter(
        lambda d, adv: uuid in [u.lower() for u in (adv.service_uuids or [])], timeout=timeout
    )
    if dev is None:
        raise SystemExit("Omi not found over BLE (powered on, in range, not connected elsewhere?)")
    return dev.address


def read_zip(path: Path, key_path: Path):
    key = imgtool_keys.load(str(key_path))
    images = []
    with zipfile.ZipFile(path) as z, tempfile.TemporaryDirectory() as tmp:
        manifest = json.loads(z.read("manifest.json"))
        for entry in manifest["files"]:
            data = z.read(entry["file"])
            staged = Path(tmp) / entry["file"]
            staged.write_bytes(data)
            result, version, digest, _ = imgtool_image.Image.verify(str(staged), key)
            if result != imgtool_image.VerifyResult.OK or digest is None:
                raise SystemExit(f"{entry['file']}: signature verification failed ({result})")
            images.append(dict(index=int(entry["image_index"]), file=entry["file"], data=data,
                               digest=bytes(digest), version=version))
    return sorted(images, key=lambda i: i["index"])


async def states(client: SMPClient):
    r = await client.request(ImageStatesRead())
    if not success(r):
        raise SystemExit(f"image state read failed: {r}")
    for s in r.images:
        flags = [n for n in ("active", "confirmed", "pending", "permanent", "bootable") if getattr(s, n)]
        print(f"  image {s.image or 0} slot {s.slot}: {s.version}  {bytes(s.hash).hex() if s.hash else '-'}  {' '.join(flags)}")
    return r.images


async def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("reset")
    st = sub.add_parser("stage")
    st.add_argument("zip", type=Path)
    st.add_argument("--key", type=Path, required=True)
    st.add_argument("--net-digest-installed", default="")
    st.add_argument("--allow-net", action="store_true")
    cf = sub.add_parser("confirm")
    cf.add_argument("digest")
    args = ap.parse_args()

    images = []
    if args.cmd == "stage":
        images = read_zip(args.zip, args.key)
        for i in images:
            print(f"verified image {i['index']} {i['file']}: {i['version']}  digest {i['digest'].hex()}")

    address = await find_omi()
    print(f"Omi at {address}")
    async with SMPClient(SMPBLETransport(), address, timeout_s=10.0) as client:
        print("before:")
        await states(client)

        if args.cmd == "status":
            return
        if args.cmd == "reset":
            r = await client.request(ResetWrite())
            print("reset requested" if success(r) else f"reset failed: {r}")
            return
        if args.cmd == "confirm":
            r = await client.request(ImageStatesWrite(hash=bytes.fromhex(args.digest), confirm=True))
            if not success(r):
                raise SystemExit(f"confirm failed: {r}")
            print("after:")
            await states(client)
            return

        for img in images:
            if img["index"] == 1:
                if img["digest"].hex() == args.net_digest_installed.lower():
                    print("network core image unchanged from the installed one: skipping it")
                    continue
                if not args.allow_net:
                    print("network core image differs from the installed one and has no fallback: "
                          "skipping it (pass --allow-net to update it deliberately)")
                    continue
            print(f"uploading image {img['index']} ({len(img['data'])} bytes) ...")
            last = -1
            async for offset in client.upload(img["data"], slot=img["index"], upgrade=False):
                pct = offset * 100 // len(img["data"])
                if pct // 10 != last // 10:
                    print(f"  {pct}%", flush=True)
                last = pct
            found = await states(client)
            if not any(s.hash and bytes(s.hash) == img["digest"] for s in found):
                raise SystemExit("device does not report the uploaded digest; not activating")
            r = await client.request(ImageStatesWrite(hash=img["digest"], confirm=False))
            if not success(r):
                raise SystemExit(f"marking test boot failed: {r}")
            print(f"image {img['index']} staged for a test boot")

        print("after staging:")
        await states(client)
        r = await client.request(ResetWrite())
        print("reset requested" if success(r) else f"reset failed: {r}")


if __name__ == "__main__":
    asyncio.run(main())

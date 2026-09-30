"""Verify every image in an Omi OTA zip against the repo's MCUboot key; print digests."""
import json, sys, tempfile, zipfile
from pathlib import Path
from imgtool import image, keys

if len(sys.argv) != 2:
    raise SystemExit("usage: verify_zip.py <dfu_application.zip>")
zip_path = sys.argv[1]
key = keys.load(str(Path(__file__).resolve().parents[2] / "bootloader" / "mcuboot" / "root-rsa-2048.pem"))
with zipfile.ZipFile(zip_path) as z, tempfile.TemporaryDirectory() as t:
    manifest = json.loads(z.read("manifest.json"))
    for f in manifest["files"]:
        p = Path(t) / f["file"]
        p.write_bytes(z.read(f["file"]))
        result, version, digest, _ = image.Image.verify(str(p), key)
        print(f"image {f['image_index']} {f['file']}: {result.name} version {version} digest {digest.hex() if digest else None}")

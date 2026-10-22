"""Merge the service configuration on install and reinstall.

Three layers: defaults (only when a key is missing) < the existing file < installer-managed keys
(paths and ports the installer owns). Anything else the owner or a later feature added survives a
reinstall. The merged result is validated with the service's own Config.load before it replaces
the file, so an invalid merge never reaches the service."""

from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

from .config import Config
from .io import temp_beside


def merge_service_config(existing, managed, defaults):
    return {**defaults, **(existing or {}), **managed}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target", required=True, type=Path, help="service.json to create or update")
    parser.add_argument("--installer", required=True, type=Path,
                        help='JSON file: {"managed": {...}, "defaults": {...}}')
    args = parser.parse_args(argv)
    installer = json.loads(args.installer.read_text(encoding="utf-8-sig"))
    existing = None
    if args.target.exists():
        try:
            existing = json.loads(args.target.read_text(encoding="utf-8-sig"))
        except ValueError as error:
            print(f"Existing {args.target} is not valid JSON ({error}); fix or remove it first.", file=sys.stderr)
            return 2
    merged = merge_service_config(existing, installer["managed"], installer.get("defaults", {}))
    args.target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = temp_beside(args.target)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(merged, out, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        try:
            Config.load(Path(temporary))
        except (ValueError, KeyError, TypeError) as error:
            print(f"Merged configuration is invalid: {error}", file=sys.stderr)
            return 1
        os.replace(temporary, args.target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(json.dumps({"ok": True, "kept": sorted(set(existing or {}) - set(installer["managed"]))}))
    return 0


if __name__ == "__main__":
    sys.exit(main())

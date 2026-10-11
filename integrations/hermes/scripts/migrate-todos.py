"""Milestone 5 todo migration, run on the Hermes host against the installed plugin.

Dry run (default) prints the inventory and touches nothing. --apply imports into the server
using the Windows bridge's producer token; repeating it is safe. See the plugin README."""
import argparse
import importlib.util
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--server", help="https://brain.example.com")
    parser.add_argument("--token-file", help="file holding a producer token (sb_server add-producer)")
    parser.add_argument("--migration-id", help="stable name for this migration, e.g. todos-2026-10")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "plugins" / "secondbrain-checklist"
    spec = importlib.util.spec_from_file_location("sbr_migrate", root / "__init__.py", submodule_search_locations=[str(root)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)
    from sbr_migrate import migrate
    if not args.apply:
        found = migrate.inventory(args.vault)
        print(json.dumps({"dry_run": True, "source_hash": found["source_hash"], **found["report"]}, indent=2))
        return
    if not (args.server and args.token_file and args.migration_id):
        parser.error("--apply needs --server, --token-file and --migration-id")
    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    print(json.dumps(migrate.apply(args.vault, args.server, token, args.migration_id), indent=2))


if __name__ == "__main__":
    main()

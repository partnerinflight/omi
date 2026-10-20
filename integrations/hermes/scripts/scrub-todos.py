"""Run against the installed plugin, using the same lock as Telegram callbacks."""
import argparse
import importlib.util
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vault", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "plugins" / "secondbrain-checklist"
    spec = importlib.util.spec_from_file_location("sbr_maintenance", root / "__init__.py", submodule_search_locations=[str(root)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = package
    spec.loader.exec_module(package)
    from sbr_maintenance.maintenance import scrub
    print(json.dumps(scrub(args.vault)))


if __name__ == "__main__":
    main()

from __future__ import annotations
import argparse
import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import signal
import sys
import threading
from pathlib import Path
from .config import Config
from .queue import Queue


def main():
    parser = argparse.ArgumentParser(description="Second Brain local service worker")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ["run", "status", "retry", "check"]:
        p = sub.add_parser(name)
        p.add_argument("--config", type=Path, required=True)
        if name == "run":
            p.add_argument("--stdin-control", action="store_true")
    args = parser.parse_args()
    cfg = Config.load(args.config)
    if args.command == "status":
        print(cfg.status_file.read_text(encoding="utf-8"))
        return
    if args.command == "retry":
        print(f"Requeued {Queue(cfg.data_dir / 'queue.sqlite3').retry_failed()} failed jobs")
        from .router import RouterQueue

        print(f"Requeued {RouterQueue(cfg.data_dir / 'router.sqlite3').retry_failed()} failed routing items")
        return
    if args.command == "check":
        from .preflight import check

        result = check(cfg)
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["ok"] else 1)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(cfg.data_dir / "service.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from .runtime import Runtime

    async def run():
        runtime = Runtime(cfg)
        loop = asyncio.get_running_loop()

        def stop(*_):
            loop.call_soon_threadsafe(runtime.stop.set)

        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, stop)
        if args.stdin_control:

            def read_control():
                for line in sys.stdin:
                    if line.strip() == "stop":
                        stop()
                        return
                stop()  # supervisor disappeared

            threading.Thread(target=read_control, daemon=True).start()
        await runtime.run()

    asyncio.run(run())


if __name__ == "__main__":
    main()

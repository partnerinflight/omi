"""Admin CLI: python -m sb_server {migrate|pair NAME|add-producer NAME|revoke-producer NAME|
                                   add-integration NAME|authority [cutover|rollback]|new-epoch|serve}."""
import argparse
import os
import secrets
import uuid

from . import auth
from .db import connect, migrate


def main():
    parser = argparse.ArgumentParser(prog="sb_server")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    pair = sub.add_parser("pair", help="print a single-use pairing code for a new device")
    pair.add_argument("device_name")
    producer = sub.add_parser("add-producer", help="print a token for a Windows worker (shown once)")
    producer.add_argument("name")
    revoke = sub.add_parser("revoke-producer")
    revoke.add_argument("name")
    integration = sub.add_parser("add-integration", help="print a long-lived device token for Hermes (shown once)")
    integration.add_argument("name")
    authority = sub.add_parser("authority", help="show or switch who owns imported todos")
    authority.add_argument("change", nargs="?", choices=["cutover", "rollback"])
    sub.add_parser("new-epoch", help="run after restoring a backup: devices must bootstrap again")
    sub.add_parser("serve")
    args = parser.parse_args()

    if args.command == "migrate":
        print("applied:", ", ".join(migrate()) or "nothing")
    elif args.command == "pair":
        code = auth.new_pairing_code()
        with connect() as conn:
            conn.execute("INSERT INTO pairing_codes (code_hash, device_name, expires_at) VALUES (%s, %s, now() + %s)",
                         (auth.digest(code), args.device_name, auth.PAIRING_TTL))
        print(f"{code}  (valid {int(auth.PAIRING_TTL.total_seconds() // 60)} minutes, single use)")
    elif args.command == "add-producer":
        token = secrets.token_urlsafe(32)
        with connect() as conn:
            conn.execute("INSERT INTO producers (id, name, token_hash) VALUES (%s, %s, %s)",
                         (uuid.uuid4(), args.name, auth.digest(token)))
        print(token)
    elif args.command == "revoke-producer":
        with connect() as conn:
            n = conn.execute("UPDATE producers SET revoked_at = now() WHERE name = %s AND revoked_at IS NULL",
                             (args.name,)).rowcount
        print("revoked:", n)
    elif args.command == "add-integration":
        # ponytail: a 10-year access token with no refresh, so the Hermes gateway and its cron
        # subprocesses never race on rotation. Revoke the device to cut it off.
        token, device_id = secrets.token_urlsafe(32), uuid.uuid4()
        with connect() as conn, conn.transaction():
            conn.execute("INSERT INTO devices (id, name) VALUES (%s, %s)", (device_id, args.name))
            conn.execute("INSERT INTO tokens (hash, device_id, kind, expires_at) VALUES (%s, %s, 'access', now() + interval '10 years')",
                         (auth.digest(token), device_id))
        print(token)
    elif args.command == "authority":
        with connect() as conn, conn.transaction():
            row = conn.execute("SELECT * FROM authority WHERE domain = 'todos' FOR UPDATE").fetchone()
            if args.change == "cutover":
                if not row["migration_id"]:
                    raise SystemExit("No todo import recorded; run the migration first")
                row = conn.execute("UPDATE authority SET mode = 'server', switched_at = now() WHERE domain = 'todos' RETURNING *").fetchone()
            elif args.change == "rollback":
                row = conn.execute("UPDATE authority SET mode = 'legacy', switched_at = now() WHERE domain = 'todos' RETURNING *").fetchone()
        print(f"todos: {row['mode']} (migration {row['migration_id'] or 'none'}, switched {row['switched_at'] or 'never'})")
    elif args.command == "new-epoch":
        with connect() as conn:
            print("epoch:", conn.execute("UPDATE meta SET epoch = gen_random_uuid() RETURNING epoch").fetchone()["epoch"])
    else:
        import uvicorn

        migrate()
        # TLS terminates at the reverse proxy; trust its forwarded headers only.
        uvicorn.run("sb_server.app:app", host="0.0.0.0", port=int(os.environ.get("SB_PORT", "8000")),
                    proxy_headers=True, forwarded_allow_ips=os.environ.get("SB_FORWARDED_ALLOW_IPS", "127.0.0.1"))


main()

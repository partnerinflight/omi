import os
from importlib.resources import files

import psycopg
from psycopg.rows import dict_row


def connect() -> psycopg.Connection:
    # Autocommit: each `with conn.transaction()` block is a real, independent commit.
    # ponytail: one connection per request; add psycopg_pool if request rate ever matters.
    return psycopg.connect(os.environ["SB_DATABASE_URL"], row_factory=dict_row, autocommit=True)


def migrate() -> list[str]:
    """Apply pending migrations/NNN_*.sql in order, each in its own transaction."""
    applied = []
    with connect() as conn:
        conn.execute("SELECT pg_advisory_lock(7331001)")  # concurrent starts migrate once
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())")
        done = {r["name"] for r in conn.execute("SELECT name FROM schema_migrations")}
        for f in sorted(files("sb_server").joinpath("migrations").iterdir(), key=lambda f: f.name):
            if f.name.endswith(".sql") and f.name not in done:
                with conn.transaction():
                    conn.execute(f.read_text())
                    conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
                applied.append(f.name)
        conn.execute("SELECT pg_advisory_unlock(7331001)")
    return applied

import os
from importlib.resources import files

import psycopg
from psycopg.rows import dict_row


from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def connect() -> psycopg.Connection:
    """A direct connection, for the admin CLI and migrations. Requests use pool()."""
    # Autocommit: each `with conn.transaction()` block is a real, independent commit.
    return psycopg.connect(os.environ["SB_DATABASE_URL"], row_factory=dict_row, autocommit=True)


def pool() -> ConnectionPool:
    """At most SB_DB_POOL (5) connections to the shared postgres-db, whatever the request rate;
    a request that waits SB_DB_TIMEOUT (5 s) for one is refused rather than queued forever."""
    global _pool
    if _pool is None:
        _pool = ConnectionPool(os.environ["SB_DATABASE_URL"], min_size=1, max_size=int(os.environ.get("SB_DB_POOL", "5")),
                               timeout=float(os.environ.get("SB_DB_TIMEOUT", "5")), check=ConnectionPool.check_connection,
                               kwargs={"row_factory": dict_row, "autocommit": True}, open=True)
    return _pool


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

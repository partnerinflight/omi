"""Opaque device tokens. Only SHA-256 hashes reach the database."""
import hashlib
import secrets
import threading
import time
import uuid
from datetime import timedelta

ACCESS_TTL = timedelta(minutes=15)
REFRESH_TTL = timedelta(days=90)  # matches the proposed 90-day offline window
PAIRING_TTL = timedelta(minutes=10)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"  # no 0/O, 1/I/L, U


def new_pairing_code() -> str:
    # ~49 bits, single use, 10 minutes, and failed attempts are rate limited.
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))


def create_pairing_code(conn, device_name: str) -> str:
    code = new_pairing_code()
    conn.execute("INSERT INTO pairing_codes (code_hash, device_name, expires_at) VALUES (%s, %s, now() + %s)",
                 (digest(code), device_name, PAIRING_TTL))
    return code


def issue_tokens(conn, device_id: uuid.UUID) -> dict:
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO tokens (hash, device_id, kind, expires_at) VALUES (%s, %s, 'access', now() + %s), (%s, %s, 'refresh', now() + %s)",
        (digest(access), device_id, ACCESS_TTL, digest(refresh), device_id, REFRESH_TTL),
    )
    return {"device_id": str(device_id), "access_token": access, "refresh_token": refresh,
            "expires_in": int(ACCESS_TTL.total_seconds())}


class FailureLimiter:
    """Failed attempts per client key (IP) in a sliding window, plus a global ceiling.

    An attempt reserves a slot before it is checked and releases it on success, so
    concurrent guesses cannot slip past the limit. ponytail: in-process, so it resets on
    restart and assumes one server process (the container runs one)."""

    def __init__(self, per_key=10, window=600, total=100, total_window=3600):
        self.per_key, self.window, self.total, self.total_window = per_key, window, total, total_window
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        with self.lock:
            self.keys: dict[str, list[float]] = {}
            self.all: list[float] = []

    def reserve(self, key: str) -> float | None:
        """A slot (pass it to release on success), or None when the caller must wait."""
        now = time.monotonic()
        with self.lock:
            mine = [t for t in self.keys.get(key, []) if t > now - self.window]
            self.all = [t for t in self.all if t > now - self.total_window]
            if len(mine) >= self.per_key or len(self.all) >= self.total:
                self.keys[key] = mine
                return None
            mine.append(now)
            self.keys[key] = mine
            self.all.append(now)
            return now

    def release(self, key: str, slot: float):
        with self.lock:
            if slot in self.keys.get(key, []):
                self.keys[key].remove(slot)
            if slot in self.all:
                self.all.remove(slot)

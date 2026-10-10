"""Opaque device tokens. Only SHA-256 hashes reach the database."""
import hashlib
import secrets
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


def issue_tokens(conn, device_id: uuid.UUID) -> dict:
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO tokens (hash, device_id, kind, expires_at) VALUES (%s, %s, 'access', now() + %s), (%s, %s, 'refresh', now() + %s)",
        (digest(access), device_id, ACCESS_TTL, digest(refresh), device_id, REFRESH_TTL),
    )
    return {"device_id": str(device_id), "access_token": access, "refresh_token": refresh,
            "expires_in": int(ACCESS_TTL.total_seconds())}


class FailureLimiter:
    """Global sliding window over failed pairing attempts.

    ponytail: in-process, so it resets on restart and assumes one server process.
    """

    def __init__(self, limit=10, window=600):
        self.limit, self.window, self.failures = limit, window, []

    def blocked(self) -> bool:
        cutoff = time.monotonic() - self.window
        self.failures = [t for t in self.failures if t > cutoff]
        return len(self.failures) >= self.limit

    def fail(self):
        self.failures.append(time.monotonic())

"""Request body caps, enforced before routing, authentication or JSON parsing.

FastAPI reads and parses a whole body before it resolves the auth dependency, so without
this an anonymous caller could make the process buffer gigabytes. Traefik caps bodies at
8 MB in front (deploy/selfhost); these per-route caps are the app's own floor."""
import re

CAPS = [  # (method, path, max body bytes); first match wins
    ("PUT", re.compile(r"/v1/worker/clips/[^/]+/\d+"), 2_100_000),  # one <=30 s PCM16 clip (~960 KB) + slack
    ("POST", re.compile(r"/v1/worker/events"), 7_000_000),  # 100 records x 64 KB
    ("POST", re.compile(r"/v1/migration/todos"), 4_000_000),
    ("POST", re.compile(r"/v1/operations"), 1_100_000),  # 100 operations x 10 KB payloads
]
DEFAULT_CAP = 256_000

_BODY = b'{"detail":"body_too_large"}'
_START = {"type": "http.response.start", "status": 413,
          "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(_BODY)).encode())]}


def cap_for(method: str, path: str) -> int:
    for m, pattern, cap in CAPS:
        if m == method and pattern.fullmatch(path):
            return cap
    return DEFAULT_CAP


class _TooLarge(Exception):
    pass


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        cap = cap_for(scope["method"], scope["path"])
        length = dict(scope["headers"]).get(b"content-length")
        if length is not None and (not length.isdigit() or int(length) > cap):
            await send(_START)
            return await send({"type": "http.response.body", "body": _BODY})

        seen, exceeded, started = 0, False, False

        async def limited_receive():
            nonlocal seen, exceeded
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > cap:
                    exceeded = True
                    raise _TooLarge  # stop reading; the app's error response is replaced below
            return message

        async def guarded_send(message):
            nonlocal started
            if not exceeded:
                started = started or message["type"] == "http.response.start"
                return await send(message)
            if message["type"] == "http.response.start" and not started:
                started = True
                await send(_START)
            elif message["type"] == "http.response.body" and not message.get("more_body"):
                await send({"type": "http.response.body", "body": _BODY})

        try:
            await self.app(scope, limited_receive, guarded_send)
        except _TooLarge:
            if not started:
                await send(_START)
                await send({"type": "http.response.body", "body": _BODY})

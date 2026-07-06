"""ASGI wrapper for the PUBLIC DEMO deployment of flowers.

flowers itself is a single-user, localhost, no-auth surface — this wrapper is
what makes one throwaway instance of it safe to reach over a private network
from a demo launcher:

* **Token gate** — every request (except /health) must carry the per-session
  secret minted by the launcher, in ``X-Flowers-Demo-Token`` or
  ``Authorization: Bearer …``. Constant-time compare; fails CLOSED when no
  token is configured.
* **Body cap** — oversized requests are rejected before they reach the app.
* **Self-destruct watchdog** — the process exits when the session goes idle
  or exceeds its max age. The demo machine is created with
  ``auto_destroy: true``, so a wrapper exit destroys the machine even if the
  launcher that created it is gone. One instance == one visitor's session;
  state is meant to die with it.

Run: ``uvicorn deploy.demo.wrapper:app`` (or ``wrapper:app`` in the demo
image) with ``FLOWERS_DEMO_TOKEN`` set. The wrapped app is the ordinary
``flowers.app:app``.
"""

from __future__ import annotations

import hmac
import os
import threading
import time

from flowers.app import app as _flowers_app

_TOKEN = os.environ.get("FLOWERS_DEMO_TOKEN", "")
_MAX_BODY_BYTES = int(os.environ.get("FLOWERS_DEMO_MAX_BODY_BYTES", "16384"))
_IDLE_TTL_S = float(os.environ.get("FLOWERS_DEMO_IDLE_TTL_SECONDS", "900"))
_MAX_AGE_S = float(os.environ.get("FLOWERS_DEMO_MAX_AGE_SECONDS", "2700"))

_START = time.monotonic()
# Single-slot last-activity timestamp; written on every authorized request.
_LAST_ACTIVITY = [time.monotonic()]


def _watchdog() -> None:
    while True:
        time.sleep(30)
        now = time.monotonic()
        idle = now - _LAST_ACTIVITY[0]
        age = now - _START
        if idle > _IDLE_TTL_S or age > _MAX_AGE_S:
            # A hard exit is the point: auto_destroy tears the machine down.
            os._exit(0)


threading.Thread(target=_watchdog, daemon=True, name="demo-watchdog").start()


async def _plain(send, status: int, message: str) -> None:
    body = message.encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    })
    await send({"type": "http.response.body", "body": body})


class DemoGate:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if scope.get("path") == "/health":
            return await self.app(scope, receive, send)

        headers: dict[str, str] = {}
        for k, v in scope.get("headers", []):
            headers[k.decode("latin-1").lower()] = v.decode("latin-1")

        length = headers.get("content-length", "")
        if length.isdigit() and int(length) > _MAX_BODY_BYTES:
            return await _plain(send, 413, "request body too large")

        provided = headers.get("x-flowers-demo-token", "")
        if not provided:
            auth = headers.get("authorization", "")
            if auth.startswith("Bearer "):
                provided = auth[len("Bearer "):]
        # Fails closed: an empty configured token never matches.
        if not _TOKEN or not hmac.compare_digest(
            provided.encode("utf-8"), _TOKEN.encode("utf-8")
        ):
            return await _plain(send, 401, "missing or invalid demo token")

        _LAST_ACTIVITY[0] = time.monotonic()
        return await self.app(scope, receive, send)


app = DemoGate(_flowers_app)

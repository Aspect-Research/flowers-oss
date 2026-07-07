"""Demo launcher: one throwaway flowers Machine per visitor session.

The public face of the flowers demo. A visitor POSTs their OpenRouter key to
/session; the launcher creates a Fly Machine from the demo worker image with
that key in its env (the key passes through launcher memory only — never
logged, never stored), waits for it to come up, and returns a session id +
token. All further traffic is proxied: /s/{session_id}/... -> the worker's
private 6PN address, gated by the per-session token.

Sessions end three ways, any of which destroys the machine (auto_destroy):
the visitor's DELETE, the worker's own idle/max-age watchdog exiting, or this
launcher's reaper. The reaper also destroys orphaned workers (e.g. after a
launcher restart — the session map is in-memory by design).

Env:
  FLY_API_TOKEN        deploy token scoped to the worker app (secret)
  FLY_WORKER_APP       app the workers run in       (default flowers-demo-workers)
  FLY_WORKER_IMAGE     image ref for workers        (registry.fly.io/<app>:worker)
  FLY_WORKER_REGION    region for workers           (default iad)
  DEMO_ALLOWED_ORIGINS CSV of browser origins for CORS
  DEMO_MAX_SESSIONS    concurrent machine cap       (default 6)
  DEMO_LAUNCHES_PER_HOUR_PER_IP                     (default 3)
  DEMO_FREE_MODEL      OpenRouter slug for free mode
  OPERATOR_OPENROUTER_KEY  operator-funded key for keyless free mode (secret);
                       when free_models is requested with no visitor key, the
                       worker runs on this key. Set via `flyctl secrets set`.
  DEMO_OPERATOR_SESSIONS_PER_IP  max concurrent operator-funded sessions per IP
                       (default 1) — throttles free-tier quota drain per visitor.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import secrets
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field

from contextlib import asynccontextmanager

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

log = logging.getLogger("flowers.demo.launcher")
logging.basicConfig(level=logging.INFO)

FLY_API = "https://api.machines.dev/v1"
FLY_API_TOKEN = os.environ.get("FLY_API_TOKEN", "")
WORKER_APP = os.environ.get("FLY_WORKER_APP", "flowers-demo-workers")
WORKER_IMAGE = os.environ.get(
    "FLY_WORKER_IMAGE", f"registry.fly.io/{WORKER_APP}:worker"
)
WORKER_REGION = os.environ.get("FLY_WORKER_REGION", "iad")
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.environ.get(
        "DEMO_ALLOWED_ORIGINS",
        "https://aspectresearch.org,http://localhost:3000",
    ).split(",")
    if o.strip()
]
MAX_SESSIONS = int(os.environ.get("DEMO_MAX_SESSIONS", "6"))
LAUNCHES_PER_HOUR_PER_IP = int(os.environ.get("DEMO_LAUNCHES_PER_HOUR_PER_IP", "3"))
# Operator-funded free sessions (free_models + no visitor key) spend the
# operator's OpenRouter free-tier quota, so they get a tighter concurrency cap
# per IP on top of the shared hourly launch limit — one visitor can't hold open
# several operator-funded workers at once and drain the shared quota.
OPERATOR_SESSIONS_PER_IP = int(os.environ.get("DEMO_OPERATOR_SESSIONS_PER_IP", "1"))
OPERATOR_KEY = os.environ.get("OPERATOR_OPENROUTER_KEY", "")
# Free slugs churn AND get throttled on OpenRouter — verify against
# /api/v1/models when this misbehaves. Chosen for availability: qwen/gpt-oss/
# llama free slugs are frequently 429'd upstream; nemotron-3-super (120B, tool
# calling) has been the reliably reachable free model.
FREE_MODEL = os.environ.get("DEMO_FREE_MODEL", "nvidia/nemotron-3-super-120b-a12b:free")
# The worker's own watchdog (idle 15 min / age 45 min) is the primary lifetime
# bound; the reaper's orphan cutoff sits safely above it.
ORPHAN_MAX_AGE_S = 50 * 60

_KEY_RE = re.compile(r"^[\x21-\x7e]{10,256}$")  # printable ASCII, no spaces


@dataclass
class Session:
    session_id: str
    token: str
    machine_id: str
    private_ip: str
    created: float = field(default_factory=time.monotonic)
    # Client IP + operator-funded flag so we can release the per-IP operator
    # concurrency slot when this session ends (delete / expiry / reap).
    client_ip: str = ""
    operator_funded: bool = False


SESSIONS: dict[str, Session] = {}
_launches: dict[str, deque] = defaultdict(deque)
# Live count of operator-funded sessions per IP (concurrency cap, in-memory like
# _launches). Incremented when an operator-funded session is reserved, decremented
# when it ends.
_operator_active: dict[str, int] = defaultdict(int)

# One client for the Fly Machines API, one for proxying to workers (no read
# timeout: SSE streams stay open for the length of a run).
_fly = httpx.AsyncClient(
    base_url=FLY_API,
    headers={"Authorization": f"Bearer {FLY_API_TOKEN}"},
    timeout=httpx.Timeout(30.0),
)
_proxy = httpx.AsyncClient(
    timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
)


def _client_ip(request: Request) -> str:
    return request.headers.get("fly-client-ip") or (
        request.client.host if request.client else "unknown"
    )


def _launch_allowed(ip: str) -> bool:
    now = time.monotonic()
    q = _launches[ip]
    while q and q[0] <= now - 3600:
        q.popleft()
    if len(q) >= LAUNCHES_PER_HOUR_PER_IP:
        return False
    q.append(now)
    return True


def _operator_slot_reserve(ip: str) -> bool:
    """Try to reserve an operator-funded concurrency slot for this IP. Returns
    False (caller should 429) when the IP is already at its cap."""
    if _operator_active[ip] >= OPERATOR_SESSIONS_PER_IP:
        return False
    _operator_active[ip] += 1
    return True


def _operator_slot_release(session: Session) -> None:
    """Give back a reserved operator-funded slot when the session ends."""
    if not session.operator_funded:
        return
    n = _operator_active.get(session.client_ip, 0) - 1
    if n > 0:
        _operator_active[session.client_ip] = n
    else:
        _operator_active.pop(session.client_ip, None)


def _worker_base(session: Session) -> str:
    return f"http://[{session.private_ip}]:8000"


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "sessions": len(SESSIONS)})


async def create_session(request: Request) -> JSONResponse:
    if not FLY_API_TOKEN:
        return JSONResponse({"error": "launcher not configured"}, status_code=503)
    ip = _client_ip(request)
    if not _launch_allowed(ip):
        return JSONResponse(
            {"error": "too many demo sessions from your address — try again later"},
            status_code=429,
        )
    if len(SESSIONS) >= MAX_SESSIONS:
        return JSONResponse(
            {"error": "demo at capacity — try again in a few minutes"},
            status_code=503,
        )
    try:
        body = json.loads(await request.body())
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    key = (body.get("openrouter_key") or "").strip()
    free_models = bool(body.get("free_models"))

    # Key resolution:
    #   free_models + non-empty key  -> validate & use the visitor's key
    #   free_models + no key         -> operator-funded (uses OPERATOR_OPENROUTER_KEY)
    #   not free_models              -> visitor key REQUIRED and validated
    operator_funded = False
    if free_models and not key:
        # Keyless free mode: run the worker on the operator's key.
        if not OPERATOR_KEY:
            return JSONResponse(
                {"error": "free mode is not configured"}, status_code=503
            )
        worker_key = OPERATOR_KEY
        operator_funded = True
    else:
        # A key was supplied (free or paid) — always validate it.
        if not _KEY_RE.match(key):
            return JSONResponse(
                {"error": "openrouter_key looks malformed"}, status_code=400
            )
        worker_key = key

    # Operator-funded sessions spend our shared free-tier quota, so cap how many
    # a single IP can hold open at once (reserved now, released when it ends).
    if operator_funded and not _operator_slot_reserve(ip):
        return JSONResponse(
            {"error": "too many free demo sessions from your address — try again later"},
            status_code=429,
        )

    token = secrets.token_urlsafe(24)
    env = {
        "OPENROUTER_API_KEY": worker_key,
        "FLOWERS_DEMO_TOKEN": token,
    }
    if free_models:
        env["FLOWERS_ROLE_CONFIG_JSON"] = json.dumps(
            # Role resolution falls back to "executor", so this single entry
            # reroutes every role to the free model. NOTE: omit "reasoning" —
            # the free model doesn't support the reasoning param, and model.py
            # only sends it when the role config carries a truthy "reasoning",
            # so leaving it out keeps OpenRouter from rejecting the call.
            {"executor": {"model": FREE_MODEL}}
        )

    machine_config = {
        "region": WORKER_REGION,
        "config": {
            "image": WORKER_IMAGE,
            "env": env,
            "guest": {"cpu_kind": "shared", "cpus": 1, "memory_mb": 512},
            "auto_destroy": True,
            "restart": {"policy": "no"},
        },
    }
    r = await _fly.post(f"/apps/{WORKER_APP}/machines", json=machine_config)
    if r.status_code >= 300:
        log.error("machine create failed: %s %s", r.status_code, r.text[:500])
        if operator_funded:
            _operator_slot_release(
                Session("", "", "", "", client_ip=ip, operator_funded=True)
            )
        return JSONResponse(
            {"error": "could not start a demo machine"}, status_code=502
        )
    machine = r.json()
    machine_id = machine["id"]
    private_ip = machine.get("private_ip", "")

    session = Session(
        session_id=uuid.uuid4().hex,
        token=token,
        machine_id=machine_id,
        private_ip=private_ip,
        client_ip=ip,
        operator_funded=operator_funded,
    )

    # Wait for the machine, then for the app inside it.
    try:
        await _fly.get(
            f"/apps/{WORKER_APP}/machines/{machine_id}/wait",
            params={"state": "started", "timeout": 60},
        )
        base = _worker_base(session)
        for _ in range(40):
            try:
                h = await _proxy.get(f"{base}/health", timeout=3.0)
                if h.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
        else:
            raise RuntimeError("worker never became healthy")
    except Exception:
        log.exception("worker %s failed to come up — destroying", machine_id)
        await _destroy_machine(machine_id)
        _operator_slot_release(session)
        return JSONResponse(
            {"error": "demo machine failed to start"}, status_code=502
        )

    SESSIONS[session.session_id] = session
    log.info(
        "session %s -> machine %s (free_models=%s operator_funded=%s)",
        session.session_id, machine_id, free_models, operator_funded,
    )
    return JSONResponse({"session_id": session.session_id, "token": session.token})


def _authorized(request: Request, session: Session) -> bool:
    provided = request.headers.get("x-flowers-session-token", "")
    return hmac.compare_digest(provided.encode(), session.token.encode())


async def delete_session(request: Request) -> JSONResponse:
    sid = request.path_params["sid"]
    session = SESSIONS.get(sid)
    if session is None or not _authorized(request, session):
        return JSONResponse({"error": "unknown session"}, status_code=404)
    SESSIONS.pop(sid, None)
    _operator_slot_release(session)
    await _destroy_machine(session.machine_id)
    return JSONResponse({"destroyed": True})


_HOP_HEADERS = {
    "connection", "keep-alive", "transfer-encoding", "upgrade",
    "proxy-authenticate", "proxy-authorization", "te", "trailers",
    "content-length", "content-encoding",
}


async def proxy(request: Request) -> Response:
    sid = request.path_params["sid"]
    path = request.path_params["path"]
    session = SESSIONS.get(sid)
    if session is None or not _authorized(request, session):
        return JSONResponse({"error": "unknown session"}, status_code=404)

    url = f"{_worker_base(session)}/{path}"
    if request.url.query:
        url += f"?{request.url.query}"
    headers = {
        "X-Flowers-Demo-Token": session.token,
        "Accept": request.headers.get("accept", "*/*"),
        "Content-Type": request.headers.get("content-type", "application/json"),
    }
    if request.headers.get("last-event-id"):
        headers["Last-Event-ID"] = request.headers["last-event-id"]

    upstream = _proxy.build_request(
        request.method, url, headers=headers, content=await request.body()
    )
    try:
        resp = await _proxy.send(upstream, stream=True)
    except httpx.HTTPError:
        # Worker likely self-destructed (idle/max-age watchdog).
        if (gone := SESSIONS.pop(sid, None)) is not None:
            _operator_slot_release(gone)
        return JSONResponse({"error": "session expired"}, status_code=410)

    out_headers = {
        k: v for k, v in resp.headers.items() if k.lower() not in _HOP_HEADERS
    }
    out_headers["Cache-Control"] = "no-cache"
    return StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        headers=out_headers,
        background=BackgroundTask(resp.aclose),
    )


async def _destroy_machine(machine_id: str) -> None:
    try:
        await _fly.delete(
            f"/apps/{WORKER_APP}/machines/{machine_id}", params={"force": "true"}
        )
    except httpx.HTTPError:
        log.exception("machine destroy failed for %s", machine_id)


async def _reaper() -> None:
    """Backstop cleanup: drop sessions whose machine is gone, and destroy
    orphaned machines (launcher restarts lose the in-memory session map;
    workers also self-destruct via their own watchdog)."""
    while True:
        await asyncio.sleep(120)
        try:
            r = await _fly.get(f"/apps/{WORKER_APP}/machines")
            if r.status_code != 200:
                continue
            machines = {m["id"]: m for m in r.json()}
            for sid, session in list(SESSIONS.items()):
                m = machines.get(session.machine_id)
                if m is None or m.get("state") in ("destroyed", "stopped"):
                    SESSIONS.pop(sid, None)
                    _operator_slot_release(session)
            known = {s.machine_id for s in SESSIONS.values()}
            now = time.time()
            for mid, m in machines.items():
                if mid in known or m.get("state") == "destroyed":
                    continue
                created = m.get("created_at", "")
                try:
                    from datetime import datetime

                    age = now - datetime.fromisoformat(
                        created.replace("Z", "+00:00")
                    ).timestamp()
                except ValueError:
                    age = 0
                if age > ORPHAN_MAX_AGE_S:
                    log.info("reaping orphan machine %s (age %ss)", mid, int(age))
                    await _destroy_machine(mid)
        except Exception:
            log.exception("reaper iteration failed")


@asynccontextmanager
async def _lifespan(_app):
    task = asyncio.create_task(_reaper())
    try:
        yield
    finally:
        task.cancel()


app = Starlette(
    routes=[
        Route("/health", health, methods=["GET"]),
        Route("/session", create_session, methods=["POST"]),
        Route("/session/{sid}", delete_session, methods=["DELETE"]),
        Route(
            "/s/{sid}/{path:path}",
            proxy,
            methods=["GET", "POST", "PUT", "DELETE"],
        ),
    ],
    middleware=[
        Middleware(
            CORSMiddleware,
            allow_origins=ALLOWED_ORIGINS,
            allow_credentials=False,
            allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
            allow_headers=["Content-Type", "X-Flowers-Session-Token", "Last-Event-ID"],
        )
    ],
    lifespan=_lifespan,
)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))

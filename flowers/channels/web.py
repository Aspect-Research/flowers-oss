"""The web dashboard channel — SSE down / POST up (the primary surface for now).

``WebChannel`` keeps a per-run event log (so a reconnecting client can replay) and feeds live SSE
streams. ``create_app`` builds a Starlette ASGI app exposing:

  POST /api/goal        {text, budget}               -> start a run
  POST /api/answer      {run_id, text}               -> answer a clarify/approval/escalation
  GET  /api/runs/{id}                                -> run status
  GET  /api/runs/{id}/events                         -> the event log (JSON; polling fallback + tests)
  GET  /events/{id}[?replay_only=1]                  -> SSE stream (replay + live)
  GET  /                                             -> a minimal dashboard

A run is created synchronously (its id returns at once) then DRIVEN in a background worker thread, so its
plan/progress/done events stream live over SSE as they happen rather than arriving in one batch after the
run finishes. The event log is an in-memory per-process dict, guarded by a lock (the drive thread writes
while the SSE loop reads). It's a single-user local surface — bind it to localhost; there is no auth.
Requires the ``[web]`` extra (starlette).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
from importlib import resources

from flowers.channels.base import Channel

_log = logging.getLogger("flowers.serve")


class WebChannel(Channel):
    def __init__(self):
        self._logs: dict[str, list[dict]] = {}
        self._lock = threading.Lock()   # a run drives in a worker thread while the SSE loop reads the log

    def emit(self, event: dict) -> None:
        rid = event.get("run_id") or "_"
        with self._lock:
            self._logs.setdefault(rid, []).append(event)

    def log(self, run_id: str) -> list[dict]:
        with self._lock:
            return list(self._logs.get(run_id, []))


def _sse(event: dict) -> str:
    return f"event: {event.get('kind', 'message')}\ndata: {json.dumps(event)}\n\n"


def _tick_lifespan(control_plane, poll_interval: float):
    """An ASGI lifespan that runs the durable-timer DRIVER: while the app is served, a background task
    periodically calls ``control_plane.tick()`` so due timers actually fire and parked await/monitor runs
    resume (the long-running feature). Without this, ``tick`` is never called and a WAITING run sits
    forever. The blocking ``tick`` (model/store/network) runs off the event loop via ``to_thread``; a tick
    failure is logged and NEVER kills the loop; shutdown cancels cleanly. Ticks never overlap (the loop
    awaits each before sleeping)."""

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        stop = asyncio.Event()

        # Crash recovery: re-drive runs left RUNNING by a previously-crashed process (a synchronous drive
        # schedules no timer, so nothing else would wake them). We SNAPSHOT the orphan ids synchronously here,
        # BEFORE serving and before any request can drive a run — so a RUNNING run at this instant is
        # necessarily a crash orphan. The snapshot is a fast SQL read. The actual re-drive (real model/network,
        # potentially slow) runs inside the background task below, so a large or stuck recovery NEVER blocks
        # the server from starting to serve — the bug where startup hung on recovering orphaned runs.
        try:
            orphan_ids = await asyncio.to_thread(control_plane.stalled_run_ids)
        except Exception:
            orphan_ids = []
            _log.exception("flowers startup: stalled_run_ids() failed")

        async def _loop():
            # Re-drive crash orphans first (idempotency-safe: the broker blocks a duplicate send on re-drive),
            # then poll timers. A recovery failure is logged and never tears down the poller.
            if orphan_ids:
                try:
                    await asyncio.to_thread(control_plane.recover_run_ids, orphan_ids)
                except Exception:
                    _log.exception("flowers startup: crash-recovery re-drive failed")
            while not stop.is_set():
                try:
                    await asyncio.to_thread(control_plane.tick)
                except Exception:   # a single tick must never tear down the serve loop
                    _log.exception("flowers tick poller: tick() failed")
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=poll_interval)

        task = asyncio.create_task(_loop())
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return lifespan


# --------------------------------------------------------------------------- the dashboard asset

_dashboard_cache: str | None = None


def _dashboard_html() -> str | None:
    """The dashboard is a static asset shipped inside the package (``flowers/channels/static/index.html``),
    loaded via ``importlib.resources`` so it resolves identically from a wheel, an sdist, and a repo
    checkout. Lazy + cached: importing this module never touches the filesystem, and a missing asset
    surfaces as a clear 500 at request time — never an import-time crash."""
    global _dashboard_cache
    if _dashboard_cache is None:
        try:
            _dashboard_cache = (
                resources.files("flowers.channels") / "static" / "index.html"
            ).read_text(encoding="utf-8")
        except (FileNotFoundError, ModuleNotFoundError, OSError):
            return None
    return _dashboard_cache


def create_app(control_plane, channel: WebChannel, *, poll_interval: float | None = None):
    """Build the Starlette app — the local dashboard + REST API. No auth: it is a single-user local
    surface, so bind it to localhost.

    ``poll_interval`` (seconds, >0) turns on the background timer DRIVER: a served app fires due timers
    via ``control_plane.tick()`` so parked await/monitor runs resume. ``None``/0 leaves it off (the
    default — for in-request/test use). The poller only runs while the app is actually SERVING (its ASGI
    lifespan is entered), so constructing the app for a unit test does not spawn a thread."""
    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
    from starlette.routing import Route

    def _load_run(run_id):
        """Load a run, or return (None, 404) when it doesn't exist."""
        run = control_plane.store.get_run(run_id)
        if run is None:
            return None, JSONResponse({"error": "not found"}, status_code=404)
        return run, None

    _background: set = set()

    def _spawn(thunk):
        """Drive a run OFF the event loop (in a worker thread) so the request returns immediately and the
        run's plan/progress/done events stream LIVE over SSE as they happen — instead of arriving in a
        batch after a blocking synchronous run. Tracked so the task isn't GC'd; failures are logged."""
        async def _run():
            try:
                await asyncio.to_thread(thunk)
            except Exception:
                _log.exception("flowers: background run drive failed")
        task = asyncio.create_task(_run())
        _background.add(task)
        task.add_done_callback(_background.discard)

    async def post_goal(request):
        body = await request.json()
        # Create the run (fast) and return its id at once; DRIVE in the background so events stream live.
        run, goal = await asyncio.to_thread(
            control_plane.begin, goal_text=body.get("text", ""), budget_usd=float(body.get("budget", 2.0)))
        _spawn(lambda: control_plane.drive(run, goal))
        return JSONResponse({"run_id": run.run_id, "status": run.status.value})

    async def post_answer(request):
        body = await request.json()
        run, err = _load_run(body["run_id"])
        if err is not None:
            return err
        text = body.get("text", "")
        _spawn(lambda: control_plane.answer(run_id=run.run_id, answer=text))   # resume in the background
        return JSONResponse({"run_id": run.run_id, "status": "running"})

    async def get_run(request):
        run, err = _load_run(request.path_params["run_id"])
        if err is not None:
            return err
        return JSONResponse({"run_id": run.run_id, "status": run.status.value,
                             "goal": run.goal_text, "spent_usd": run.spent_usd})

    async def get_events(request):
        _run, err = _load_run(request.path_params["run_id"])
        if err is not None:
            return err
        return JSONResponse({"events": channel.log(request.path_params["run_id"])})

    async def sse(request):
        rid = request.path_params["run_id"]
        _run, err = _load_run(rid)
        if err is not None:
            return err
        replay_only = request.query_params.get("replay_only")

        async def gen():
            sent = 0
            for ev in channel.log(rid):
                yield _sse(ev)
                sent += 1
            if replay_only:
                return
            for _ in range(600):   # ~60s ceiling for a single connection
                await asyncio.sleep(0.1)
                log = channel.log(rid)
                while sent < len(log):
                    yield _sse(log[sent])
                    sent += 1
                run = control_plane.store.get_run(rid)
                if run and run.status.value in ("done", "escalated", "stopped", "failed"):
                    return

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def index(_request):
        html = _dashboard_html()
        if html is None:
            return JSONResponse(
                {"error": "dashboard asset missing — reinstall flowers: pip install 'flowers[web]'"},
                status_code=500)
        return HTMLResponse(html)

    async def _on_error(_request, _exc):
        # Error boundary: a backend failure (e.g. the store is unreachable) returns a CLEAN 500 — it never
        # crashes the worker and never leaks the exception detail (which could carry a DSN/secret).
        return JSONResponse({"error": "internal error"}, status_code=500)

    async def health(_request):
        """Liveness: the process is up + serving. Unauthenticated, no backend touch (so a brief store
        blip doesn't flap a liveness probe and trigger a needless restart)."""
        return JSONResponse({"status": "ok"})

    async def ready(_request):
        """Readiness: the STORE is reachable (a read-only probe, no write). 503 when it isn't."""
        try:
            control_plane.store.get_run("__readiness_probe__")
        except Exception:
            return JSONResponse({"status": "unready"}, status_code=503)
        return JSONResponse({"status": "ready"})

    extra = {}
    if poll_interval and poll_interval > 0:
        extra["lifespan"] = _tick_lifespan(control_plane, poll_interval)
    return Starlette(
        routes=[
            Route("/", index),
            Route("/health", health),
            Route("/ready", ready),
            Route("/api/goal", post_goal, methods=["POST"]),
            Route("/api/answer", post_answer, methods=["POST"]),
            Route("/api/runs/{run_id}", get_run),
            Route("/api/runs/{run_id}/events", get_events),
            Route("/events/{run_id}", sse),
        ],
        exception_handlers={Exception: _on_error},
        **extra,
    )

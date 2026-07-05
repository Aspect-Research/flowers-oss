"""The web dashboard channel — SSE down / POST up (the primary surface for now).

``WebChannel`` appends every event to the DURABLE per-run event log in the store (so both a
reconnecting client and a restarted server replay the same timeline) and feeds live SSE streams.
``create_app`` builds a Starlette ASGI app exposing:

  POST /api/goal        {text, budget}               -> start a run
  POST /api/answer      {run_id, text}               -> answer a clarify/approval/escalation
  GET  /api/runs/{id}                                -> run status
  GET  /api/runs/{id}/events[?after=<eid>]           -> the event log (JSON; polling fallback + tests)
  GET  /events/{id}[?after=<eid>|replay_only=1]      -> SSE stream (replay + live; id: = resume cursor)
  GET  /                                             -> the dashboard (a static asset in this package)

A run is created synchronously (its id returns at once) then DRIVEN in a background worker thread, so its
plan/progress/done events stream live over SSE as they happen rather than arriving in one batch after the
run finishes. It's a single-user local surface — bind it to localhost; there is no auth.
Requires the ``[web]`` extra (starlette).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from importlib import resources

from flowers.channels.base import Channel

_log = logging.getLogger("flowers.serve")


class WebChannel(Channel):
    """Store-backed: ``emit`` appends to the DURABLE per-run event log and ``log`` reads it back, so a
    reconnecting client — or a client whose server restarted mid-run — replays the same timeline. Each
    event gets a per-run monotonic ``id`` (assigned by the store), the SSE resume cursor. The store
    serializes its own access; no channel lock is needed."""

    def __init__(self, store):
        self._store = store

    def emit(self, event: dict) -> None:
        rid = event.get("run_id") or "_"
        event["id"] = self._store.append_event(rid, event)

    def log(self, run_id: str, *, after: int = 0) -> list[dict]:
        return self._store.get_events(run_id, after=after)


def _sse(event: dict) -> str:
    # The `id:` field feeds EventSource's Last-Event-ID, so an auto-reconnect resumes exactly where
    # the previous connection left off — even across a server restart (ids are durable).
    return (f"id: {event.get('id', '')}\nevent: {event.get('kind', 'message')}\n"
            f"data: {json.dumps(event)}\n\n")


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


def create_app(control_plane, channel: WebChannel, *, poll_interval: float | None = None,
               degraded: str | None = None, keepalive_seconds: float = 15.0):
    """Build the Starlette app — the local dashboard + REST API. No auth: it is a single-user local
    surface, so bind it to localhost.

    ``poll_interval`` (seconds, >0) turns on the background timer DRIVER: a served app fires due timers
    via ``control_plane.tick()`` so parked await/monitor runs resume. ``None``/0 leaves it off (the
    default — for in-request/test use). The poller only runs while the app is actually SERVING (its ASGI
    lifespan is entered), so constructing the app for a unit test does not spawn a thread.

    ``degraded`` (a human-readable reason, e.g. "no model configured") makes ``POST /api/goal`` refuse
    up front with an actionable 503 instead of accepting a goal that would die mid-run — failing fast
    beats a spinner that never clears."""
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

    def _spawn(thunk, run_id: str):
        """Drive a run OFF the event loop (in a worker thread) so the request returns immediately and the
        run's plan/progress/done events stream LIVE over SSE as they happen — instead of arriving in a
        batch after a blocking synchronous run. Tracked so the task isn't GC'd. An escaped exception is
        NEVER swallowed silently: the run is marked as an honest ESCALATED outcome (fail_run), so the
        dashboard sees a terminal event instead of a stuck-RUNNING spinner that never clears."""
        async def _run():
            try:
                await asyncio.to_thread(thunk)
            except Exception:
                _log.exception("flowers: background run drive failed")
                try:
                    await asyncio.to_thread(
                        control_plane.fail_run, run_id,
                        "something went wrong while I was working — see the server log; "
                        "reply to continue or start over")
                except Exception:
                    _log.exception("flowers: could not record the drive failure for run %s", run_id)
        task = asyncio.create_task(_run())
        _background.add(task)
        task.add_done_callback(_background.discard)

    async def post_goal(request):
        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "a JSON object body is required"}, status_code=400)
        if degraded:
            # Fail fast: with no usable model the run would die mid-drive, so refuse the goal with an
            # actionable message instead of accepting it and hanging the dashboard.
            return JSONResponse({"error": degraded, "reason_code": "model_unavailable"},
                                status_code=503)
        # Create the run (fast) and return its id at once; DRIVE in the background so events stream live.
        run, goal = await asyncio.to_thread(
            control_plane.begin, goal_text=body.get("text", ""), budget_usd=float(body.get("budget", 2.0)))
        _spawn(lambda: control_plane.drive(run, goal), run.run_id)
        return JSONResponse({"run_id": run.run_id, "status": run.status.value})

    async def post_answer(request):
        body = await request.json()
        if not isinstance(body, dict) or not str(body.get("run_id") or "").strip():
            return JSONResponse({"error": "run_id required"}, status_code=400)
        run, err = _load_run(body["run_id"])
        if err is not None:
            return err
        text = body.get("text", "")
        _spawn(lambda: control_plane.answer(run_id=run.run_id, answer=text), run.run_id)
        return JSONResponse({"run_id": run.run_id, "status": run.status.value})

    async def get_run(request):
        run, err = _load_run(request.path_params["run_id"])
        if err is not None:
            return err
        return JSONResponse({"run_id": run.run_id, "status": run.status.value,
                             "goal": run.goal_text, "spent_usd": run.spent_usd})

    def _after_cursor(request) -> int:
        """The SSE resume cursor: ``?after=<eid>`` (manual reconnects, tests, the polling fallback)
        or the ``Last-Event-ID`` header (EventSource sends it automatically on auto-reconnect).
        Non-numeric/absent -> 0 (full replay)."""
        raw = request.query_params.get("after") or request.headers.get("last-event-id") or "0"
        return int(raw) if str(raw).isdigit() else 0

    async def get_events(request):
        _run, err = _load_run(request.path_params["run_id"])
        if err is not None:
            return err
        return JSONResponse(
            {"events": channel.log(request.path_params["run_id"], after=_after_cursor(request))})

    async def sse(request):
        rid = request.path_params["run_id"]
        _run, err = _load_run(rid)
        if err is not None:
            return err
        cursor = _after_cursor(request)
        replay_only = request.query_params.get("replay_only")

        async def gen():
            nonlocal cursor
            # Replay-then-tail from the durable log, cursor-based (a cheap indexed query). No fixed
            # connection ceiling: the stream lives until the run CLOSES (done/stopped — an escalated
            # run stays open: it is parked on the owner, who answers in this same conversation) or
            # the client goes away. During quiet stretches (a long model call, a parked run) an SSE
            # comment keeps the connection visibly alive through proxies and looks-frozen UIs.
            idle = 0.0
            while True:
                batch = channel.log(rid, after=cursor)
                for ev in batch:
                    cursor = ev["id"]
                    yield _sse(ev)
                if batch:
                    idle = 0.0
                run = control_plane.store.get_run(rid)
                closed = run is None or run.status.value in ("done", "stopped")
                if replay_only or closed:
                    return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.1)
                idle += 0.1
                if idle >= keepalive_seconds:
                    idle = 0.0
                    yield ": keepalive\n\n"

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

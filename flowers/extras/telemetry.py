"""Optional telemetry adapter — Langfuse.

``LangfuseTracer`` is an optional adapter template (not wired into the default ``build_app``; the wired
defaults are ``NoOpTracer`` / ``LocalTracer`` in ``flowers/seams/telemetry.py``). It ships spans to
Langfuse via the raw ingestion API (no SDK dependency — stdlib urllib). Each span becomes a Langfuse
observation under a per-RUN trace, tagged ``run_id``. Gated on ``LANGFUSE_SECRET_KEY``
(unavailable offline). A telemetry ship failure NEVER breaks a run. To use this, swap it for the local
tracer in ``build_app``.
"""

from __future__ import annotations

import base64
import datetime
import json
import traceback
import urllib.request

from flowers import runtime
from flowers.seams.interfaces import Span
from flowers.types import new_id


def _iso(dt: datetime.datetime) -> str:
    """ISO-8601, UTC, millisecond precision, trailing Z — the format Langfuse ingestion requires."""
    return dt.astimezone(datetime.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _LangfuseSpan:
    """SpanHandle for Langfuse: times the ``with`` block (wall clock, for ISO start/end), captures cost
    + error, and on exit ships a per-run trace + this observation to Langfuse via the raw ingestion API.
    A ship failure is swallowed — telemetry must never break the run."""

    def __init__(self, tracer: LangfuseTracer, name: str, run_id: str, attrs: dict) -> None:
        self._tracer = tracer
        self.name = name
        self.run_id = run_id
        self.attributes: dict = dict(attrs)
        self.cost_usd: float = 0.0
        self.error: str | None = None
        self._start: datetime.datetime | None = None
        self._end: datetime.datetime | None = None

    def __enter__(self) -> _LangfuseSpan:
        self._start = datetime.datetime.now(datetime.UTC)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._end = datetime.datetime.now(datetime.UTC)
        if exc is not None:
            self.error = "".join(traceback.format_exception_only(exc_type, exc)).strip()
        self._tracer._ship(self)
        return False   # never swallow the caller's exception

    def set(self, **attrs) -> None:
        self.attributes.update(attrs)

    def add_cost(self, usd: float) -> None:
        self.cost_usd += float(usd or 0.0)


class LangfuseTracer:
    """Optional Langfuse tracer — ships spans to Langfuse via the raw ingestion API (POST /api/public/ingestion,
    HTTP Basic ``public:secret``), NO SDK dependency (the stdlib core stays dep-free). Each span becomes
    a Langfuse observation under a per-RUN trace, tagged ``run_id``; a span carrying cost
    is shipped as a ``generation-create`` with ``costDetails`` — the ONLY observation kind whose cost
    Langfuse rolls up (a plain span's cost is silently ignored). Gated on ``LANGFUSE_SECRET_KEY``
    (unavailable offline → the engine uses NoOp/Local). A telemetry ship failure NEVER breaks a run.
    """

    KEY_ENV = "LANGFUSE_SECRET_KEY"

    def __init__(self, *, public_key: str | None = None, secret_key: str | None = None,
                 host: str | None = None, timeout: float = 10.0) -> None:
        self._public = public_key or runtime.env("LANGFUSE_PUBLIC_KEY")
        self._secret = secret_key or runtime.env(self.KEY_ENV)
        self._host = (host or runtime.env("LANGFUSE_HOST") or "https://cloud.langfuse.com").rstrip("/")
        self._timeout = timeout

    def available(self) -> bool:
        return runtime.adapter_available(key_env=self.KEY_ENV)

    def span(self, name: str, *, run_id: str = "", **attrs) -> _LangfuseSpan:
        return _LangfuseSpan(self, name, run_id, attrs)

    def spans(self) -> list[Span]:
        return []   # live spans live in Langfuse, not buffered here

    def trace_id_for(self, run_id: str) -> str:
        return "flowers-" + (run_id or "no-run")

    def _ship(self, h: _LangfuseSpan) -> None:
        if not (self.available() and self._public and self._secret):
            return   # not configured / offline -> silent no-op (NoOp/Local are the wired defaults)
        try:
            now = datetime.datetime.now(datetime.UTC)
            start, end = _iso(h._start or now), _iso(h._end or now)
            obs = {"id": new_id("obs"), "traceId": self.trace_id_for(h.run_id), "name": h.name,
                   "startTime": start, "endTime": end,
                   "metadata": {"run_id": h.run_id, **h.attributes}}
            if h.error:
                obs["level"] = "ERROR"
                obs["statusMessage"] = h.error[:500]
            if h.cost_usd and h.cost_usd > 0:
                obs_type = "generation-create"
                obs["model"] = str(h.attributes.get("model") or h.attributes.get("role") or h.name)
                obs["costDetails"] = {"total": float(h.cost_usd)}   # only generations roll up cost
            else:
                obs_type = "span-create"
            batch = {"batch": [
                {"id": new_id("evt"), "type": "trace-create", "timestamp": start,
                 "body": {"id": self.trace_id_for(h.run_id), "name": "flowers run " + (h.run_id or ""),
                          "userId": None, "metadata": {}}},
                {"id": new_id("evt"), "type": obs_type, "timestamp": end, "body": obs},
            ]}
            auth = base64.b64encode(f"{self._public}:{self._secret}".encode()).decode()
            req = urllib.request.Request(
                self._host + "/api/public/ingestion",
                data=json.dumps(batch).encode("utf-8"), method="POST",
                headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                r.read()
        except Exception:
            return   # a telemetry ship failure must never affect the run

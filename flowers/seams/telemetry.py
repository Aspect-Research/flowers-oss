"""Telemetry seam — the ``Tracer`` / ``SpanHandle`` implementations.

Two implementations conform to the Protocols in ``flowers.seams.interfaces``:

  * :class:`NoOpTracer`  — the zero-overhead default; discards everything.
  * :class:`LocalTracer` — the WIRED default; records completed spans in-memory for tests + a dashboard.

The optional ``LangfuseTracer`` adapter lives in ``flowers/extras/telemetry.py``. Every span carries a
``run_id`` so cost and errors roll up per run.
"""

from __future__ import annotations

import time
import traceback

from flowers.seams.interfaces import Span

# --------------------------------------------------------------------------- no-op

class _NoOpSpan:
    """A zero-overhead SpanHandle: a context manager that records nothing.

    ``set`` / ``add_cost`` are accepted and ignored so call sites are identical regardless of which
    tracer is wired. ``__exit__`` returns False so exceptions propagate normally.
    """

    def __enter__(self) -> _NoOpSpan:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def set(self, **attrs) -> None:
        return None

    def add_cost(self, usd: float) -> None:
        return None


class NoOpTracer:
    """The default tracer: discards all spans with zero overhead. ``spans()`` is always empty."""

    def available(self) -> bool:
        return True

    def span(self, name: str, *, run_id: str = "", **attrs) -> _NoOpSpan:
        return _NoOpSpan()

    def spans(self) -> list[Span]:
        return []


# --------------------------------------------------------------------------- local

class _LocalSpan:
    """A recording SpanHandle. Times itself across the ``with`` block and, on exit, appends a fully
    populated :class:`Span` (name/run/attrs/cost/error/duration) to the owning tracer's list.

    On exception inside the block it captures the formatted exception into ``error`` and RE-RAISES
    (``__exit__`` returns False).
    """

    def __init__(self, tracer: LocalTracer, name: str, run_id: str, attrs: dict) -> None:
        self._tracer = tracer
        self.name = name
        self.run_id = run_id
        self.attributes: dict = dict(attrs)
        self.cost_usd: float = 0.0
        self.error: str | None = None
        self.duration_s: float = 0.0
        self._start: float = 0.0

    def __enter__(self) -> _LocalSpan:
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.duration_s = time.perf_counter() - self._start
        if exc is not None:
            self.error = "".join(traceback.format_exception_only(exc_type, exc)).strip()
        self._tracer._record(self)
        # Return False: never swallow the exception — the caller's flow must see it.
        return False

    def set(self, **attrs) -> None:
        """Merge attributes into the span (last write wins)."""
        self.attributes.update(attrs)

    def add_cost(self, usd: float) -> None:
        """Accumulate cost (in USD) onto this span."""
        self.cost_usd += usd


class LocalTracer:
    """An in-memory tracer for dev/test (and a future local dashboard). Completed spans are kept in
    insertion order and exposed via :meth:`spans`. Duration is stored on each span's ``attributes``
    under ``"duration_s"`` so it survives the plain :class:`Span` dataclass."""

    def __init__(self) -> None:
        self._spans: list[Span] = []

    def available(self) -> bool:
        return True

    def span(self, name: str, *, run_id: str = "", **attrs) -> _LocalSpan:
        return _LocalSpan(self, name, run_id, attrs)

    def spans(self) -> list[Span]:
        return list(self._spans)

    def _record(self, handle: _LocalSpan) -> None:
        attributes = dict(handle.attributes)
        attributes["duration_s"] = handle.duration_s
        self._spans.append(
            Span(
                name=handle.name,
                run_id=handle.run_id,
                attributes=attributes,
                cost_usd=handle.cost_usd,
                error=handle.error,
            )
        )

"""Telemetry seam — offline tests for the Tracer / SpanHandle implementations.

No network: LangfuseTracer must report unavailable under the offline contract, and the
NoOp/Local tracers are pure in-memory.
"""

from __future__ import annotations

import pytest

from flowers.extras.telemetry import LangfuseTracer
from flowers.seams.interfaces import SpanHandle, Tracer
from flowers.seams.telemetry import LocalTracer, NoOpTracer


def test_localtracer_records_name_ids_attrs_and_positive_duration():
    tracer = LocalTracer()
    assert tracer.available() is True
    assert isinstance(tracer, Tracer)

    with tracer.span("plan", tenant_id="t_1", run_id="r_1", phase="planning") as s:
        assert isinstance(s, SpanHandle)
        s.set(model="planner")
        # do a tiny bit of work so duration is measurably positive
        sum(range(1000))

    (recorded,) = tracer.spans()
    assert recorded.name == "plan"
    assert recorded.tenant_id == "t_1"
    assert recorded.run_id == "r_1"
    assert recorded.attributes["phase"] == "planning"
    assert recorded.attributes["model"] == "planner"
    assert recorded.attributes["duration_s"] >= 0.0
    assert recorded.error is None


def test_localtracer_add_cost_accumulates_into_recorded_span():
    tracer = LocalTracer()
    with tracer.span("exec", tenant_id="t_1", run_id="r_2") as s:
        s.add_cost(0.01)
        s.add_cost(0.02)

    (recorded,) = tracer.spans()
    assert recorded.cost_usd == pytest.approx(0.03)


def test_localtracer_captures_exception_and_reraises():
    tracer = LocalTracer()

    with pytest.raises(ValueError, match="boom"):
        with tracer.span("risky", tenant_id="t_1", run_id="r_3"):
            raise ValueError("boom")

    # the span was still recorded, with the exception captured into error
    (recorded,) = tracer.spans()
    assert recorded.name == "risky"
    assert recorded.error is not None
    assert "boom" in recorded.error
    assert "ValueError" in recorded.error


def test_noop_tracer_is_a_context_manager_and_records_nothing():
    tracer = NoOpTracer()
    assert tracer.available() is True
    assert isinstance(tracer, Tracer)

    with tracer.span("anything", tenant_id="t_1", run_id="r_4") as s:
        assert isinstance(s, SpanHandle)
        s.set(foo="bar")
        s.add_cost(1.0)

    assert tracer.spans() == []


def test_noop_tracer_does_not_swallow_exceptions():
    tracer = NoOpTracer()
    with pytest.raises(ValueError):
        with tracer.span("x"):
            raise ValueError("nope")
    assert tracer.spans() == []


def test_langfuse_tracer_unavailable_offline():
    tracer = LangfuseTracer()
    assert isinstance(tracer, Tracer)
    assert tracer.available() is False
    # spans() is empty for the live tracer (spans live in Langfuse)
    assert tracer.spans() == []


def test_langfuse_span_is_a_safe_noop_when_unavailable():
    # offline, a span must NOT ship and must NOT raise — telemetry can never break a run.
    tracer = LangfuseTracer()
    assert tracer.available() is False
    with tracer.span("model", tenant_id="t1", run_id="r1") as sp:
        sp.set(role="planner")
        sp.add_cost(0.0123)
    assert sp.cost_usd == 0.0123
    tracer._ship(sp)                 # internally gated -> a no-op offline (no network, no raise)

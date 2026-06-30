"""The control plane — intake, answer routing, timer tick."""

from __future__ import annotations

import json

from flowers.controlplane import ControlPlane
from flowers.engine.operator import Operator
from flowers.seams.integrations import FakeIntegrations
from flowers.seams.interfaces import ModelResponse
from flowers.seams.model import FakeModel
from flowers.seams.search import FakeSearch
from flowers.seams.store import SqliteStore
from flowers.seams.timers import LocalTimers
from flowers.types import RunStatus, ToolCall


def _brain(steps, actions=None):
    actions = actions or {}
    def fn(messages, tools, role):
        if role == "planner" and "intake step" in messages[0]["content"]:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            return ModelResponse(content=json.dumps({"steps": steps}))
        user = messages[1]["content"]
        acts = next((a for s, a in actions.items() if s in user), [])
        n = sum(1 for m in messages if m.get("role") == "tool")
        if n < len(acts):
            return ModelResponse(tool_calls=[acts[n]], finish_reason="tool_calls")
        return ModelResponse(tool_calls=[ToolCall(name="finish", args={"summary": "done"})],
                             finish_reason="tool_calls")
    return FakeModel(on_complete=fn)


def _cp(model, *, integrations=None, timers=None):
    store = SqliteStore()
    op = Operator(store=store, model=model, search=FakeSearch(),
                  integrations=integrations or FakeIntegrations(), timers=timers or LocalTimers())
    return ControlPlane(store=store, operator=op), store


def test_intake_creates_and_starts_a_run():
    model = _brain([{"text": "write a file"}],
                   {"write a file": [ToolCall(name="write_file", args={"path": "a.md", "content": "x"})]})
    cp, _store = _cp(model)
    run = cp.intake(goal_text="write a file", budget_usd=1.0)
    assert run.status is RunStatus.DONE
    assert run.tenant_id == "local"   # single local user


def test_tick_resumes_due_monitor_timer():
    integ = FakeIntegrations()
    timers = LocalTimers()
    model = _brain([{"text": "watch for the bank email", "kind": "monitor",
                     "params": {"interval_seconds": 100, "match": {"from": "bank@chase.com"},
                                "notify": "arrived"}}])
    cp, store = _cp(model, integrations=integ, timers=timers)
    run = cp.intake(goal_text="watch the bank email")
    assert run.status is RunStatus.WAITING
    # nothing yet: a due timer with no match reschedules and stays waiting
    timers.advance(101)
    cp.tick()
    assert store.get_run(run.run_id).status is RunStatus.WAITING
    # the email arrives; next due tick finds the verified match -> done
    integ.deliver_inbound(run.tenant_id, sender="bank@chase.com", subject="loan", body="ok")
    timers.advance(101)
    cp.tick()
    assert store.get_run(run.run_id).status is RunStatus.DONE

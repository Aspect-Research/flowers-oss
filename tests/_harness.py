"""Scenario harness — drive the REAL engine with scripted seams ($0, offline).

This is the salvaged-from-the-offline-test-suite pattern: every DECISION is the real code (planner, scheduler,
executor, trust gate, persistence); only the INPUTS are scripted (the model's output, search results,
the integration world, owner answers, the clock). A scenario builds one ``make_brain`` (a FakeModel
serving clarifier+planner+executor by role/content) and a ``build(...)`` bundle of real components.

Not a test module (no ``test_`` prefix) — imported by the e2e tests.
"""

from __future__ import annotations

import json
import re

from flowers.channels.inproc import InProcChannel
from flowers.controlplane import ControlPlane
from flowers.engine.operator import Operator
from flowers.seams.integrations import FakeIntegrations
from flowers.seams.interfaces import ModelResponse
from flowers.seams.model import FakeModel
from flowers.seams.search import FakeSearch
from flowers.seams.store import SqliteStore
from flowers.seams.timers import LocalTimers
from flowers.types import ToolCall


def make_brain(*, questions=None, steps=None, actions=None, mandate=None, verdict=None):
    """One FakeModel serving the clarifier, planner, verifier, and executor by role + message content.

    * ``questions``: list returned by the clarifier.
    * ``steps``: the plan JSON steps the planner returns.
    * ``actions``: {step_text_substr: [ToolCall, ...]} — the executor emits these in order for the
      matching step, then calls finish().
    * ``mandate``: optional mandate dict the planner returns alongside the steps (the autonomy card).
    * ``verdict``: the independent verifier's verdict dict (default: satisfied). Use
      ``{"satisfied": False, "unmet": [...]}`` to script a constraint failure.
    """
    questions = questions or []
    steps = steps or [{"text": "do it"}]
    actions = actions or {}
    verdict = verdict if verdict is not None else {"satisfied": True}

    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        if role == "verifier":
            return ModelResponse(content=json.dumps(verdict))
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": questions}))
        if role == "planner":
            payload = {"steps": steps}
            if mandate is not None:
                payload["mandate"] = mandate
            return ModelResponse(content=json.dumps(payload))
        user = messages[1]["content"]
        # Match against THIS step's text only (the blob also lists the whole plan, which would
        # otherwise let a later step match an earlier step's action key).
        m = re.search(r"YOUR STEP \(\d+\): (.+)", user)
        stepname = m.group(1) if m else user
        acts = next((a for substr, a in actions.items() if substr in stepname), [])
        n = sum(1 for m in messages if m.get("role") == "tool")
        if n < len(acts):
            return ModelResponse(tool_calls=[acts[n]], finish_reason="tool_calls")
        return ModelResponse(tool_calls=[ToolCall(name="finish", args={"summary": "done"})],
                             finish_reason="tool_calls")

    return FakeModel(on_complete=fn)


def build(*, model, integrations=None, timers=None, store=None, search=None, channel=None,
          browser=None, tracer=None):
    """Assemble a real Operator + ControlPlane over (mostly default) fakes. Returns a dict bundle."""
    store = store if store is not None else SqliteStore()
    timers = timers if timers is not None else LocalTimers()
    integ = integrations if integrations is not None else FakeIntegrations()
    channel = channel if channel is not None else InProcChannel()
    op = Operator(store=store, model=model, search=search or FakeSearch(),
                  integrations=integ, timers=timers, channel=channel, browser=browser,
                  tracer=tracer)
    cp = ControlPlane(store=store, operator=op)
    return {"op": op, "cp": cp, "store": store, "timers": timers, "integ": integ, "channel": channel,
            "browser": browser, "tracer": op.tracer}


def tc(name, **args):
    return ToolCall(name=name, args=args)

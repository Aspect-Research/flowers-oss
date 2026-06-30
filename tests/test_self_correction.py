"""The bounded-redirect SELF-CORRECTION loop — a gate-refused step is re-run WITH feedback and reaches
DONE on a later attempt (the redirect path, distinct from the terminal escalate-on-cap / needs_owner /
allow_redirect=False branches that were already covered).

A 2026-06-22 grounded audit found this load-bearing behavior had ZERO direct coverage: "PRIOR ATTEMPT
WAS REJECTED" / `_redirects` / `_feedback` existed in the operator+executor but in no test. This locks it.
"""

from __future__ import annotations

import json

from _harness import build

from flowers.seams.interfaces import ModelResponse
from flowers.seams.model import FakeModel
from flowers.types import Goal, RunStatus, ToolCall


def test_gate_refused_step_is_redirected_with_feedback_then_completes():
    # Attempt 1 claims done on NOTHING (empty deliverable, no work) -> the gate's `unsupported-completion`
    # reliability signal refuses it REDIRECTABLY (needs_owner=False). The operator re-runs the SAME step
    # with `_feedback`; attempt 2 sees "PRIOR ATTEMPT WAS REJECTED" and produces a real deliverable -> the
    # gate accepts -> DONE. Proves: a refused step self-corrects and proceeds (not just escalates).
    calls = {"rejected": 0, "accepted": 0}

    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        if role == "verifier":
            return ModelResponse(content=json.dumps({"satisfied": True}))
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            return ModelResponse(content=json.dumps({"steps": [{"text": "write the summary"}]}))
        blob = messages[1]["content"]
        if "PRIOR ATTEMPT WAS REJECTED" in blob:          # the feedback reached the model
            calls["accepted"] += 1
            return ModelResponse(
                tool_calls=[ToolCall(name="finish",
                                     args={"completed": True, "summary": "The full writeup, with content."})],
                finish_reason="tool_calls")
        calls["rejected"] += 1
        return ModelResponse(content="", finish_reason="stop")   # claim done out of thin air -> refuse

    h = build(model=FakeModel(on_complete=fn))
    run = h["op"].start(Goal(text="summarize the thing"))

    assert run.status is RunStatus.DONE                  # self-corrected to done, did NOT escalate
    assert calls["rejected"] == 1 and calls["accepted"] == 1   # exactly one redirect, driven by feedback


def test_redirect_is_bounded_then_escalates():
    # If every attempt is unsupported, the redirect loop is BOUNDED (_MAX_REDIRECTS) and then escalates —
    # it never loops forever and never fabricates a done. (The cap side of the same loop.)
    attempts = {"n": 0}

    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            return ModelResponse(content=json.dumps({"steps": [{"text": "write the summary"}]}))
        attempts["n"] += 1
        return ModelResponse(content="", finish_reason="stop")   # always empty -> always refused

    h = build(model=FakeModel(on_complete=fn))
    run = h["op"].start(Goal(text="summarize the thing"))

    assert run.status is RunStatus.ESCALATED             # bounded: never DONE, never an infinite loop
    assert attempts["n"] >= 2                             # the initial attempt + at least one bounded redirect

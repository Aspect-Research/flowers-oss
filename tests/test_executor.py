"""The executor — verified side-effects, approval parking, and the anti-thrash circuit breaker."""

from __future__ import annotations

from flowers.broker import Broker
from flowers.engine.executor import Executor
from flowers.engine.scheduler import SemanticBudget
from flowers.seams.integrations import FakeIntegrations
from flowers.seams.interfaces import ModelResponse
from flowers.seams.model import FakeModel
from flowers.seams.sandbox import LocalSubprocessSandbox
from flowers.seams.search import FakeSearch
from flowers.types import Goal, Plan, PlanStep, ToolCall


def _step_plan(text="email the venue"):
    step = PlanStep(index=0, text=text)
    return step, Plan(steps=[step], goal_text=text)


def _broker(model, *, search=None, integrations=None):
    return Broker(model=model, search=search or FakeSearch(),
                  integrations=integrations or FakeIntegrations(), run_id="run_1")


def test_verified_send_then_finish(tmp_path):
    model = FakeModel([
        ModelResponse(tool_calls=[ToolCall(name="send_email",
                      args={"to": "bob@acme.com", "subject": "Venue inquiry"})],
                      finish_reason="tool_calls"),
        ModelResponse(tool_calls=[ToolCall(name="finish", args={"summary": "emailed the venue"})],
                      finish_reason="tool_calls"),
    ])
    step, plan = _step_plan()
    b = _broker(model)
    # the grant binds to the EXACT params the send_email tool builds (incl. the defaulted body="")
    gk = b.grant_key_for("gmail", "GMAIL_SEND_EMAIL",
                         {"to": "bob@acme.com", "subject": "Venue inquiry", "body": ""})
    sb = LocalSubprocessSandbox(workdir=str(tmp_path))
    res = Executor().run(step, plan=plan, goal=Goal(text="organize the venue"), broker=b,
                         sandbox=sb, grants={gk})
    sb.close()
    assert res.claimed_done is True and len(res.effects) == 1
    eff = res.effects[0]
    assert eff.phase == "forwarded" and eff.expected_present is True
    assert res.searches == 0


def test_unauthorized_send_parks_for_approval(tmp_path):
    model = FakeModel([
        ModelResponse(tool_calls=[ToolCall(name="send_email",
                      args={"to": "bob@acme.com", "subject": "Venue inquiry"})],
                      finish_reason="tool_calls"),
    ])
    step, plan = _step_plan()
    sb = LocalSubprocessSandbox(workdir=str(tmp_path))
    res = Executor().run(step, plan=plan, goal=Goal(text="x"), broker=_broker(model), sandbox=sb)
    sb.close()
    assert res.claimed_done is False
    assert res.signals.get("needs_approval") is not None
    assert res.signals["needs_approval"].kind == "side_effect"
    assert res.signals["pending_action"]["action"] == "GMAIL_SEND_EMAIL"   # carried for resume-at-action
    assert "resume" in res.signals                                          # the parked loop state
    assert res.effects and res.effects[-1].phase == "deferred"


def test_blocked_search_circuit_breaks_not_thrash(tmp_path):
    # THE anti-thrash proof: a blocked search stops after the breaker threshold (2), not 50 times.
    model = FakeModel(on_complete=lambda m, t, r: ModelResponse(
        tool_calls=[ToolCall(name="web_search", args={"query": "party venue near me"})],
        finish_reason="tool_calls"))
    step, plan = _step_plan("find a venue")
    sb = LocalSubprocessSandbox(workdir=str(tmp_path))
    res = Executor().run(step, plan=plan, goal=Goal(text="organize the venue"),
                         broker=_broker(model, search=FakeSearch(blocked={"venue"})), sandbox=sb)
    sb.close()
    assert res.claimed_done is False
    assert res.signals.get("tool_failed") == "web_search"
    assert res.searches == 2          # tripped after 2 consecutive failures — NOT 50


def test_search_budget_caps_actual_searches(tmp_path):
    # Even if the model keeps asking, the number of ACTUAL searches is capped (no budget-burning loop).
    model = FakeModel(on_complete=lambda m, t, r: ModelResponse(
        tool_calls=[ToolCall(name="web_search", args={"query": "venues"})],
        finish_reason="tool_calls"))
    step, plan = _step_plan("find venues")
    sb = LocalSubprocessSandbox(workdir=str(tmp_path))
    res = Executor(budget=SemanticBudget(max_searches=8, max_iterations=20)).run(
        step, plan=plan, goal=Goal(text="x"),
        broker=_broker(model, search=FakeSearch()), sandbox=sb)  # default: ok=True, results=[]
    sb.close()
    assert res.searches == 8          # capped — actual searches never exceed the budget
    # the step did not complete and was NOT falsely claimed done (blocked/exhausted, never fabricated)
    assert res.claimed_done is False
    assert res.signals.get("blocked") or res.signals.get("exhausted")


def test_write_file_step(tmp_path):
    model = FakeModel([
        ModelResponse(tool_calls=[ToolCall(name="write_file",
                      args={"path": "brief.md", "content": "Hello world"})], finish_reason="tool_calls"),
        ModelResponse(tool_calls=[ToolCall(name="finish", args={"summary": "wrote the brief"})],
                      finish_reason="tool_calls"),
    ])
    step, plan = _step_plan("write a brief")
    sb = LocalSubprocessSandbox(workdir=str(tmp_path))
    res = Executor().run(step, plan=plan, goal=Goal(text="x"), broker=_broker(model), sandbox=sb)
    assert res.claimed_done is True
    assert sb.read_file("brief.md") == "Hello world"
    sb.close()

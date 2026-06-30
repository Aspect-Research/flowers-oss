"""P2 executor-effectiveness + methodical-pacing regressions (M2 + M3)."""

from __future__ import annotations

import json

from _harness import build, make_brain, tc

from flowers.engine.executor import Executor
from flowers.engine.planner import Planner
from flowers.seams.integrations import FakeIntegrations
from flowers.seams.model import FakeModel
from flowers.seams.timers import LocalTimers
from flowers.types import Goal, Plan, PlanStep, RunStatus

# --- M2a: resume-at-action ---

def test_resume_at_action_sends_exactly_the_approved_email():
    model = make_brain(steps=[{"text": "email bob@acme.com about the venue"}],
                       actions={"email bob": [tc("send_email", to="bob@acme.com", subject="Venue inquiry")]})
    h = build(model=model)
    run = h["op"].start(Goal(text="email bob"))
    assert run.status is RunStatus.AWAITING_APPROVAL
    run2 = h["cp"].answer(run_id=run.run_id, answer="yes")
    assert run2.status is RunStatus.DONE
    sent = h["integ"].surface("local", "sent")
    assert len(sent) == 1 and next(iter(sent.values()))["to"] == "bob@acme.com"


def test_resume_at_action_handles_a_two_send_batch_with_separate_approvals():
    # A step that sends to two venues parks once per send; each is approved separately; both land once.
    model = make_brain(steps=[{"text": "email both venues"}],
                       actions={"email both venues": [
                           tc("send_email", to="v1@hall.com", subject="Inquiry"),
                           tc("send_email", to="v2@hall.com", subject="Inquiry")]})
    h = build(model=model)
    run = h["op"].start(Goal(text="email both venues"))
    assert run.status is RunStatus.AWAITING_APPROVAL                     # first send parked
    run = h["cp"].answer(run_id=run.run_id, answer="yes")                # approve v1 -> resume -> hits v2 -> park
    assert run.status is RunStatus.AWAITING_APPROVAL                     # second send parked
    run = h["cp"].answer(run_id=run.run_id, answer="yes")                # approve v2 -> resume -> finish
    assert run.status is RunStatus.DONE
    tos = sorted(v["to"] for v in h["integ"].surface("local", "sent").values())
    assert tos == ["v1@hall.com", "v2@hall.com"]                        # both sent, once each


# --- M2c: planner attaches effect_landed from `produces` ---

def test_planner_produces_attaches_effect_landed():
    p = Planner(FakeModel([]))
    content = json.dumps({"steps": [
        {"text": "email the venue", "produces": "gmail:GMAIL_SEND_EMAIL"},
        {"text": "just research", "kind": "generic"},
    ]})
    steps = p._parse_steps(content, "goal")
    assert any(c.get("objective_check", {}).get("kind") == "effect_landed"
               and c["objective_check"]["params"]["label"] == "gmail:GMAIL_SEND_EMAIL"
               for c in steps[0].done_criteria)
    assert steps[1].done_criteria == []   # non-side-effecting step gets none


# --- M2b: tool affordances are injected into the executor's context ---

def test_executor_blob_lists_available_tools():
    step = PlanStep(index=0, text="do it")
    plan = Plan(steps=[step], goal_text="g")
    blob = Executor._step_blob(step, plan, Goal(text="g"),
                               available_tools=["gmail:GMAIL_SEND_EMAIL - Send an email"])
    assert "AVAILABLE INTEGRATIONS" in blob and "gmail:GMAIL_SEND_EMAIL" in blob


# --- M3: await -> next batch, then complete on a later reply ---

def test_await_next_batch_then_completes_on_a_later_reply():
    steps = [{"text": "watch for the venue reply", "kind": "await_replies",
              "params": {"window_seconds": 3600, "min_replies": 1, "match": {"from": "venue@hall.com"}}},
             {"text": "note the reply", "depends_on": [0]}]
    actions = {"note the reply": [tc("write_file", path="n.md", content="they replied")]}
    integ = FakeIntegrations()
    timers = LocalTimers()
    h = build(model=make_brain(steps=steps, actions=actions), integrations=integ, timers=timers)
    run = h["op"].start(Goal(text="organize the venue"))
    assert run.status is RunStatus.WAITING
    # deadline with no reply -> send the next batch (replan); still waiting, replans incremented
    timers.advance(10 ** 7)
    h["cp"].tick()
    mid = h["store"].get_run(run.run_id)
    assert mid.status is RunStatus.WAITING and mid.replans >= 1
    # a matching reply arrives on the next batch -> await completes -> note -> DONE
    integ.deliver_inbound("local", sender="venue@hall.com", subject="re: venue", body="available")
    assert h["cp"].deliver(run_id=run.run_id).status is RunStatus.DONE

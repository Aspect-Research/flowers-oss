"""Part II — relentlessness: the wall-clock deadline terminator, the feedback ladder (keep trying past
2-3 attempts until budget/time, then escalate honestly), relentless await batches, and the recurring
step kind. Real engine, scripted seams, virtual clock."""

from __future__ import annotations

import json
import re

from _harness import build, make_brain, tc

from flowers.seams.integrations import FakeIntegrations
from flowers.seams.interfaces import ModelResponse
from flowers.seams.model import FakeModel
from flowers.seams.search import FakeSearch
from flowers.seams.timers import LocalTimers
from flowers.types import Goal, RunStatus


def _always_unsupported_model():
    """Planner returns one generic step; the executor always claims done on NOTHING -> the gate refuses it
    redirectably (unsupported-completion), driving the relentless ladder."""
    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            return ModelResponse(content=json.dumps({"steps": [{"text": "write the summary"}]}))
        return ModelResponse(content="", finish_reason="stop")
    return FakeModel(on_complete=fn)


# --------------------------------------------------------------------------- the ladder

def test_ladder_is_relentless_then_escalates_at_hard_cap():
    # $0 model + no deadline -> the ladder climbs to the hard cap (12), FAR past the old _MAX_REDIRECTS=2,
    # then escalates honestly (never DONE, never an infinite loop).
    attempts = {"n": 0}

    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            return ModelResponse(content=json.dumps({"steps": [{"text": "write the summary"}]}))
        attempts["n"] += 1
        return ModelResponse(content="", finish_reason="stop")

    h = build(model=FakeModel(on_complete=fn))
    run = h["op"].start(Goal(text="summarize"))
    assert run.status is RunStatus.ESCALATED
    assert attempts["n"] >= 8           # relentless: many more attempts than the old 2-3


def test_ladder_escalating_feedback_reaches_the_model():
    # the ladder doesn't just retry — it ESCALATES the feedback (fix -> different approach -> alt channel).
    hints = {"alt": False}

    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            return ModelResponse(content=json.dumps({"steps": [{"text": "do the thing"}]}))
        blob = messages[1]["content"]
        if "ALTERNATE CHANNEL" in blob:     # a higher ladder rung's hint reached the model
            hints["alt"] = True
        return ModelResponse(content="", finish_reason="stop")

    h = build(model=FakeModel(on_complete=fn))
    h["op"].start(Goal(text="do it"))
    assert hints["alt"] is True


# (budget-termination of the ladder is metered/live — FakeModel is forced to $0 by the offline discipline,
# so it's covered by the live budget path + the pre-existing budget escalate; the NEW deadline_ts
# terminator IS offline-testable on the virtual clock, below.)


# --------------------------------------------------------------------------- deadline via async loops

def test_deadline_terminates_a_watch():
    timers = LocalTimers()
    steps = [{"text": "watch the page", "kind": "monitor",
              "params": {"interval_seconds": 100, "probe": "url", "url": "http://x",
                         "match": {"contains": "never-appears"}}}]
    h = build(model=make_brain(steps=steps), integrations=FakeIntegrations(), timers=timers,
              search=FakeSearch(fetches={"http://x": "nothing here yet"}))
    run = h["op"].start(Goal(text="watch", max_runtime_s=500))
    assert run.status is RunStatus.WAITING and run.deadline_ts is not None
    for _ in range(10):
        timers.advance(101)
        h["cp"].tick()
        if h["store"].get_run(run.run_id).status is RunStatus.ESCALATED:
            break
    assert h["store"].get_run(run.run_id).status is RunStatus.ESCALATED   # stopped at the wall-clock budget


# --------------------------------------------------------------------------- recurring

def _recurring(params):
    return [{"text": "morning ping", "kind": "recurring", "params": params}]


def test_recurring_fires_n_times_then_stops_at_max_occurrences():
    timers = LocalTimers()
    h = build(model=make_brain(steps=_recurring({"interval_seconds": 100, "max_occurrences": 3,
                                                 "notify": "good morning"})), timers=timers)
    run = h["op"].start(Goal(text="remind me each morning"))
    assert run.status is RunStatus.WAITING
    for _ in range(6):
        timers.advance(101)
        h["cp"].tick()
    assert h["store"].get_run(run.run_id).status is RunStatus.DONE
    notifies = [e for e in h["channel"].for_run(run.run_id) if e["kind"] == "notify"]
    assert len(notifies) == 3 and all("good morning" in n["text"] for n in notifies)


def test_recurring_stops_at_until_ts():
    timers = LocalTimers()
    h = build(model=make_brain(steps=_recurring({"interval_seconds": 100,
                                                 "until_ts": timers.now() + 250})), timers=timers)
    run = h["op"].start(Goal(text="x"))
    for _ in range(6):
        timers.advance(101)
        h["cp"].tick()
    assert h["store"].get_run(run.run_id).status is RunStatus.DONE


def test_recurring_stops_at_deadline():
    timers = LocalTimers()
    h = build(model=make_brain(steps=_recurring({"interval_seconds": 100})), timers=timers)  # unbounded occ
    run = h["op"].start(Goal(text="x", max_runtime_s=250))
    for _ in range(6):
        timers.advance(101)
        h["cp"].tick()
    assert h["store"].get_run(run.run_id).status is RunStatus.DONE   # the wall-clock deadline bounds it


def test_recurring_occurrences_persist_in_the_plan():
    timers = LocalTimers()
    h = build(model=make_brain(steps=_recurring({"interval_seconds": 100, "max_occurrences": 5})),
              timers=timers)
    run = h["op"].start(Goal(text="x"))
    timers.advance(101)
    h["cp"].tick()
    # the occurrence counter is durable on the step (survives a park/restart via save_plan)
    step = h["store"].get_plan(run.run_id).steps[0]
    assert int(step.params.get("_occurrences")) == 1


def test_recurring_hard_backstop_terminates_unbounded():
    # an UNBOUNDED recurring (no max_occurrences / until_ts / deadline; a notify is free) MUST still stop at
    # the hard occurrence backstop — R2 (always terminates). Seed near the cap so the test is fast.
    from flowers.engine.operator import _MAX_RECURRING_OCCURRENCES_HARD as CAP
    timers = LocalTimers()
    h = build(model=make_brain(steps=_recurring({"interval_seconds": 100})), timers=timers)  # no bounds
    run = h["op"].start(Goal(text="x"))                                                      # no deadline
    plan = h["store"].get_plan(run.run_id)
    plan.steps[0].params["_occurrences"] = CAP - 1
    h["store"].save_plan(run.run_id, plan)
    for _ in range(3):
        timers.advance(101)
        h["cp"].tick()
    assert h["store"].get_run(run.run_id).status is RunStatus.DONE   # the hard backstop terminated it


def test_malformed_recurring_params_dont_crash_and_still_terminate():
    # junk model-authored bounds (max_occurrences:"three", until_ts:"tomorrow", interval:"hourly") must be
    # coerced fail-closed -> never raise out of tick() -> still bounded by the hard cap.
    from flowers.engine.operator import _MAX_RECURRING_OCCURRENCES_HARD as CAP
    timers = LocalTimers()
    h = build(model=make_brain(steps=_recurring({"interval_seconds": "hourly", "max_occurrences": "three",
                                                 "until_ts": "tomorrow"})), timers=timers)
    run = h["op"].start(Goal(text="x"))
    plan = h["store"].get_plan(run.run_id)
    plan.steps[0].params["_occurrences"] = CAP - 1
    h["store"].save_plan(run.run_id, plan)
    for _ in range(3):
        timers.advance(10 ** 7)
        h["cp"].tick()          # must NOT raise on the junk params
    assert h["store"].get_run(run.run_id).status is RunStatus.DONE


def test_tick_isolates_a_raising_run(monkeypatch):
    # a single run whose resume() raises must NOT abort the whole tick() due-batch (cross-tenant isolation).
    timers = LocalTimers()
    h = build(model=make_brain(steps=_recurring({"interval_seconds": 100, "max_occurrences": 2})),
              timers=timers)
    h["op"].start(Goal(text="x"))

    def boom(run_id, **kw):
        raise ValueError("poison")

    monkeypatch.setattr(h["op"], "resume", boom)
    timers.advance(101)
    h["cp"].tick()              # must NOT propagate the ValueError


def test_changed_watch_fires_only_on_a_real_change():
    timers = LocalTimers()
    search = FakeSearch(fetches={"http://x": "version one"})
    steps = [{"text": "watch the page", "kind": "monitor",
              "params": {"interval_seconds": 100, "probe": "url", "url": "http://x",
                         "confirm_polls": 1, "match": {"changed": True}}}]
    h = build(model=make_brain(steps=steps), integrations=FakeIntegrations(), timers=timers, search=search)
    run = h["op"].start(Goal(text="watch", max_runtime_s=10 ** 7))
    timers.advance(101)
    h["cp"].tick()                                  # poll 1: establishes the baseline (never fires)
    assert h["store"].get_run(run.run_id).status is RunStatus.WAITING
    search.fetches["http://x"] = "version two — the page CHANGED"
    timers.advance(101)
    h["cp"].tick()                                  # poll 2: changed -> fire
    assert h["store"].get_run(run.run_id).status is RunStatus.DONE


def test_watch_then_act_monitor_triggers_dependent_action():
    # Part III #7: a monitor that fires unblocks a DEPENDENT action step in the SAME durable run; the action
    # still parks for approval and lands a gate-verified effect on "yes" (the trust path is unchanged).
    timers = LocalTimers()
    search = FakeSearch(fetches={"http://shop": "Sold Out"})
    steps = [
        {"text": "watch the page for restock", "kind": "monitor",
         "params": {"interval_seconds": 100, "probe": "url", "url": "http://shop", "confirm_polls": 1,
                    "match": {"absent": "Sold Out"}}},
        {"text": "email me at buyer@acme.com that it is back", "kind": "generic", "depends_on": [0],
         "params": {}},
    ]
    actions = {"email me at buyer": [tc("send_email", to="buyer@acme.com", subject="back in stock")]}
    h = build(model=make_brain(steps=steps, actions=actions), integrations=FakeIntegrations(),
              timers=timers, search=search)
    run = h["op"].start(Goal(text="tell me when it restocks", max_runtime_s=10 ** 7))
    assert run.status is RunStatus.WAITING                 # parked on the watch
    timers.advance(101)
    h["cp"].tick()
    assert h["store"].get_run(run.run_id).status is RunStatus.WAITING   # still sold out -> keep waiting
    search.fetches["http://shop"] = "In stock! Buy now"
    timers.advance(101)
    h["cp"].tick()
    run = h["store"].get_run(run.run_id)
    assert run.status is RunStatus.AWAITING_APPROVAL       # the watch fired -> the dependent send parks
    run = h["cp"].answer(run_id=run.run_id, answer="yes")
    assert run.status is RunStatus.DONE
    effs = [e for e in h["store"].get_effects(run.run_id) if e.label == "gmail:GMAIL_SEND_EMAIL"]
    assert effs and effs[0].phase == "forwarded" and effs[0].expected_present is True


def test_max_runtime_zero_sets_an_immediate_deadline_not_unbounded():
    h = build(model=make_brain(steps=[{"text": "do it"}], actions={"do it": []}))
    run = h["op"].start(Goal(text="x", max_runtime_s=0))
    assert run.deadline_ts is not None              # 0 means "stop ~now", NOT "no time bound" (None)


def test_changed_with_contains_ands_the_filter():
    # contains+changed: a page change that does NOT match `contains` must NOT fire (the filter isn't dropped).
    timers = LocalTimers()
    search = FakeSearch(fetches={"http://x": "version one — tickets available"})
    steps = [{"text": "watch", "kind": "monitor",
              "params": {"interval_seconds": 100, "probe": "url", "url": "http://x", "confirm_polls": 1,
                         "match": {"contains": "tickets available", "changed": True}}}]
    h = build(model=make_brain(steps=steps), integrations=FakeIntegrations(), timers=timers, search=search)
    run = h["op"].start(Goal(text="watch", max_runtime_s=10 ** 7))
    timers.advance(101)
    h["cp"].tick()                                   # baseline
    search.fetches["http://x"] = "version two — SOLD OUT now"   # changed, but no longer 'tickets available'
    timers.advance(101)
    h["cp"].tick()
    assert h["store"].get_run(run.run_id).status is RunStatus.WAITING   # changed but filter fails -> no fire
    search.fetches["http://x"] = "version three — tickets available again"   # changed AND matches
    timers.advance(101)
    h["cp"].tick()
    assert h["store"].get_run(run.run_id).status is RunStatus.DONE      # changed AND contains -> fires


# --------------------------------------------------------------------------- lever 1: model escalation

def test_executor_escalates_to_the_hard_model_on_high_rungs():
    # Rungs 0..(_HARD_RUNG-1) use the cheap "executor"; at rung >= _HARD_RUNG the operator escalates to the
    # stronger "executor_hard" model — horsepower exactly when the cheap approach has already failed. The
    # stronger model still routes through the same broker + read-back gate (verification is unchanged).
    from flowers.engine.operator import _HARD_RUNG
    roles: list[str] = []

    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            return ModelResponse(content=json.dumps({"steps": [{"text": "write the summary"}]}))
        roles.append(role)                                       # an executor turn
        return ModelResponse(content="", finish_reason="stop")   # always refused -> climbs the ladder

    h = build(model=FakeModel(on_complete=fn))
    h["op"].start(Goal(text="summarize"))
    assert roles[:_HARD_RUNG] == ["executor"] * _HARD_RUNG       # the first rungs stay on the cheap model
    assert roles[_HARD_RUNG] == "executor_hard"                  # then the operator escalates the model
    assert set(roles) == {"executor", "executor_hard"}          # only those two, never anything else


# --------------------------------------------------------------------------- lever 2: plan-level replan

def _stepname(user: str) -> str:
    """The CURRENT step's text (the executor blob also lists the whole plan, so match the step line only)."""
    m = re.search(r"YOUR STEP \(\d+\): (.+)", user)
    return m.group(1) if m else user


def test_ladder_exhaustion_replans_the_dag_then_completes():
    # When a step's per-step ladder is spent, the operator RE-ARCHITECTS the remaining plan (a different
    # route) and drives the new plan to DONE — relentlessness at the PLAN level, not just step-level retries.
    state = {"replanned": False}

    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        user = messages[1]["content"] if len(messages) > 1 else ""
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            if "WHY REPLAN" in user:                              # the replan call -> offer a DIFFERENT route
                state["replanned"] = True
                return ModelResponse(content=json.dumps({"steps": [{"text": "do it the OTHER way"}]}))
            return ModelResponse(content=json.dumps({"steps": [{"text": "do it the FIRST way"}]}))
        if "do it the OTHER way" in _stepname(user):             # the alternate route SUCCEEDS
            return ModelResponse(tool_calls=[tc("finish", completed=True,
                                                summary="Accomplished via the alternate route, with content.")],
                                 finish_reason="tool_calls")
        return ModelResponse(content="", finish_reason="stop")   # the first route always refuses -> ladder

    h = build(model=FakeModel(on_complete=fn))
    run = h["op"].start(Goal(text="accomplish the goal"))
    assert state["replanned"] is True
    assert run.status is RunStatus.DONE
    assert h["store"].get_run(run.run_id).dag_replans == 1       # one re-architecture got it done


def test_dag_replans_are_bounded_then_escalate():
    # Every route always refuses, so each re-architecture also fails — bounded by _MAX_REPLANS, then it
    # escalates honestly (never an infinite ladder<->replan loop, never a fabricated done).
    from flowers.engine.operator import _MAX_REPLANS

    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            return ModelResponse(content=json.dumps({"steps": [{"text": "try again"}]}))
        return ModelResponse(content="", finish_reason="stop")   # every route always refuses

    h = build(model=FakeModel(on_complete=fn))
    run = h["op"].start(Goal(text="an impossible task"))
    assert run.status is RunStatus.ESCALATED
    assert h["store"].get_run(run.run_id).dag_replans == _MAX_REPLANS   # capped, did not loop forever


def test_dag_replan_with_no_new_steps_escalates_without_looping():
    # If a replan returns ZERO new steps (the model can't find another route), the no-progress guard stops
    # immediately rather than burning the whole replan budget on nothing.
    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        user = messages[1]["content"] if len(messages) > 1 else ""
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            if "WHY REPLAN" in user:
                return ModelResponse(content=json.dumps({"steps": []}))   # no new steps -> no progress
            return ModelResponse(content=json.dumps({"steps": [{"text": "the only route"}]}))
        return ModelResponse(content="", finish_reason="stop")

    h = build(model=FakeModel(on_complete=fn))
    run = h["op"].start(Goal(text="x"))
    assert run.status is RunStatus.ESCALATED
    assert h["store"].get_run(run.run_id).dag_replans == 1   # tried once, no new work -> stop (not to the cap)


def test_dag_replan_preserves_completed_steps_and_does_not_rerun_them():
    # A 2-step plan: step 0 completes; step 1's ladder exhausts -> replan. The COMPLETED step 0 must be
    # preserved (DONE) and NOT re-run; only the remaining work is re-architected.
    ran = {"step0": 0, "alt": 0}

    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        user = messages[1]["content"] if len(messages) > 1 else ""
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            if "WHY REPLAN" in user:
                return ModelResponse(content=json.dumps({"steps": [{"text": "finish via alternate"}]}))
            return ModelResponse(content=json.dumps(
                {"steps": [{"text": "step zero work"}, {"text": "step one work"}]}))
        name = _stepname(user)
        if "step zero work" in name:
            ran["step0"] += 1
            return ModelResponse(tool_calls=[tc("finish", completed=True,
                                                summary="step zero deliverable content")],
                                 finish_reason="tool_calls")
        if "finish via alternate" in name:
            ran["alt"] += 1
            return ModelResponse(tool_calls=[tc("finish", completed=True,
                                                summary="alternate deliverable content")],
                                 finish_reason="tool_calls")
        return ModelResponse(content="", finish_reason="stop")   # "step one work" refuses -> ladder -> replan

    h = build(model=FakeModel(on_complete=fn))
    run = h["op"].start(Goal(text="a two-part goal"))
    assert run.status is RunStatus.DONE
    assert ran["step0"] == 1            # the completed step ran exactly ONCE (not re-run after the replan)
    assert ran["alt"] == 1             # the re-architected remaining step completed
    assert h["store"].get_run(run.run_id).dag_replans == 1


def test_dag_replans_round_trips_field_explicitly():
    # The new counter must survive the field-explicit serializers (or it silently resets on restart and the
    # bound is lost); an old row written before the field existed must load as 0.
    from flowers.seams.store import _run_from_dict, _run_to_dict
    from flowers.types import RunState

    run = RunState(run_id="r", tenant_id="t", goal_text="g", budget_usd=1.0, dag_replans=3)
    d = _run_to_dict(run)
    assert d["dag_replans"] == 3
    assert _run_from_dict(d).dag_replans == 3
    d.pop("dag_replans")                            # an OLD row (pre-field)
    assert _run_from_dict(d).dag_replans == 0       # loads cleanly with the default

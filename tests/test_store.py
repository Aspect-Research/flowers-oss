"""Offline tests for the Store seam (SqliteStore).

Pure persistence seam — no network, no ``available()``. These tests pin down round-trip
fidelity (dataclasses + enums survive a write/read cycle), append ordering for effects,
approval resolution, stable tenant identity, usage summing, and persistence across reopen.
"""

from __future__ import annotations

from flowers.seams.store import SqliteStore
from flowers.types import (
    ApprovalRequest,
    EffectRecord,
    Plan,
    PlanStep,
    RunState,
    RunStatus,
    StepKind,
    StepStatus,
)


def _store() -> SqliteStore:
    return SqliteStore()  # :memory:


def test_run_round_trip_with_enum_status():
    s = _store()
    run = RunState(
        run_id="run_1",
        tenant_id="ten_1",
        goal_text="book a table",
        budget_usd=5.0,
        status=RunStatus.PENDING,
        replans=2,
        spent_usd=1.25,
    )
    s.create_run(run)

    got = s.get_run("run_1")
    assert got is not None
    assert got.run_id == "run_1"
    assert got.tenant_id == "ten_1"
    assert got.goal_text == "book a table"
    assert got.budget_usd == 5.0
    assert got.status is RunStatus.PENDING
    assert got.replans == 2
    assert got.spent_usd == 1.25

    # save_run updates in place, status enum preserved.
    run.status = RunStatus.RUNNING
    run.spent_usd = 2.5
    s.save_run(run)
    got2 = s.get_run("run_1")
    assert got2.status is RunStatus.RUNNING
    assert got2.spent_usd == 2.5


def test_unknown_step_kind_degrades_to_generic():
    # a plan persisted by NEWER code with a StepKind this build doesn't know must still LOAD (degrade to
    # GENERIC), not raise — mirroring the planner's _coerce_kind (Part II forward/back-compat hardening).
    from flowers.seams.store import _step_from_dict
    s = _step_from_dict({"index": 0, "text": "future step", "kind": "some_future_kind"})
    assert s.kind is StepKind.GENERIC
    assert _step_from_dict({"index": 0, "text": "x", "kind": "recurring"}).kind is StepKind.RECURRING


def test_mandate_fields_round_trip(tmp_path):
    """The Mandate's RunState.mandate / mandate_counts and Plan.mandate must survive serialize +
    rehydrate AND a process restart — the field-explicit serializer trap (invariant I7)."""
    db = str(tmp_path / "mandate.db")
    s1 = SqliteStore(db)
    mandate = {"action_types": ["gmail:GMAIL_SEND_EMAIL"], "recipient_scope": ["@acme.com"],
               "magnitude_caps": {"max_sends": 5, "per_domain": 3, "per_recipient": 2},
               "irreversibility_ceiling": "ASK", "done_definition": "all emailed"}
    counts = {"sends_total": 2, "by_domain": {"acme.com": 2}, "by_recipient": {"a@acme.com": 2},
              "sent_digests": ["deadbeef"]}
    run = RunState(run_id="run_m", tenant_id="t", goal_text="g", budget_usd=2.0,
                   mandate=mandate, mandate_counts=counts, deadline_ts=1782233000.5)
    s1.create_run(run)
    s1.save_plan("run_m", Plan(steps=[PlanStep(index=0, text="s")], goal_text="g", mandate=mandate))
    s1.close()

    s2 = SqliteStore(db)
    got = s2.get_run("run_m")
    assert got.mandate == mandate
    assert got.mandate_counts == counts
    assert got.deadline_ts == 1782233000.5          # the wall-clock deadline round-trips too (Part II)
    assert s2.get_plan("run_m").mandate == mandate
    # a pre-mandate row (no fields) rehydrates to empty defaults, never a KeyError.
    s2.create_run(RunState(run_id="run_old", tenant_id="t", goal_text="g", budget_usd=1.0))
    old = s2.get_run("run_old")
    assert old.mandate == {} and old.mandate_counts == {}
    s2.close()


def test_get_run_missing_returns_none():
    s = _store()
    assert s.get_run("nope") is None


def test_run_with_pending_approval_round_trip():
    s = _store()
    apr = ApprovalRequest(run_id="run_x", kind="side_effect", prompt="Send email?", tier="ask")
    run = RunState(
        run_id="run_x",
        tenant_id="ten_x",
        goal_text="g",
        budget_usd=1.0,
        status=RunStatus.AWAITING_APPROVAL,
        pending_approval=apr,
    )
    s.create_run(run)
    got = s.get_run("run_x")
    assert got.pending_approval is not None
    assert got.pending_approval.kind == "side_effect"
    assert got.pending_approval.tier == "ask"
    assert got.pending_approval.id == apr.id


def test_list_runs_by_tenant():
    s = _store()
    s.create_run(RunState(run_id="r1", tenant_id="A", goal_text="g1", budget_usd=1.0))
    s.create_run(RunState(run_id="r2", tenant_id="A", goal_text="g2", budget_usd=1.0))
    s.create_run(RunState(run_id="r3", tenant_id="B", goal_text="g3", budget_usd=1.0))
    a = s.list_runs("A")
    assert {r.run_id for r in a} == {"r1", "r2"}
    assert all(isinstance(r, RunState) for r in a)
    assert [r.run_id for r in s.list_runs("B")] == ["r3"]
    assert s.list_runs("missing") == []


def test_plan_round_trip_three_steps():
    s = _store()
    steps = [
        PlanStep(index=0, text="search", kind=StepKind.GENERIC, status=StepStatus.DONE),
        PlanStep(
            index=1,
            text="await replies",
            kind=StepKind.AWAIT_REPLIES,
            depends_on=[0],
            needs=["gmail"],
            params={"k": 3, "wait": 86400},
            status=StepStatus.WAITING,
        ),
        PlanStep(
            index=2,
            text="monitor inbox",
            kind=StepKind.MONITOR,
            depends_on=[0, 1],
            done_criteria=[{"check": "reply_count", "min": 1}],
            status=StepStatus.PENDING,
        ),
    ]
    plan = Plan(steps=steps, goal_text="outreach")
    s.save_plan("run_p", plan)

    got = s.get_plan("run_p")
    assert got is not None
    assert got.goal_text == "outreach"
    assert len(got.steps) == 3

    assert got.steps[0].kind is StepKind.GENERIC
    assert got.steps[0].status is StepStatus.DONE

    assert got.steps[1].kind is StepKind.AWAIT_REPLIES
    assert got.steps[1].status is StepStatus.WAITING
    assert got.steps[1].depends_on == [0]
    assert got.steps[1].needs == ["gmail"]
    assert got.steps[1].params == {"k": 3, "wait": 86400}

    assert got.steps[2].kind is StepKind.MONITOR
    assert got.steps[2].status is StepStatus.PENDING
    assert got.steps[2].depends_on == [0, 1]
    assert got.steps[2].done_criteria == [{"check": "reply_count", "min": 1}]


def test_get_plan_missing_returns_none():
    s = _store()
    assert s.get_plan("nope") is None


def test_effects_append_order_preserved():
    s = _store()
    e1 = EffectRecord(toolkit="gmail", action="GMAIL_SEND_EMAIL", phase="forwarded", label="first")
    e2 = EffectRecord(
        toolkit="calendar",
        action="CAL_CREATE_EVENT",
        side_effecting=True,
        drift_present=True,
        expected_present=True,
        label="second",
        detail={"k": "v"},
    )
    s.append_effect("run_e", e1)
    s.append_effect("run_e", e2)

    got = s.get_effects("run_e")
    assert [e.label for e in got] == ["first", "second"]
    assert got[0].toolkit == "gmail"
    assert got[0].phase == "forwarded"
    assert got[1].side_effecting is True
    assert got[1].drift_present is True
    assert got[1].expected_present is True
    assert got[1].detail == {"k": "v"}
    assert got[1].action_id == e2.action_id

    assert s.get_effects("other") == []


def test_approval_save_resolve_answer():
    s = _store()
    apr = ApprovalRequest(
        run_id="run_a",
        kind="clarify",
        prompt="Which city?",
        options=["SF", "NYC"],
    )
    s.save_approval(apr)

    got = s.get_approval(apr.id)
    assert got is not None
    assert got.kind == "clarify"
    assert got.prompt == "Which city?"
    assert got.options == ["SF", "NYC"]

    # Unresolved -> no answer yet.
    assert s.get_answer(apr.id) is None

    s.resolve_approval(apr.id, "SF")
    assert s.get_answer(apr.id) == "SF"

    assert s.get_approval("missing") is None
    assert s.get_answer("missing") is None


def test_usage_spend_sums():
    s = _store()
    s.record_usage(tenant_id="T", run_id="R1", kind="model", cost_usd=0.10, detail={"role": "planner"})
    s.record_usage(tenant_id="T", run_id="R1", kind="search", cost_usd=0.05, detail={})
    s.record_usage(tenant_id="T", run_id="R2", kind="model", cost_usd=0.20, detail={})

    assert abs(s.run_spend("R1") - 0.15) < 1e-9
    assert abs(s.run_spend("R2") - 0.20) < 1e-9

    # No usage recorded -> 0.0, not an error.
    assert s.run_spend("nope") == 0.0


def test_persistence_across_reopen(tmp_path):
    db = str(tmp_path / "store.db")

    s1 = SqliteStore(db)
    run = RunState(
        run_id="run_persist",
        tenant_id="ten_persist",
        goal_text="survive restart",
        budget_usd=3.0,
        status=RunStatus.RUNNING,
    )
    s1.create_run(run)
    plan = Plan(
        steps=[PlanStep(index=0, text="step one", kind=StepKind.GENERIC, status=StepStatus.RUNNING)],
        goal_text="survive restart",
    )
    s1.save_plan("run_persist", plan)
    s1.close()

    # Brand new store object on the SAME path -> data still there.
    s2 = SqliteStore(db)
    got_run = s2.get_run("run_persist")
    assert got_run is not None
    assert got_run.goal_text == "survive restart"
    assert got_run.status is RunStatus.RUNNING

    got_plan = s2.get_plan("run_persist")
    assert got_plan is not None
    assert len(got_plan.steps) == 1
    assert got_plan.steps[0].kind is StepKind.GENERIC
    assert got_plan.steps[0].status is StepStatus.RUNNING
    s2.close()


def test_step_result_round_trip():
    s = _store()
    from flowers.types import StepResult

    eff = EffectRecord(toolkit="fs", action="WRITE", phase="forwarded")
    step = PlanStep(
        index=0,
        text="do",
        status=StepStatus.DONE,
        result=StepResult(
            claimed_done=True,
            ok=True,
            text="finished",
            effects=[eff],
            events=[{"kind": "write"}],
            signals={"replan": False},
            searches=2,
            tool_failures=1,
        ),
    )
    s.save_plan("run_sr", Plan(steps=[step], goal_text="g"))
    got = s.get_plan("run_sr")
    r = got.steps[0].result
    assert r is not None
    assert r.claimed_done is True
    assert r.text == "finished"
    assert len(r.effects) == 1
    assert r.effects[0].toolkit == "fs"
    assert r.events == [{"kind": "write"}]
    assert r.signals == {"replan": False}
    assert r.searches == 2
    assert r.tool_failures == 1

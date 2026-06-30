"""Core data contracts — the EffectRecord gate-dict and the Plan DAG helpers."""

from __future__ import annotations

from flowers import trustgate as g
from flowers.types import (
    EffectRecord,
    Plan,
    PlanStep,
    StepKind,
    StepStatus,
)


def test_effectrecord_gate_dict_omits_unset_guard_fields():
    rec = EffectRecord(toolkit="gmail", action="GMAIL_SEND_EMAIL", side_effecting=True,
                       phase="forwarded", expected_present=True)
    d = rec.as_gate_dict()
    assert d["toolkit"] == "gmail" and d["expected_present"] is True
    # the optional self-report-guard fields are omitted when unset, so the gate stays byte-identical
    assert "verification" not in d and "observer" not in d and "actor" not in d


def test_effectrecord_flows_through_gate_verified():
    rec = EffectRecord(toolkit="gmail", action="GMAIL_SEND_EMAIL", side_effecting=True,
                       phase="forwarded", expected_present=True)
    unver, unverifiable = g.classify_effects([rec.as_gate_dict()], claimed_done=True)
    assert unver == [] and unverifiable == []


def test_effectrecord_fabricated_is_refused_through_gate():
    # The production-path shape: a claimed send whose read-back showed no such message.
    rec = EffectRecord(toolkit="gmail", action="GMAIL_SEND_EMAIL", side_effecting=True,
                       phase="forwarded", expected_present=False)
    unver, _ = g.classify_effects([rec.as_gate_dict()], claimed_done=True)
    accept, reason = g.gate_verdict(claimed_done=True, ok=True, stale_files=[], gate_breaking=[],
                                    unverified_external=unver)
    assert accept is False and "not reflected" in reason


def test_browser_self_report_carries_guard_fields():
    rec = EffectRecord(toolkit="browser", action="book_table", effect_kind="cua",
                       side_effecting=True, phase="forwarded", expected_present=True,
                       verification="screenshot", observer="agent", actor="agent")
    d = rec.as_gate_dict()
    assert d["verification"] == "screenshot" and d["observer"] == "agent"
    _, unverifiable = g.classify_effects([d], claimed_done=True)
    assert unverifiable == ["browser:book_table"]


def test_plan_ready_indices_respects_deps():
    steps = [
        PlanStep(index=0, text="search venues", kind=StepKind.GENERIC),
        PlanStep(index=1, text="email batch", depends_on=[0]),
        PlanStep(index=2, text="await replies", kind=StepKind.AWAIT_REPLIES, depends_on=[1]),
    ]
    plan = Plan(steps=steps, goal_text="organize the venue")
    assert plan.ready_indices() == [0]              # only step 0 is ready
    steps[0].status = StepStatus.DONE
    assert plan.ready_indices() == [1]              # 1 unblocks
    steps[1].status = StepStatus.DONE
    assert plan.ready_indices() == [2]
    steps[2].status = StepStatus.DONE
    assert plan.is_complete() is True


def test_plan_incomplete_when_empty():
    assert Plan(steps=[], goal_text="x").is_complete() is False

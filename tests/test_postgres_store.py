"""Live PostgresStore contract test. SKIPPED unless FLOWERS_DATABASE_URL (or DBOS_DATABASE_URL)
is set — so the default offline suite stays $0/no-network. Run live:

    FLOWERS_DATABASE_URL=postgresql://... py -3 -m pytest tests/test_postgres_store.py -v

Proves the Postgres adapter round-trips EVERY Store method with full typed fidelity (the same property
SqliteStore is held to) + the durable continuation, against real Neon Postgres.
"""

from __future__ import annotations

import os
import uuid

import pytest

from flowers.types import (
    ApprovalRequest,
    EffectRecord,
    Plan,
    PlanStep,
    RunState,
    RunStatus,
    StepResult,
)

_DSN = os.environ.get("FLOWERS_DATABASE_URL") or os.environ.get("DBOS_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not _DSN, reason="set FLOWERS_DATABASE_URL (or DBOS_DATABASE_URL) to run the live Postgres test")


def _uid(p: str) -> str:
    return f"{p}_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def store():
    from flowers.extras.store import PostgresStore
    s = PostgresStore(_DSN)
    yield s
    s.close()


def test_runs_plans_effects_approvals_usage_round_trip(store):
    tenant = _uid("ten")
    run = RunState(run_id=_uid("run"), tenant_id=tenant, goal_text="organize the offsite",
                   budget_usd=5.0, status=RunStatus.RUNNING)
    store.create_run(run)
    assert store.get_run(run.run_id) == run            # full typed fidelity

    run.spent_usd = 1.23
    run.status = RunStatus.AWAITING_APPROVAL
    store.save_run(run)                                # upsert
    assert store.get_run(run.run_id) == run
    assert any(r.run_id == run.run_id for r in store.list_runs(tenant))

    eff_a = EffectRecord(toolkit="gmail", action="GMAIL_SEND_EMAIL", side_effecting=True,
                         phase="forwarded", expected_present=True, label="gmail:GMAIL_SEND_EMAIL")
    eff_b = EffectRecord(toolkit="browser", action="submit", side_effecting=True, phase="forwarded",
                         effect_kind="cua", observer="o", actor="a", expected_present=True)
    step = PlanStep(index=0, text="email + book",
                    result=StepResult(claimed_done=True, ok=True, text="done", effects=[eff_a]))
    plan = Plan(steps=[step], goal_text=run.goal_text)
    store.save_plan(run.run_id, plan)
    assert store.get_plan(run.run_id) == plan

    store.append_effect(run.run_id, eff_a)
    store.append_effect(run.run_id, eff_b)
    assert store.get_effects(run.run_id) == [eff_a, eff_b]   # append order preserved

    appr = ApprovalRequest(run_id=run.run_id, kind="side_effect", prompt="Authorize?",
                           options=["yes", "no"], tier="ask", effect_label="browser:submit")
    store.save_approval(appr)
    assert store.get_approval(appr.id) == appr
    assert store.get_answer(appr.id) is None
    store.resolve_approval(appr.id, "yes")
    assert store.get_answer(appr.id) == "yes"
    store.save_approval(appr)                          # re-save must NOT clobber the recorded answer
    assert store.get_answer(appr.id) == "yes"

    store.record_usage(tenant_id=tenant, run_id=run.run_id, kind="model", cost_usd=0.50, detail={"role": "planner"})
    store.record_usage(tenant_id=tenant, run_id=run.run_id, kind="model", cost_usd=0.25, detail={})
    assert abs(store.run_spend(run.run_id) - 0.75) < 1e-9


def test_continuation_round_trips(store):
    rid = _uid("run")
    assert store.get_continuation(rid) is None
    cont = {"grants": ["browser:submit|abc123"], "pending_grant": None,
            "resume_state": {"messages": [{"role": "user", "content": "x"}], "searches": 1,
                             "pending": {"toolkit": "browser", "action": "submit", "params": {"ref": "BK-1"}}}}
    store.save_continuation(rid, cont)
    assert store.get_continuation(rid) == cont
    store.save_continuation(rid, {})                   # upsert to empty
    assert store.get_continuation(rid) == {}

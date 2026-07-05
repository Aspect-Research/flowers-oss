"""The no-double-send invariant — a VERIFIED side-effect is NEVER re-sent.

A run-scoped idempotency guard makes "never re-send a verified side-effect" mechanical across retries,
per-step ladder climbs, plan-level replans, and process restarts. The broker keys on the EXACT-action
grant_key: once a byte-identical action VERIFIED-landed, a re-issue is short-circuited as already-done —
NOT re-executed and NOT re-prompted — closing the replan/ladder/cached-grant duplicate-send hole.

These ride the same real read-back harness as test_broker.py (a FakeIntegrations whose read-back makes a
send verifiably land) plus the real engine (`_harness.build`).
"""

from __future__ import annotations

from _harness import build, make_brain, tc

from flowers.broker import Broker
from flowers.seams.browser import FakeBrowser
from flowers.seams.integrations import FakeIntegrations
from flowers.types import EffectRecord, Goal, Plan, PlanStep, RunState, RunStatus, StepStatus

_SEND = {"to": "chef@bistro.com", "subject": "table?", "body": "hi"}


# --------------------------------------------------------------------------- broker-level

def test_seeded_grant_key_short_circuits_a_resend_without_re_executing():
    """The cross-step path: the operator seeds the broker with grant_keys already VERIFIED-landed in prior
    steps. A byte-identical action whose gk is seeded is short-circuited (idempotent replay) — the backend
    is never touched, so no duplicate goes out, and no approval is re-prompted."""
    fake = FakeIntegrations()
    gk = Broker(integrations=fake, run_id="r1").grant_key_for("gmail", "GMAIL_SEND_EMAIL", _SEND)
    b = Broker(integrations=fake, run_id="r1", forwarded_gks={gk})
    res = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL", params=dict(_SEND),
                             user_id="u1", grants={gk})            # even WITH the cached grant
    assert res.status == "ok"
    assert res.effect.phase == "forwarded" and res.effect.detail.get("idempotent_replay") is True
    assert res.data.get("idempotent") is True
    assert len(fake.surface("u1", "sent")) == 0                    # NOTHING was sent — pure replay


def test_within_loop_identical_send_is_deduped_to_one_delivery():
    """The within-step path: a model that issues the SAME verified send twice in one loop delivers once.
    The first forwards+verifies (its gk enters the set); the second is the idempotent replay."""
    fake = FakeIntegrations()
    b = Broker(integrations=fake, run_id="r1")
    r1 = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL", params=dict(_SEND),
                            user_id="u1", authorized=True)
    r2 = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL", params=dict(_SEND),
                            user_id="u1", authorized=True)
    assert r1.status == "ok" and r1.effect.expected_present is True and not r1.effect.detail.get("idempotent_replay")
    assert r2.status == "ok" and r2.effect.detail.get("idempotent_replay") is True
    assert len(fake.surface("u1", "sent")) == 1                    # exactly ONE delivery


def test_idempotency_ignores_read_only_and_different_params():
    """The guard binds to the EXACT action (toolkit:action + full-params digest) and only side-effects: a
    read is never deduped, and a send with ANY different field is a different action (its own gk)."""
    fake = FakeIntegrations()
    gk = Broker(integrations=fake, run_id="r1").grant_key_for("gmail", "GMAIL_SEND_EMAIL", _SEND)
    b = Broker(integrations=fake, run_id="r1", forwarded_gks={gk})
    # a read-only fetch is never short-circuited (re-reading is free + correct)
    assert b.call_integration(toolkit="gmail", action="GMAIL_FETCH_EMAILS", params={},
                              user_id="u1").status == "ok"
    # a send with a DIFFERENT subject is a different action -> not deduped (it must still go through)
    other = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                               params={**_SEND, "subject": "different"}, user_id="u1", authorized=True)
    assert other.status == "ok" and not other.effect.detail.get("idempotent_replay")
    assert len(fake.surface("u1", "sent")) == 1                    # the genuinely-different send landed


def test_browser_identical_side_effect_is_deduped():
    """The invariant covers the browser/cua path too: a re-issued identical submit/book is short-circuited."""
    b = Broker(browser=FakeBrowser(), run_id="r1")
    p = {"ref": "BK-1", "target": "venue.com"}
    r1 = b.call_browser(action="submit", params=dict(p), user_id="u1", authorized=True)
    r2 = b.call_browser(action="submit", params=dict(p), user_id="u1", authorized=True)
    assert r1.effect.expected_present is True and not r1.effect.detail.get("idempotent_replay")
    assert r2.effect.detail.get("idempotent_replay") is True and r2.effect.effect_kind == "cua"


# --------------------------------------------------------------------------- effect_landed is per-step (#3)

def test_effect_landed_is_scoped_to_the_producing_step():
    """A replanned/later step that declares the same `produces` label must NOT false-pass on an EARLIER
    step's verified effect: the gate's verified-effects set is scoped to the step being gated. Run-wide
    (no step) keeps the legacy view for back-compat."""
    h = build(model=make_brain())
    op, store = h["op"], h["store"]
    run = RunState(run_id="r1", goal_text="x", budget_usd=2.0)
    store.create_run(run)
    store.append_effect("r1", EffectRecord(
        toolkit="gmail", action="GMAIL_SEND_EMAIL", side_effecting=True, phase="forwarded",
        expected_present=True, label="gmail:GMAIL_SEND_EMAIL", detail={"step_index": 0}))
    sandbox = op._sandbox("r1")
    b0 = op._bundle(run, sandbox, PlanStep(index=0, text="send"))
    b1 = op._bundle(run, sandbox, PlanStep(index=1, text="confirm"))
    assert "gmail:GMAIL_SEND_EMAIL" in b0["verified_effects"]          # step 0 OWNS the effect
    assert "gmail:GMAIL_SEND_EMAIL" not in b1["verified_effects"]      # step 1 must NOT inherit it
    assert "gmail:GMAIL_SEND_EMAIL" in op._bundle(run, sandbox)["verified_effects"]   # run-wide back-compat


# --------------------------------------------------------------------------- end-to-end (the blocker repro)

def test_second_step_reissuing_an_approved_send_is_not_duplicated():
    """The headline duplicate-send blocker, refuted end-to-end: the owner approves a send ONCE (step 0);
    a later step re-issues the byte-identical send. WITHOUT the guard the cached grant would silently
    re-execute it (two emails from one approval); WITH it, step 1 is an idempotent replay — one delivery."""
    integ = FakeIntegrations()
    steps = [
        {"text": "email the chef once"},
        {"text": "email the chef again (duplicate)", "depends_on": [0]},
    ]
    actions = {"email the chef once": [tc("send_email", **_SEND)],
               "email the chef again (duplicate)": [tc("send_email", **_SEND)]}
    h = build(model=make_brain(steps=steps, actions=actions), integrations=integ)
    run = h["op"].start(Goal(text="email the chef"))
    assert run.status is RunStatus.AWAITING_APPROVAL                   # step 0 parks for the ONE approval
    run = h["cp"].answer(run_id=run.run_id, answer="yes")
    assert run.status is RunStatus.DONE
    assert len(integ.surface("local", "sent")) == 1                   # ONE delivery from ONE approval
    sends = [e for e in h["store"].get_effects(run.run_id) if e.label == "gmail:GMAIL_SEND_EMAIL"]
    assert sum(1 for e in sends if e.detail.get("idempotent_replay")) == 1   # step 1 was a replay, not a send


# --------------------------------------------------------------------------- crash recovery (#5)

def test_recover_stalled_redrives_a_crashed_running_run():
    """A run left RUNNING with a RUNNING step (the process died mid-step) has no timer to wake it. The
    startup sweep re-drives it from its persisted plan to completion."""
    from flowers.seams.store import SqliteStore
    store = SqliteStore()
    steps = [{"text": "do the thing"}]
    actions = {"do the thing": []}                                     # a no-tool step that just finishes
    # Simulate a crash: a persisted run + plan left RUNNING, its step RUNNING, no timer scheduled.
    run = RunState(run_id="r1", goal_text="x", budget_usd=2.0, status=RunStatus.RUNNING)
    store.create_run(run)
    store.save_plan("r1", Plan(steps=[PlanStep(index=0, text="do the thing", status=StepStatus.RUNNING)],
                               goal_text="x"))
    h = build(model=make_brain(steps=steps, actions=actions), store=store)   # fresh operator, cold caches
    recovered = h["cp"].recover_stalled()
    assert any(r is not None and r.run_id == "r1" for r in recovered)
    assert store.get_run("r1").status is RunStatus.DONE

    # A second sweep is a no-op (no RUNNING runs remain) — recovery is idempotent.
    assert h["cp"].recover_stalled() == []


def test_recover_stalled_escalates_a_planning_crash():
    """A process that died DURING planning leaves a run in status PLANNING with no plan and no timer — also a
    silent orphan. The sweep covers this synchronous pre-drive window: nothing was driven (no effects), so it
    surfaces honestly (ESCALATED) instead of hanging forever."""
    from flowers.seams.store import SqliteStore
    store = SqliteStore()
    run = RunState(run_id="r1", goal_text="x", budget_usd=2.0, status=RunStatus.PLANNING)
    store.create_run(run)                                              # PLANNING, NO plan saved (crash mid-plan)
    h = build(model=make_brain(), store=store)
    recovered = h["cp"].recover_stalled()
    assert any(r is not None and r.run_id == "r1" for r in recovered)
    assert store.get_run("r1").status is RunStatus.ESCALATED

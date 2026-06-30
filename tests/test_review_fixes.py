"""Regressions for the adversarial-review findings (flowers-review). Each test names its finding so a
future change that reopens the hole fails loudly.
"""

from __future__ import annotations

import os
import shutil
import tempfile

from _harness import build, make_brain, tc

from flowers import policy
from flowers import trustgate as g
from flowers.broker import Broker
from flowers.engine.operator import Operator
from flowers.seams.integrations import FakeIntegrations
from flowers.seams.model import FakeModel
from flowers.seams.search import FakeSearch
from flowers.seams.store import SqliteStore
from flowers.seams.timers import LocalTimers
from flowers.types import Goal, RunStatus

# --- Finding 1: the gate is the single source of truth for "what landed" ---

def test_finding1_verified_effects_excludes_unverified_and_non_side_effecting():
    recs = [
        {"toolkit": "gmail", "action": "GMAIL_SEND_EMAIL", "side_effecting": False, "phase": "forwarded"},
        {"toolkit": "gmail", "action": "GMAIL_TRASH_MESSAGE", "side_effecting": True,
         "phase": "forwarded", "expected_present": False},
        {"toolkit": "googlecalendar", "action": "GOOGLECALENDAR_CREATE_EVENT", "side_effecting": True,
         "phase": "forwarded", "expected_present": True},
    ]
    assert g.verified_effects(recs) == ["googlecalendar:GOOGLECALENDAR_CREATE_EVENT"]


def test_finding1_unverified_send_cannot_satisfy_effect_landed():
    # A send that executes but does NOT land (provider accepted it, nothing appeared) is NOT verified,
    # so it can't satisfy an effect_landed criterion -> the run escalates, never DONE. (The gmail=auto
    # override only WAIVES approval; per P3-review fix B it does NOT waive the read-back verification.)
    steps = [{"text": "email the venue", "done_criteria": [
        {"id": "sent", "objective_check": {"kind": "effect_landed",
                                           "params": {"label": "gmail:GMAIL_SEND_EMAIL"}}}]}]
    actions = {"email the venue": [tc("send_email", to="bob@acme.com", subject="Hi")]}
    op = Operator(store=SqliteStore(), model=make_brain(steps=steps, actions=actions),
                  search=FakeSearch(),
                  integrations=FakeIntegrations(drop_actions=[("gmail", "GMAIL_SEND_EMAIL")]),
                  timers=LocalTimers(), overrides={"gmail": policy.AUTO})
    run = op.start(Goal(text="email the venue"))
    assert run.status is RunStatus.ESCALATED          # a non-verified effect can't satisfy effect_landed
    assert run.status is not RunStatus.DONE


# --- Finding 2: an await/monitor with an empty match verifies nothing (fail closed) ---

def test_finding2_empty_match_does_not_complete_on_existing_mail():
    integ = FakeIntegrations()
    integ.deliver_inbound("local", sender="anyone@x.com", subject="hello", body="hi")  # pre-existing
    steps = [{"text": "watch for a reply", "kind": "await_replies",
              "params": {"window_seconds": 3600, "min_replies": 1, "match": {}}},
             {"text": "note it", "depends_on": [0]}]
    h = build(model=make_brain(steps=steps, actions={"note it": [tc("write_file", path="n.md", content="x")]}),
              integrations=integ)
    run = h["op"].start(Goal(text="watch"))
    assert run.status is RunStatus.WAITING
    assert h["cp"].deliver(run_id=run.run_id).status is RunStatus.WAITING   # empty match -> stays waiting


# --- Finding 3: a grant is bound to the action fingerprint, not the bare toolkit:action label ---

def test_finding3_grant_is_fingerprint_bound():
    b = Broker(integrations=FakeIntegrations(), run_id="r")
    gk_bob = b.grant_key_for("gmail", "GMAIL_SEND_EMAIL", {"to": "bob@acme.com", "subject": "Hi"})
    to_alice = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                                  params={"to": "alice@acme.com", "subject": "Hi"},
                                  user_id="u", grants={gk_bob})
    assert to_alice.status == "needs_approval"        # bob's grant does NOT authorize a send to alice
    to_bob = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                                params={"to": "bob@acme.com", "subject": "Hi"},
                                user_id="u", grants={gk_bob})
    assert to_bob.status == "ok" and to_bob.effect.expected_present is True


# --- Finding 4: a monitor escalates after the poll cap instead of polling forever ---

def test_finding4_monitor_escalates_after_max_polls():
    # a plan-set max_polls bounds a watch that never matches -> escalate (the default is now relentless to
    # the hard cap / deadline; an explicit max_polls is the per-watch bound).
    timers = LocalTimers()
    steps = [{"text": "watch for the bank email", "kind": "monitor",
              "params": {"interval_seconds": 100, "max_polls": 3,
                         "match": {"from": "bank@chase.com"}, "notify": "x"}}]
    h = build(model=make_brain(steps=steps), integrations=FakeIntegrations(), timers=timers)
    run = h["op"].start(Goal(text="watch the bank email"))
    assert run.status is RunStatus.WAITING
    for _ in range(6):
        timers.advance(101)
        h["cp"].tick()
        if h["store"].get_run(run.run_id).status is RunStatus.ESCALATED:
            break
    assert h["store"].get_run(run.run_id).status is RunStatus.ESCALATED


# --- Finding 5: the per-run sandbox workdir is stable across operator instances (survives restart) ---

def test_finding5_sandbox_workdir_is_stable_across_instances():
    rid = "run_stableworkdir_regression"
    path = os.path.join(tempfile.gettempdir(), f"flowers-sbx-{rid}")
    shutil.rmtree(path, ignore_errors=True)
    try:
        op1 = Operator(store=SqliteStore(), model=FakeModel([]), search=FakeSearch(),
                       integrations=FakeIntegrations(), timers=LocalTimers())
        op1._sandbox(rid).write_file("a.md", "persist me")
        op2 = Operator(store=SqliteStore(), model=FakeModel([]), search=FakeSearch(),
                       integrations=FakeIntegrations(), timers=LocalTimers())
        assert op2._sandbox(rid).read_file("a.md") == "persist me"
    finally:
        shutil.rmtree(path, ignore_errors=True)


# --- Finding 6: missing await params must not crash ---

def test_finding6_await_with_missing_params_does_not_crash():
    integ = FakeIntegrations()
    steps = [{"text": "watch", "kind": "await_replies", "params": {"match": {"from": "venue@hall.com"}}}]
    h = build(model=make_brain(steps=steps), integrations=integ)
    run = h["op"].start(Goal(text="watch"))   # no window_seconds, no min_replies
    assert run.status is RunStatus.WAITING
    integ.deliver_inbound("local", sender="venue@hall.com", subject="re", body="ok")
    assert h["cp"].deliver(run_id=run.run_id).status is RunStatus.DONE


# --- Finding 7 (live review): an honest "I could not do it" must escalate, never be accepted as done ---

def test_finding7_honest_failure_escalates_not_accepted():
    # The executor finishes with completed=False (a weak/blocked model being honest). With no effect to
    # refute and no done-criteria, the OLD code accepted it as done. Now it must escalate.
    model = make_brain(steps=[{"text": "do the impossible thing"}],
                       actions={"do the impossible thing": [
                           tc("finish", completed=False, summary="I was unable to do this")]})
    h = build(model=model)
    run = h["op"].start(Goal(text="do the impossible thing"))
    assert run.status is RunStatus.ESCALATED
    assert run.status is not RunStatus.DONE

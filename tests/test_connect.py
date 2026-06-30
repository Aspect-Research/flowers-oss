"""The end-user OAuth connect round-trip.

A step that needs the user's own Gmail/Calendar, when that account is not yet connected, must NOT die as
a tool failure: the broker surfaces ``needs_auth`` (a consent URL + the exact pending action), the operator
parks the run (AWAITING_CONNECT), texts a tappable connect link, polls for the grant on the durable tick,
and — once connected — resumes-at-action EXACTLY (deterministic, once), never silently authorizing and
never silently quitting. Money/illegal REFUSE stays strictly above this (a refused action never connects).
All offline ($0): FakeIntegrations(unauthorized=...) models the unconnected account + the grant landing.
"""

from __future__ import annotations

from _harness import build, make_brain, tc

from flowers.broker import Broker
from flowers.engine.operator import _CONNECT_POLL_S
from flowers.seams.integrations import FakeIntegrations
from flowers.types import Goal, RunStatus

# --- C1: the authorize seam (offline model) -----------------------------------------------------

def test_fake_authorize_pending_then_completed():
    fi = FakeIntegrations(unauthorized={"gmail"})
    status, url = fi.authorize("gmail", "u1")
    assert status == "pending" and "connect.arcade.test/gmail" in url
    assert fi.authorize("googlecalendar", "u1") == ("completed", "")   # already connected
    fi.grant("gmail")                                                  # the user completes the flow
    assert fi.authorize("gmail", "u1") == ("completed", "")


# --- C2: the broker surfaces needs_auth (not error), and REFUSE stays above it ------------------

def test_broker_surfaces_needs_auth_with_url_and_pending():
    b = Broker(integrations=FakeIntegrations(unauthorized={"gmail"}), run_id="r")
    res = b.call_integration(toolkit="gmail", action="GMAIL_FETCH_EMAILS", params={}, user_id="u1")
    assert res.status == "needs_auth"
    assert res.auth_url.startswith("https://connect.arcade.test/gmail")
    assert res.pending == {"toolkit": "gmail", "action": "GMAIL_FETCH_EMAILS", "params": {}}
    assert res.effect is not None and res.effect.phase == "deferred"   # not landed -> gate sees non-completion


def test_money_action_stays_refused_never_needs_auth():
    # a refused (money) action is hard-stopped BEFORE execute, so it can never reach the connect prompt.
    b = Broker(integrations=FakeIntegrations(unauthorized={"stripe"}), run_id="r")
    res = b.call_integration(toolkit="stripe", action="STRIPE_CREATE_CHARGE", params={}, user_id="u1")
    assert res.status == "refused"


# --- C3 + C5: the operator parks on connect, then resumes-at-action when the grant lands --------

def _connect_brain():
    # one step whose ONLY action reads the user's Gmail (AUTO) -> hits the unconnected account immediately.
    return make_brain(steps=[{"text": "check inbox"}],
                      actions={"check inbox": [tc("integration", toolkit="gmail",
                                                  action="GMAIL_FETCH_EMAILS", params={})]})


def test_step_needing_gmail_parks_on_connect_with_a_link():
    integ = FakeIntegrations(unauthorized={"gmail"})
    h = build(model=_connect_brain(), integrations=integ)
    run = h["op"].start(Goal(text="summarize my inbox"))
    assert run.status is RunStatus.AWAITING_CONNECT
    ev = h["channel"].of_kind("connect")
    assert ev and ev[-1]["url"].startswith("https://connect.arcade.test/gmail")
    assert ev[-1]["provider"] == "Gmail"


def test_connect_completes_on_tick_and_resumes_at_action():
    integ = FakeIntegrations(unauthorized={"gmail"})
    h = build(model=_connect_brain(), integrations=integ)
    run = h["op"].start(Goal(text="summarize my inbox"))
    assert run.status is RunStatus.AWAITING_CONNECT

    integ.grant("gmail")                          # the user taps the link and connects their account
    h["timers"].advance(_CONNECT_POLL_S + 1)      # the poll timer comes due
    h["cp"].tick()                                # the durable tick probes auth -> completed -> resume
    assert h["store"].get_run(run.run_id).status is RunStatus.DONE


def test_connect_that_never_lands_escalates_cleanly():
    integ = FakeIntegrations(unauthorized={"gmail"})
    h = build(model=_connect_brain(), integrations=integ)
    run = h["op"].start(Goal(text="summarize my inbox", max_runtime_s=10))   # a tight deadline
    assert run.status is RunStatus.AWAITING_CONNECT

    h["timers"].advance(_CONNECT_POLL_S + 5)      # never granted; the deadline (10s) is now past
    h["cp"].tick()
    run2 = h["store"].get_run(run.run_id)
    assert run2.status is RunStatus.ESCALATED      # honest surfacing, never a silent quit / silent authorize


def test_connect_poll_re_arms_while_still_pending():
    # not yet connected and still within the deadline -> the run stays parked and re-arms the poll.
    integ = FakeIntegrations(unauthorized={"gmail"})
    h = build(model=_connect_brain(), integrations=integ)
    run = h["op"].start(Goal(text="summarize my inbox"))
    h["timers"].advance(_CONNECT_POLL_S + 1)
    h["cp"].tick()
    assert h["store"].get_run(run.run_id).status is RunStatus.AWAITING_CONNECT   # still waiting, not failed
    integ.grant("gmail")                          # connect on the SECOND poll
    h["timers"].advance(_CONNECT_POLL_S + 1)
    h["cp"].tick()
    assert h["store"].get_run(run.run_id).status is RunStatus.DONE


def test_connect_park_survives_a_process_restart_and_resumes_at_action():
    # A parked-on-connect run must rehydrate from the Store in a FRESH process (cold caches) and STILL
    # resume-at-action when the grant lands: the continuation blob (connect + resume_state) is the source
    # of truth, not the operator's in-memory _connect cache. This guards the field-explicit-style hazard —
    # a future drop of the 'connect' key from _persist_continuation/_load_continuation would otherwise pass
    # CI silently (the warm-cache tests above never exercise disk rehydration of the connect park).
    integ = FakeIntegrations(unauthorized={"gmail"})
    h1 = build(model=_connect_brain(), integrations=integ)
    run = h1["op"].start(Goal(text="summarize my inbox"))
    assert run.status is RunStatus.AWAITING_CONNECT

    # Simulate a process restart: a brand-new Operator/ControlPlane over the SAME durable store + timers
    # (+ the same integration world). Its hot caches start EMPTY, so the tick -> resume path MUST read the
    # connect park back from disk via _load_continuation.
    h2 = build(model=_connect_brain(), integrations=integ, store=h1["store"], timers=h1["timers"])
    assert run.run_id not in h2["op"]._connect          # cold cache: nothing in memory on the fresh process

    integ.grant("gmail")                                 # the user taps the link and connects their account
    h2["timers"].advance(_CONNECT_POLL_S + 1)            # the poll timer comes due
    h2["cp"].tick()                                      # fresh process: rehydrate from disk -> resume-at-action
    assert h2["store"].get_run(run.run_id).status is RunStatus.DONE

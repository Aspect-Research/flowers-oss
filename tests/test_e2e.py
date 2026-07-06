"""End-to-end Phase 1 scenarios — the real engine, scripted seams, the motivating journeys.

These are the proof that flowers does what an earlier prototype couldn't: a methodical outreach journey that reaches
a VERIFIED done; a fabricated completion REFUSED through the production path; a blocked search that
CIRCUIT-BREAKS instead of burning the budget; a heartbeat monitor; and durable resume across a
simulated process restart.
"""

from __future__ import annotations

import json

from _harness import build, make_brain, tc

from flowers.seams.integrations import FakeIntegrations
from flowers.seams.interfaces import ModelResponse, SearchResult
from flowers.seams.model import FakeModel
from flowers.seams.search import FakeSearch
from flowers.seams.store import SqliteStore
from flowers.seams.timers import LocalTimers
from flowers.types import Goal, RunStatus, ToolCall

# The four-step outreach journey: find -> email -> await verified reply -> book (verified on calendar).
_OUTREACH_STEPS = [
    {"text": "find a batch of venues", "kind": "generic"},
    {"text": "email the venue at venue@hall.com", "kind": "generic", "depends_on": [0]},
    {"text": "await the venue reply", "kind": "await_replies", "depends_on": [1],
     "params": {"window_seconds": 86400, "min_replies": 1, "match": {"from": "venue@hall.com"}}},
    {"text": "book the venue on the calendar", "kind": "generic", "depends_on": [2],
     "done_criteria": [{"id": "booked", "objective_check": {
         "kind": "effect_landed", "params": {"label": "googlecalendar:GOOGLECALENDAR_CREATE_EVENT"}}}]},
]
_OUTREACH_ACTIONS = {
    "find a batch of venues": [tc("web_search", query="party venues near me")],
    "email the venue": [tc("send_email", to="venue@hall.com", subject="Venue inquiry for our party")],
    "book the venue": [tc("integration", toolkit="googlecalendar",
                          action="GOOGLECALENDAR_CREATE_EVENT", params={"summary": "Party at the Grand Hall"})],
}


def _venue_search():
    return FakeSearch(scripted={"venue": [SearchResult(title="Grand Hall", url="http://grandhall.example",
                                                       snippet="a great party venue")]})


def _drive_outreach_to_booking(h, rid):
    """Helper: run the shared journey up to (and through) the await, leaving it parked on the booking."""
    h["integ"].deliver_inbound("local", sender="venue@hall.com",
                               subject="re: venue inquiry", body="we're available!")
    return h["cp"].deliver(run_id=rid)


def test_full_outreach_journey_reaches_verified_done():
    h = build(model=make_brain(steps=_OUTREACH_STEPS, actions=_OUTREACH_ACTIONS), search=_venue_search(),
              integrations=FakeIntegrations())
    run = h["op"].start(Goal(text="organize the venue for the party"))
    rid = run.run_id
    # 1) search step accepted automatically; email step parks for approval
    assert run.status is RunStatus.AWAITING_APPROVAL
    assert run.pending_approval.effect_label == "gmail:GMAIL_SEND_EMAIL"
    # 2) approve the email -> verified send -> parks on the await
    run = h["cp"].answer(run_id=rid, answer="yes")
    assert run.status is RunStatus.WAITING
    # 3) the venue replies -> verified inbound -> parks on the booking approval
    run = _drive_outreach_to_booking(h, rid)
    assert run.status is RunStatus.AWAITING_APPROVAL
    assert run.pending_approval.effect_label == "googlecalendar:GOOGLECALENDAR_CREATE_EVENT"
    # 4) approve the booking -> verified calendar event -> DONE
    run = h["cp"].answer(run_id=rid, answer="yes")
    assert run.status is RunStatus.DONE
    effs = {(e.label, e.phase, e.expected_present) for e in h["store"].get_effects(rid)}
    assert ("gmail:GMAIL_SEND_EMAIL", "forwarded", True) in effs
    assert ("googlecalendar:GOOGLECALENDAR_CREATE_EVENT", "forwarded", True) in effs
    # methodical, not thrash: the discovery step did exactly one search
    plan = h["store"].get_plan(rid)
    assert plan.steps[0].result is not None and plan.steps[0].result.searches == 1


def test_fabricated_booking_is_refused_in_journey():
    # Same journey, but the calendar create never lands -> the gate refuses the final completion.
    h = build(model=make_brain(steps=_OUTREACH_STEPS, actions=_OUTREACH_ACTIONS), search=_venue_search(),
              integrations=FakeIntegrations(drop_actions={("googlecalendar", "GOOGLECALENDAR_CREATE_EVENT")}))
    run = h["op"].start(Goal(text="organize the venue for the party"))
    rid = run.run_id
    run = h["cp"].answer(run_id=rid, answer="yes")        # email
    run = _drive_outreach_to_booking(h, rid)              # reply -> booking approval
    run = h["cp"].answer(run_id=rid, answer="yes")        # approve the booking (which won't land)
    assert run.status is RunStatus.ESCALATED              # NOT done — fabrication refused
    assert run.status is not RunStatus.DONE


def test_blocked_search_circuit_breaks_instead_of_burning_budget():
    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            return ModelResponse(content=json.dumps({"steps": [{"text": "find a venue"}]}))
        # the (misbehaving) model keeps trying the same blocked search
        return ModelResponse(tool_calls=[ToolCall(name="web_search", args={"query": "party venue near me"})],
                             finish_reason="tool_calls")
    h = build(model=FakeModel(on_complete=fn), search=FakeSearch(blocked={"venue"}))
    run = h["op"].start(Goal(text="find a venue", budget_usd=100.0))   # huge budget — must NOT be the limiter
    assert run.status is RunStatus.ESCALATED
    assert "failed repeatedly" in run.pending_approval.prompt
    assert run.spent_usd == 0.0                                        # it stopped on the breaker, not the budget


def test_monitor_heartbeat_notifies_on_verified_match():
    model = make_brain(steps=[{"text": "watch for the bank email", "kind": "monitor",
                               "params": {"interval_seconds": 3600, "match": {"from": "bank@chase.com"},
                                          "notify": "your bank email arrived"}}])
    h = build(model=model)
    run = h["op"].start(Goal(text="watch for the bank email and ping me"))
    assert run.status is RunStatus.WAITING
    h["integ"].deliver_inbound("local", sender="bank@chase.com", subject="loan decision", body="approved")
    run = h["cp"].deliver(run_id=run.run_id)
    assert run.status is RunStatus.DONE
    assert any(e["kind"] == "notify" for e in h["channel"].events)


def test_await_survives_simulated_process_restart(tmp_path):
    db = str(tmp_path / "flowers.db")
    tdb = str(tmp_path / "timers.db")
    steps = [
        {"text": "watch for the venue reply", "kind": "await_replies",
         "params": {"window_seconds": 3600, "min_replies": 1, "match": {"from": "venue@hall.com"}}},
        {"text": "note the reply", "depends_on": [0]},
    ]
    actions = {"note the reply": [tc("write_file", path="note.md", content="the venue replied")]}
    model = make_brain(steps=steps, actions=actions)
    # The external world (the integration backend) persists independently of OUR process.
    world = FakeIntegrations()

    # --- process 1: start the run, it parks on the durable await ---
    h1 = build(model=model, integrations=world, store=SqliteStore(db), timers=LocalTimers(tdb))
    run = h1["op"].start(Goal(text="organize the venue"))
    rid = run.run_id
    assert run.status is RunStatus.WAITING

    # --- the reply arrives, then we SIMULATE A RESTART: brand-new objects on the SAME db files ---
    world.deliver_inbound("local", sender="venue@hall.com", subject="re: venue", body="available")
    h2 = build(model=model, integrations=world, store=SqliteStore(db), timers=LocalTimers(tdb))
    # the suspended wait + frozen plan survived on disk; resuming on the delivered reply completes it
    run2 = h2["cp"].deliver(run_id=rid)
    assert run2.status is RunStatus.DONE


def test_interim_checks_detect_a_reply_without_an_inbound_channel():
    # Found live: self-hosted flowers has no webhook to call deliver(), so a reply that arrived in
    # minutes sat unseen until the window DEADLINE (two hours later). The await now POLLS: interim
    # check timers probe the inbox mid-window, and only the window timer fires the deadline path.
    h = build(model=make_brain(steps=_OUTREACH_STEPS, actions=_OUTREACH_ACTIONS), search=_venue_search(),
              integrations=FakeIntegrations())
    run = h["op"].start(Goal(text="organize the venue for the party"))
    rid = run.run_id
    h["cp"].answer(run_id=rid, answer="yes")            # approve the email -> parks on the await
    assert h["store"].get_run(rid).status is RunStatus.WAITING

    # an interim check with NO reply yet: keeps waiting, never triggers the next-batch deadline path
    h["timers"].advance(181)
    h["cp"].tick()
    assert h["store"].get_run(rid).status is RunStatus.WAITING
    assert h["store"].get_run(rid).replans == 0         # no "next batch" was sent

    # the reply lands in the INBOX ONLY — deliver() is never called (there is no inbound channel)
    h["integ"].deliver_inbound("local", sender="venue@hall.com",
                               subject="re: venue inquiry", body="we're available!")
    h["timers"].advance(181)                            # the re-armed interim check comes due
    h["cp"].tick()
    run2 = h["store"].get_run(rid)
    assert run2.status is RunStatus.AWAITING_APPROVAL   # reply seen mid-window -> on to the booking
    assert run2.pending_approval.effect_label == "googlecalendar:GOOGLECALENDAR_CREATE_EVENT"

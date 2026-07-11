"""P1.1 — the zombie-run reaper: an ESCALATED run the owner never answers is closed quietly.

On ``_escalate`` the operator arms a durable ``kind="reap"`` timer keyed to THAT escalation's approval id
(``escalation_ttl_h``, 24h default). If it fires and the run is STILL parked on the SAME question, tick()
routes it to ``reap()`` which STOPs the run with a quiet note. The apr.id identity makes a stale timer
harmless: answering, finishing, stopping, or RE-escalating (a fresh apr.id + a fresh timer) all leave the
old one a no-op. These tests drive the REAL engine offline via the Fake seams and the LocalTimers virtual
clock ($0, no sleeping).
"""

from __future__ import annotations

import json
import re

from _harness import build, make_brain, tc

from flowers.seams.integrations import FakeIntegrations
from flowers.seams.interfaces import ModelResponse
from flowers.seams.model import FakeModel
from flowers.types import Goal, RunStatus, ToolCall

_SEND = ("gmail", "GMAIL_SEND_EMAIL")
TTL_S = 24 * 3600      # the default escalation TTL the harness's Operator uses (escalation_ttl_h=24)


def _armed(timers, rid, kind) -> int:
    """Count timers of ``kind`` still armed (not cancelled, not fired) for ``rid`` — a direct read of the
    durable table (same pattern as test_verification_broken / test_escalation_intents)."""
    rows = timers._conn.execute(
        "SELECT kind FROM timers WHERE run_id=? AND cancelled=0 AND fired=0", (rid,)).fetchall()
    return sum(1 for r in rows if r["kind"] == kind)


def _escalated_unverifiable():
    """A one-step send whose Sent read-back is UNAVAILABLE -> forwarded-but-unverifiable ->
    needs_owner_confirm escalation the owner is asked to confirm. Returns (h, run)."""
    h = build(model=make_brain(
        steps=[{"text": "email bob@acme.com"}],
        actions={"email bob": [tc("send_email", to="bob@acme.com", subject="hi", body="hello there")]}),
        integrations=FakeIntegrations(no_readback={"gmail"}))
    run = h["op"].start(Goal(text="email bob@acme.com"))
    run = h["cp"].answer(run_id=run.run_id, answer="yes")
    assert run.status is RunStatus.ESCALATED
    return h, run


def _no_replan_brain():
    """make_brain, but ANY escalation replan (message carries 'OWNER GUIDANCE') yields NO new steps — so a
    guidance reply hits the no-new-steps path. The intent-classify call gets a finish tool call (no valid
    JSON) -> the hard 'guidance' default. Offline."""
    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        if role == "verifier":
            return ModelResponse(content=json.dumps({"satisfied": True}))
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            blob = json.dumps([m.get("content", "") for m in messages])
            if "OWNER GUIDANCE" in blob:
                return ModelResponse(content=json.dumps({"steps": []}))
            return ModelResponse(content=json.dumps({"steps": [{"text": "email bob@acme.com"}]}))
        user = messages[1]["content"]
        m = re.search(r"YOUR STEP \(\d+\): (.+)", user)
        if m and "email bob" in m.group(1) and sum(1 for x in messages if x.get("role") == "tool") == 0:
            return ModelResponse(tool_calls=[tc("send_email", to="bob@acme.com", subject="hi",
                                                body="hello there")], finish_reason="tool_calls")
        return ModelResponse(tool_calls=[ToolCall(name="finish", args={"summary": "done"})],
                             finish_reason="tool_calls")
    return FakeModel(on_complete=fn)


def _two_step_confirm_brain():
    """A 2-step run: step 1 sends (unverifiable -> escalates needs_owner_confirm), step 2 writes a note.
    'it was sent' confirms step 1 and resumes the drive so step 2 runs and the run reaches DONE via the
    normal finalize path (which does NOT cancel timers -> the reap timer stays LIVE)."""
    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        if role == "verifier":
            return ModelResponse(content=json.dumps({"satisfied": True}))
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            return ModelResponse(content=json.dumps({"steps": [
                {"text": "email bob@acme.com"}, {"text": "write a wrap-up note"}]}))
        user = messages[1]["content"]
        m = re.search(r"YOUR STEP \(\d+\): (.+)", user)
        stepname = m.group(1) if m else user
        ntool = sum(1 for x in messages if x.get("role") == "tool")
        if "email bob" in stepname and ntool == 0:
            return ModelResponse(tool_calls=[tc("send_email", to="bob@acme.com", subject="hi",
                body="hello there")], finish_reason="tool_calls")
        if "wrap-up" in stepname and ntool == 0:
            return ModelResponse(content="all wrapped up",
                tool_calls=[ToolCall(name="finish", args={"summary": "wrote the note"})],
                finish_reason="tool_calls")
        return ModelResponse(tool_calls=[ToolCall(name="finish", args={"summary": "done"})],
                             finish_reason="tool_calls")
    return FakeModel(on_complete=fn)


# --------------------------------------------------------------------------- the core reap

def test_ignored_escalation_reaped_after_ttl():
    # An escalation the owner never answers: before the TTL it stays parked; past the TTL the reaper
    # closes it quietly (STOPPED, question cleared, a quiet note — no chore, no dead end).
    h, run = _escalated_unverifiable()
    rid = run.run_id
    assert _armed(h["timers"], rid, "reap") == 1              # the escalation armed a reaper

    h["timers"].advance(TTL_S - 3600)                        # ~hour 23: not yet due
    h["cp"].tick()
    assert h["store"].get_run(rid).status is RunStatus.ESCALATED

    h["timers"].advance(2 * 3600)                            # past the TTL
    h["cp"].tick()
    run = h["store"].get_run(rid)
    assert run.status is RunStatus.STOPPED
    assert run.pending_approval is None
    assert any("closing this out" in e["text"] for e in h["channel"].of_kind("notify"))


def test_reap_no_ops_when_an_unresumed_answer_is_pending():
    # The unprocessed-answer guard: the owner replies right at the TTL boundary — the answer is SAVED at the
    # store (the way cp.answer does) but its resume hasn't run yet — and the reap timer fires in the same
    # tick. The reaper must NOT close a live conversation: it no-ops (run stays ESCALATED, no 'closing this
    # out'), and the deferred resume then consumes the stored answer normally.
    h, run = _escalated_unverifiable()
    rid = run.run_id
    apr_id = run.pending_approval.id
    h["store"].resolve_approval(apr_id, "it was sent")       # answer saved (as cp.answer does); resume NOT run

    h["timers"].advance(TTL_S + 60)                          # the reap timer is due
    h["cp"].tick()
    run = h["store"].get_run(rid)
    assert run.status is RunStatus.ESCALATED                 # NOT reaped — an answer is waiting to be consumed
    assert not any("closing this out" in e["text"] for e in h["channel"].of_kind("notify"))

    run = h["op"].resume(rid)                                # the deferred resume consumes the stored answer
    assert run.status is RunStatus.DONE                      # "it was sent" -> confirmed -> closed normally
    assert any("all set" in e["text"] for e in h["channel"].of_kind("done"))


def test_reescalation_reaps_on_its_own_clock():
    # Owner answers at ~hour 1 with unparseable guidance that RE-escalates a NEW question (the rephrase
    # plea). The OLD reap timer must no-op at hour 24 (its apr.id no longer matches); the NEW one reaps
    # only after ITS own 24h.
    h = build(model=_no_replan_brain(), integrations=FakeIntegrations(no_readback={"gmail"}))
    run = h["op"].start(Goal(text="email bob@acme.com"))
    run = h["cp"].answer(run_id=run.run_id, answer="yes")
    rid = run.run_id
    assert run.status is RunStatus.ESCALATED
    first_apr = run.pending_approval.id

    h["timers"].advance(3600)                                # ~hour 1
    run = h["cp"].answer(run_id=rid, answer="uhh do the thing i guess")   # -> guidance -> re-escalate (plea)
    assert run.status is RunStatus.ESCALATED
    assert run.pending_approval.id != first_apr             # a fresh escalation -> a fresh reap timer

    # ~hour 24: the OLD timer fires but no-ops (stale apr.id); the run stays parked on the NEW question
    h["timers"].advance(TTL_S - 3600 + 60)                  # offset ~ 24h + 60s
    h["cp"].tick()
    assert h["store"].get_run(rid).status is RunStatus.ESCALATED

    # ~hour 25: the NEW timer (armed at hour 1) reaches ITS TTL and reaps
    h["timers"].advance(3600)
    h["cp"].tick()
    assert h["store"].get_run(rid).status is RunStatus.STOPPED
    assert any("closing this out" in e["text"] for e in h["channel"].of_kind("notify"))


def test_done_run_is_never_reaped():
    # A run that escalated then reached DONE (owner confirmed step 1, step 2 ran, finalized) leaves its
    # reap timer LIVE (finalize doesn't cancel it). When that stale timer fires, the identity guard
    # (status is no longer ESCALATED) makes it a no-op — a DONE run is never reaped.
    h = build(model=_two_step_confirm_brain(), integrations=FakeIntegrations(no_readback={"gmail"}))
    run = h["op"].start(Goal(text="email bob@acme.com and write a note"))
    run = h["cp"].answer(run_id=run.run_id, answer="yes")     # approve + send -> escalate on step 1
    rid = run.run_id
    assert run.status is RunStatus.ESCALATED
    run = h["cp"].answer(run_id=rid, answer="it was sent")    # confirm -> resume -> step 2 -> DONE
    assert run.status is RunStatus.DONE
    assert _armed(h["timers"], rid, "reap") >= 1             # the reap timer is still LIVE (not cancelled)

    h["timers"].advance(TTL_S + 60)
    h["cp"].tick()
    assert h["store"].get_run(rid).status is RunStatus.DONE   # the identity guard no-ops it — never reaped
    assert not any("closing this out" in e["text"] for e in h["channel"].of_kind("notify"))


def test_reverify_track_unaffected_and_its_escalation_is_reapable():
    # P0.1's reverify track shares the timer store and tick() with the reaper (kind-discriminated). A
    # verification_broken + dropped send reaches DONE; its +60s re-check still FIRES (reverify unaffected)
    # and PROVES the send missing -> re-escalates. That fresh escalation is itself reapable if ignored.
    integ = FakeIntegrations(readback_errors={_SEND}, drop_actions={_SEND})
    h = build(model=make_brain(
        steps=[{"text": "email bob@acme.com"}],
        actions={"email bob": [tc("send_email", to="bob@acme.com", subject="hi", body="hello there")]}),
        integrations=integ)
    run = h["op"].start(Goal(text="email bob@acme.com"))
    run = h["cp"].answer(run_id=run.run_id, answer="yes")
    rid = run.run_id
    assert run.status is RunStatus.DONE                       # verification_broken completed (landed)
    assert _armed(h["timers"], rid, "reverify") == 1         # the +60s re-check is armed
    assert _armed(h["timers"], rid, "reap") == 0             # not escalated yet -> no reaper

    integ.heal_readback_errors()                             # tool recovers; the dropped send is still gone
    h["timers"].advance(61)
    h["cp"].tick()                                           # reverify fires (dispatched, NOT swallowed by reap)
    run = h["store"].get_run(rid)
    assert run.status is RunStatus.ESCALATED                 # proven missing -> re-opened for the owner
    assert run.pending_approval.reason_code == "reverify_proven_missing"
    assert _armed(h["timers"], rid, "reap") == 1            # the fresh escalation armed a reaper

    h["timers"].advance(TTL_S + 60)                          # the owner ignores the correction
    h["cp"].tick()
    run = h["store"].get_run(rid)
    assert run.status is RunStatus.STOPPED
    assert any("closing this out" in e["text"] for e in h["channel"].of_kind("notify"))

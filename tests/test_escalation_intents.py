"""P0.2 — escalation replies get INTENTS (confirmed / denied / retry / stop / guidance).

An escalation is a parked conversation, not a dead end. Before P0.2 the ONLY move on any owner reply was
"replan with owner guidance", and a no-new-steps replan was treated as failure — so the incident
(run_3c30c72c1b8e): an unverifiable send escalated "can you double-check?", the owner said "It was
sent!", and the run answered "I couldn't turn that into a next step — can you rephrase…" and stayed
ESCALATED. These tests drive the REAL engine offline via the Fake seams ($0, no network) and prove each
intent: the deterministic shortcuts are model-free; the model path hard-defaults to "guidance" (a Fake
model returns no valid JSON, so the default is exercised without a live model).
"""

from __future__ import annotations

import json
import re

from _harness import build, make_brain, tc

from flowers import trustgate
from flowers.engine.operator import _deterministic_intent
from flowers.seams.integrations import FakeIntegrations
from flowers.seams.interfaces import ModelResponse
from flowers.seams.model import FakeModel
from flowers.types import Goal, RunStatus, StepStatus, ToolCall

_SEND = ("gmail", "GMAIL_SEND_EMAIL")
_CAL = ("googlecalendar", "GOOGLECALENDAR_CREATE_EVENT")


def _live_reverify(timers, rid) -> int:
    """Count the +60s re-check timers still armed (not cancelled, not fired) for ``rid`` — a direct read
    of the durable table (the same pattern test_verification_broken uses)."""
    rows = timers._conn.execute(
        "SELECT kind FROM timers WHERE run_id=? AND cancelled=0 AND fired=0", (rid,)).fetchall()
    return sum(1 for r in rows if r["kind"] == "reverify")


def _send_scenario(integ):
    """Build a one-step 'email bob@acme.com' run over ``integ`` and approve the send. Returns (h, run)."""
    h = build(model=make_brain(
        steps=[{"text": "email bob@acme.com"}],
        actions={"email bob": [tc("send_email", to="bob@acme.com", subject="hi", body="hello there")]}),
        integrations=integ)
    run = h["op"].start(Goal(text="email bob@acme.com"))
    assert run.status is RunStatus.AWAITING_APPROVAL      # the send parks for approval (the one touch)
    run = h["cp"].answer(run_id=run.run_id, answer="yes")
    return h, run


def _escalated_unverifiable():
    """A send whose Sent read-back is UNAVAILABLE -> forwarded-but-unverifiable -> a needs_owner_confirm
    escalation (the 'I sent it but couldn't confirm — double-check?' case). Returns (h, run)."""
    h, run = _send_scenario(FakeIntegrations(no_readback={"gmail"}))
    assert run.status is RunStatus.ESCALATED
    assert run.pending_approval.reason_code == "needs_owner_confirm"
    return h, run


def _reverify_proven_missing(integ):
    """Drive a verification_broken + DROPPED send to DONE, then fire the +60s re-check (after the read-back
    tool heals) so it PROVES the send missing and re-opens the run ESCALATED. Returns (h, run)."""
    h, run = _send_scenario(integ)
    rid = run.run_id
    assert run.status is RunStatus.DONE                   # verification_broken completed (landed)
    integ.heal_readback_errors()                          # the read-back tool recovers; the send is missing
    h["timers"].advance(61)
    h["cp"].tick()
    run = h["store"].get_run(rid)
    assert run.status is RunStatus.ESCALATED
    assert run.pending_approval.reason_code == "reverify_proven_missing"
    return h, run


def _no_replan_brain():
    """``make_brain``, but ANY escalation replan (its message carries 'OWNER GUIDANCE') yields NO new
    steps — so a guidance reply hits the no-new-steps path. The intent-classify call (role executor,
    expects JSON) gets a finish tool call with empty content -> the hard 'guidance' default. Offline."""
    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        if role == "verifier":
            return ModelResponse(content=json.dumps({"satisfied": True}))
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            blob = json.dumps([m.get("content", "") for m in messages])
            if "OWNER GUIDANCE" in blob:                  # any escalation replan -> no new steps
                return ModelResponse(content=json.dumps({"steps": []}))
            return ModelResponse(content=json.dumps({"steps": [{"text": "email bob@acme.com"}]}))
        user = messages[1]["content"]
        m = re.search(r"YOUR STEP \(\d+\): (.+)", user)   # a real step -> emit the send once; else finish
        if m and "email bob" in m.group(1) and sum(1 for x in messages if x.get("role") == "tool") == 0:
            return ModelResponse(tool_calls=[tc("send_email", to="bob@acme.com", subject="hi",
                                                body="hello there")], finish_reason="tool_calls")
        return ModelResponse(tool_calls=[ToolCall(name="finish", args={"summary": "done"})],
                             finish_reason="tool_calls")
    return FakeModel(on_complete=fn)


# --------------------------------------------------------------------------- confirmed (the incident)

def test_it_was_sent_confirms_and_closes_done():
    # THE incident regression: an unverifiable send escalates, the owner says "It was sent!", and the run
    # must reach DONE with the attestation recorded — NOT dead-end on a "rephrase" plea.
    h, run = _escalated_unverifiable()
    rid = run.run_id
    run = h["cp"].answer(run_id=rid, answer="It was sent!")
    assert run.status is RunStatus.DONE
    send = next(e for e in h["store"].get_effects(rid)
                if e.label == "gmail:GMAIL_SEND_EMAIL" and e.phase == "forwarded")
    assert send.detail.get("verification") == "owner-confirmed"   # the owner's attestation, on the ledger
    assert send.expected_present is None                          # honest: never an independent read-back
    assert h["channel"].of_kind("escalated")                      # the original escalation did happen
    assert not any("rephrase" in e["text"] for e in h["channel"].events)   # no dead end
    assert "all set" in h["channel"].of_kind("done")[-1]["text"]  # a friendly close was emitted
    # honesty floor: owner-confirmed is a DISTINCT evidence class — verified_effects (strict) still won't
    # list it, so the final report never claims an independent read-back it doesn't have.
    verified = trustgate.verified_effects([e.as_gate_dict() for e in h["store"].get_effects(rid)])
    assert "gmail:GMAIL_SEND_EMAIL" not in verified


def test_bare_confirmation_on_reverify_is_ambiguous_not_confirmed():
    # A bare "yes" on the reverify 'how do you want to handle it?' question is genuinely ambiguous — it must
    # NOT be treated as a send confirmation (that would fabricate an attestation). It falls to guidance.
    h, run = _reverify_proven_missing(FakeIntegrations(readback_errors={_SEND}, drop_actions={_SEND}))
    rid = run.run_id
    run = h["cp"].answer(run_id=rid, answer="yes")
    send = next(e for e in h["store"].get_effects(rid) if e.label == "gmail:GMAIL_SEND_EMAIL")
    assert send.detail.get("verification") != "owner-confirmed"   # no fabricated attestation


# --------------------------------------------------------------------------- bare "no" (D1)
# A bare "no"-family reply is question-dependent (D1): on the "did it arrive?" send-confirmation escalation
# (needs_owner_confirm) it ANSWERS the question — it didn't arrive — i.e. DENIED (the resend path); on the
# open-ended reverify question and on any non-send escalation it keeps its pre-P0.2 meaning: stop.

def _model_error_scenario():
    """A run whose only step fails hard -> a NON-send escalation (reason_code 'model_error'). A replan that
    carries owner guidance yields a fresh step so a guidance reply would drive on. Returns (h, run)."""
    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        if role == "verifier":
            return ModelResponse(content=json.dumps({"satisfied": True}))
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            blob = json.dumps([m.get("content", "") for m in messages])
            if "OWNER GUIDANCE" in blob:
                return ModelResponse(content=json.dumps({"steps": [{"text": "write the note"}]}))
            return ModelResponse(content=json.dumps({"steps": [{"text": "try the flaky path"}]}))
        user = messages[1]["content"]
        if "try the flaky path" in user:
            raise RuntimeError("transport down")          # step 1 fails hard -> escalate (model_error)
        return ModelResponse(tool_calls=[ToolCall(name="finish", args={"summary": "done"})],
                             finish_reason="tool_calls")

    h = build(model=FakeModel(on_complete=fn))
    run = h["op"].start(Goal(text="jot a note"))
    assert run.status is RunStatus.ESCALATED and run.pending_approval.reason_code == "model_error"
    return h, run


def test_bare_no_family_intent_is_denied_only_on_needs_owner_confirm():
    # The model-free mapping (D1), locked including the whole bare "no"-family: denied ONLY on the
    # "did it arrive?" question; stop on every other escalation. Explicit stop tokens always stop.
    for w in ("no", "nope", "nah", "n"):
        assert _deterministic_intent(w, "needs_owner_confirm") == "denied"
        assert _deterministic_intent(w, "reverify_proven_missing") == "stop"
        assert _deterministic_intent(w, "model_error") == "stop"
        assert _deterministic_intent(w, "") == "stop"
    assert _deterministic_intent("stop", "needs_owner_confirm") == "stop"      # explicit stop still stops
    assert _deterministic_intent("leave it", "needs_owner_confirm") == "stop"


def test_bare_no_on_needs_owner_confirm_denies_and_resends():
    # D1: a bare "no" to "did it arrive?" means "it didn't" — the DENIED path (mirror the denied test
    # shape): the subject send is corrected to failed + proven-absent and the resend replan engages. It is
    # NOT a stop.
    h, run = _escalated_unverifiable()
    rid = run.run_id
    run = h["cp"].answer(run_id=rid, answer="no")
    # the honest correction: the owner-reported-missing send is marked failed + proven-absent
    corr = [e for e in h["store"].get_effects(rid)
            if e.detail.get("correction") == "owner-reported-missing"]
    assert len(corr) == 1 and corr[0].phase == "failed" and corr[0].expected_present is False
    # the denied->resend replan engaged (not the stop path): the run did NOT stop, no "leaving it here"
    assert run.status is not RunStatus.STOPPED
    assert not any("leaving it here" in e["text"] for e in h["channel"].of_kind("notify"))


def test_bare_no_on_reverify_proven_missing_still_stops():
    # D1: the reverify escalation asks the open-ended "how do you want to handle it?" — a bare "no" there is
    # "drop it", i.e. STOP (unchanged), never a denial. No effect is flipped.
    h, run = _reverify_proven_missing(FakeIntegrations(readback_errors={_SEND}, drop_actions={_SEND}))
    rid = run.run_id
    run = h["cp"].answer(run_id=rid, answer="no")
    assert run.status is RunStatus.STOPPED
    assert any("leaving it here" in e["text"] for e in h["channel"].of_kind("notify"))
    assert not any(e.detail.get("correction") for e in h["store"].get_effects(rid))   # nothing flipped


def test_bare_no_on_non_send_escalation_still_stops():
    # D1: a bare "no" on a NON send-confirmation escalation (model_error) keeps the pre-P0.2 meaning: stop.
    h, run = _model_error_scenario()
    run = h["cp"].answer(run_id=run.run_id, answer="no")
    assert run.status is RunStatus.STOPPED
    assert any("leaving it here" in e["text"] for e in h["channel"].of_kind("notify"))


# --------------------------------------------------------------------------- denied / retry (resend)

def test_nothing_arrived_flips_effect_and_resends():
    # Owner reports the escalated send never arrived: the effect is corrected to FAILED (releasing the
    # idempotency lock), a resend is replanned + driven, and the broker ACTUALLY forwards a second time
    # (no short-circuit). This is the reviewed P0.1 finding — without the release the resend is silently
    # deduped away.
    integ = FakeIntegrations(readback_errors={_SEND}, drop_actions={_SEND})
    h, run = _reverify_proven_missing(integ)
    rid = run.run_id
    orig = next(e for e in h["store"].get_effects(rid) if e.label == "gmail:GMAIL_SEND_EMAIL")
    assert orig.expected_present is None                          # reverify escalated but did not mutate it
    integ._drop.clear()                                           # the transient drop is over — a resend lands
    run = h["cp"].answer(run_id=rid, answer="nothing arrived")
    assert run.status is RunStatus.DONE
    # the honest correction: the owner-reported-missing send is marked failed + proven-absent
    corr = [e for e in h["store"].get_effects(rid) if e.detail.get("correction") == "owner-reported-missing"]
    assert len(corr) == 1 and corr[0].phase == "failed" and corr[0].expected_present is False
    # a REAL second send executed against the backend (the original was dropped -> surface was empty)
    assert len(h["integ"].surface("local", "sent")) == 1
    assert not any(e.detail.get("idempotent_replay") for e in h["store"].get_effects(rid))   # not a replay
    # and the resend is independently verified in the final ledger (supersedes the failed original)
    verified = trustgate.verified_effects([e.as_gate_dict() for e in h["store"].get_effects(rid)])
    assert "gmail:GMAIL_SEND_EMAIL" in verified


def test_retry_releases_lock_and_resends():
    # "retry" takes the same release-then-resend path as denied (the owner explicitly wants another send).
    integ = FakeIntegrations(readback_errors={_SEND}, drop_actions={_SEND})
    h, run = _reverify_proven_missing(integ)
    rid = run.run_id
    integ._drop.clear()
    run = h["cp"].answer(run_id=rid, answer="retry")
    assert run.status is RunStatus.DONE
    corr = [e for e in h["store"].get_effects(rid) if e.detail.get("correction") == "owner-requested-retry"]
    assert corr and corr[0].phase == "failed"
    assert len(h["integ"].surface("local", "sent")) == 1          # a real second send executed
    assert not any(e.detail.get("idempotent_replay") for e in h["store"].get_effects(rid))


# --------------------------------------------------------------------------- guidance / no dead ends

def test_unparseable_guidance_no_pending_work_closes_done():
    # No-new-steps guidance on a run with NO pending work (all steps done) must NOT dead-end on the
    # rephrase plea — it closes DONE with a friendly line ("nothing more needed — all set.").
    integ = FakeIntegrations(readback_errors={_SEND}, drop_actions={_SEND})
    h = build(model=_no_replan_brain(), integrations=integ)
    run = h["op"].start(Goal(text="email bob@acme.com"))
    run = h["cp"].answer(run_id=run.run_id, answer="yes")
    rid = run.run_id
    assert run.status is RunStatus.DONE                           # verification_broken completed (landed)
    integ.heal_readback_errors()
    h["timers"].advance(61)
    h["cp"].tick()
    assert h["store"].get_run(rid).status is RunStatus.ESCALATED  # reverify proved it missing

    run = h["cp"].answer(run_id=rid, answer="hmm ok whatever then")
    assert run.status is RunStatus.DONE
    assert not any("rephrase" in e["text"] for e in h["channel"].events)   # never a dead end
    assert any("nothing more needed" in e["text"] for e in h["channel"].of_kind("done"))


def test_unparseable_guidance_with_pending_work_pleas_at_most_once():
    # No-new-steps guidance on a run that GENUINELY has pending work (the send step FAILED the gate) may
    # ask ONCE to rephrase — but never twice in a row: a second unparseable guidance closes DONE instead.
    h = build(model=_no_replan_brain(), integrations=FakeIntegrations(no_readback={"gmail"}))
    run = h["op"].start(Goal(text="email bob@acme.com"))
    run = h["cp"].answer(run_id=run.run_id, answer="yes")
    rid = run.run_id
    assert run.status is RunStatus.ESCALATED
    assert run.pending_approval.reason_code == "needs_owner_confirm"   # the send step FAILED -> pending work

    run = h["cp"].answer(run_id=rid, answer="uhh do the thing i guess")   # FIRST unparseable guidance
    assert run.status is RunStatus.ESCALATED
    assert len([e for e in h["channel"].of_kind("escalated") if "rephrase" in e["text"]]) == 1

    run = h["cp"].answer(run_id=rid, answer="whatever man")               # SECOND -> never plea twice
    assert run.status is RunStatus.DONE
    assert len([e for e in h["channel"].of_kind("escalated") if "rephrase" in e["text"]]) == 1
    assert any("nothing more needed" in e["text"] for e in h["channel"].of_kind("done"))


# --------------------------------------------------------------------------- other escalations untouched

def test_non_send_escalation_reply_still_replans_as_guidance():
    # A NON send-confirmation escalation (here: a hard step failure) must keep the pre-P0.2 behavior — an
    # owner reply is folded into a replan and the SAME run drives on. Intent classification must not hijack
    # it into a confirmed/denied branch.
    calls = {"replan": 0}

    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        if role == "verifier":
            return ModelResponse(content=json.dumps({"satisfied": True}))
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            blob = json.dumps([m.get("content", "") for m in messages])
            if "OWNER GUIDANCE" in blob:
                calls["replan"] += 1
                return ModelResponse(content=json.dumps({"steps": [{"text": "write the note"}]}))
            return ModelResponse(content=json.dumps({"steps": [{"text": "try the flaky path"}]}))
        user = messages[1]["content"]
        if "try the flaky path" in user:
            raise RuntimeError("transport down")          # step 1 fails hard -> escalate (model_error)
        return ModelResponse(tool_calls=[ToolCall(name="finish", args={"summary": "done"})],
                             finish_reason="tool_calls")

    h = build(model=FakeModel(on_complete=fn))
    run = h["op"].start(Goal(text="jot a note"))
    assert run.status is RunStatus.ESCALATED
    assert run.pending_approval.reason_code == "model_error"   # NOT a send-confirmation escalation
    run = h["cp"].answer(run_id=run.run_id, answer="try writing it up as a plain note instead")
    assert run.status is RunStatus.DONE and calls["replan"] == 1   # folded into a replan, drove to done


def test_retry_on_non_send_escalation_replans_and_flips_nothing():
    # DEFECT 1: "retry" is GATED on a send-confirmation escalation. On a NON-send escalation (model_error)
    # "retry" must NOT take the resend path (there is no send to re-issue) — it falls through to guidance,
    # where the replan naturally reads it as "try the failed step again". No effect is corrected.
    calls = {"replan": 0}

    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        if role == "verifier":
            return ModelResponse(content=json.dumps({"satisfied": True}))
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            blob = json.dumps([m.get("content", "") for m in messages])
            if "OWNER GUIDANCE" in blob:
                calls["replan"] += 1
                return ModelResponse(content=json.dumps({"steps": [{"text": "write the note"}]}))
            return ModelResponse(content=json.dumps({"steps": [{"text": "try the flaky path"}]}))
        user = messages[1]["content"]
        if "try the flaky path" in user:
            raise RuntimeError("transport down")          # step 1 fails hard -> escalate (model_error)
        return ModelResponse(tool_calls=[ToolCall(name="finish", args={"summary": "done"})],
                             finish_reason="tool_calls")

    h = build(model=FakeModel(on_complete=fn))
    run = h["op"].start(Goal(text="jot a note"))
    assert run.status is RunStatus.ESCALATED and run.pending_approval.reason_code == "model_error"
    run = h["cp"].answer(run_id=run.run_id, answer="retry")
    assert run.status is RunStatus.DONE and calls["replan"] == 1     # guidance replan, NOT a resend
    assert not any(e.detail.get("correction") for e in h["store"].get_effects(run.run_id))  # nothing flipped


# ------------------------------------------------------------ subject-scoping (one run, two side-effects)
# The two DEFECT-1 scoping tests need a run carrying BOTH a plain-unverifiable send AND a separate
# verification_broken side-effect (on its own +60s reverify track). The only true "send" label is gmail's,
# and the gate collapses same-label effects, so the second side-effect is a googlecalendar create — the
# stand-in for "another forwarded, self-verify-broken effect". Step 1 (calendar) lands verification_broken
# (arming its reverify timer); step 2 (gmail) escalates plain-unverifiable. Judgment call: a non-send
# second effect is unavoidable given one send label per toolkit; it exercises the exact scoping path.

def _two_effect_brain():
    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        if role == "verifier":
            return ModelResponse(content=json.dumps({"satisfied": True}))
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            blob = json.dumps([m.get("content", "") for m in messages])
            if "COMPLETED" in blob:                       # any replan (incl. a resend) -> no new steps
                return ModelResponse(content=json.dumps({"steps": []}))
            return ModelResponse(content=json.dumps({"steps": [
                {"text": "add the calendar event"}, {"text": "email bob@acme.com"}]}))
        user = messages[1]["content"]
        m = re.search(r"YOUR STEP \(\d+\): (.+)", user)
        stepname = m.group(1) if m else user
        ntool = sum(1 for x in messages if x.get("role") == "tool")
        if "calendar" in stepname and ntool == 0:
            return ModelResponse(tool_calls=[tc("integration", toolkit="googlecalendar",
                action="GOOGLECALENDAR_CREATE_EVENT", params={"summary": "sync with bob"})],
                finish_reason="tool_calls")
        if "email bob" in stepname and ntool == 0:
            return ModelResponse(tool_calls=[tc("send_email", to="bob@acme.com", subject="hi",
                body="hello there")], finish_reason="tool_calls")
        return ModelResponse(tool_calls=[ToolCall(name="finish", args={"summary": "done"})],
                             finish_reason="tool_calls")
    return FakeModel(on_complete=fn)


def _two_effect_scenario(integ):
    """A run whose step 1 forwards a verification_broken calendar create (reverify armed) and whose step 2
    forwards a plain-unverifiable gmail send that escalates needs_owner_confirm. Returns (h, run)."""
    h = build(model=_two_effect_brain(), integrations=integ)
    run = h["op"].start(Goal(text="add a calendar event and email bob@acme.com"))
    run = h["cp"].answer(run_id=run.run_id, answer="yes")     # approve the calendar create
    run = h["cp"].answer(run_id=run.run_id, answer="yes")     # approve the gmail send
    assert run.status is RunStatus.ESCALATED
    assert run.pending_approval.reason_code == "needs_owner_confirm"
    return h, run


def test_needs_owner_confirm_reply_flips_only_the_subject_send():
    # DEFECT 1 (b): "nothing arrived" on the gmail send's escalation must flip ONLY that send — the
    # co-resident verification_broken calendar effect (its own +60s reverify track) is untouched: still
    # forwarded / expected None, with its reverify timer intact.
    h, run = _two_effect_scenario(FakeIntegrations(no_readback={"gmail"}, readback_errors={_CAL}))
    rid = run.run_id
    # the escalation is stamped with EXACTLY the gmail send (the plain-unverifiable one), not the calendar
    assert len(run.pending_approval.subject_keys) == 1
    run = h["cp"].answer(run_id=rid, answer="nothing arrived")

    effs = h["store"].get_effects(rid)
    gmail = [e for e in effs if e.label == "gmail:GMAIL_SEND_EMAIL"]
    cal = next(e for e in effs if e.label == "googlecalendar:GOOGLECALENDAR_CREATE_EVENT")
    # ONLY the gmail send was corrected to failed + proven-absent
    corr = [e for e in gmail if e.detail.get("correction") == "owner-reported-missing"]
    assert len(corr) == 1 and corr[0].phase == "failed" and corr[0].expected_present is False
    # the verification_broken calendar effect is UNTOUCHED, and its reverify timer still armed
    assert cal.phase == "forwarded" and cal.expected_present is None
    assert cal.detail.get("readback_error") and not cal.detail.get("correction")
    assert _live_reverify(h["timers"], rid) == 1


def test_reverify_proven_missing_reply_flips_only_the_reverified_send():
    # DEFECT 1 (c): the calendar create is verification_broken AND dropped; its +60s re-check proves it
    # missing and re-escalates reverify_proven_missing stamped with THAT send alone. "nothing arrived" then
    # flips ONLY the calendar effect — the co-resident plain-unverifiable gmail send is untouched.
    integ = FakeIntegrations(no_readback={"gmail"}, readback_errors={_CAL}, drop_actions={_CAL})
    h, run = _two_effect_scenario(integ)
    rid = run.run_id
    integ.heal_readback_errors(_CAL)                         # the read-back tool recovers; the create is gone
    h["timers"].advance(61)
    h["cp"].tick()
    run = h["store"].get_run(rid)
    assert run.status is RunStatus.ESCALATED
    assert run.pending_approval.reason_code == "reverify_proven_missing"
    assert len(run.pending_approval.subject_keys) == 1      # stamped with the calendar send, not the gmail one
    run = h["cp"].answer(run_id=rid, answer="nothing arrived")

    effs = h["store"].get_effects(rid)
    cal = next(e for e in effs if e.label == "googlecalendar:GOOGLECALENDAR_CREATE_EVENT")
    gmail = next(e for e in effs if e.label == "gmail:GMAIL_SEND_EMAIL")
    assert cal.phase == "failed" and cal.expected_present is False       # the reverified send was flipped
    assert cal.detail.get("correction") == "owner-reported-missing"
    assert gmail.phase == "forwarded" and gmail.expected_present is None  # the sibling send is UNTOUCHED
    assert not gmail.detail.get("correction")


# ------------------------------------------------------------------- confirm continues a multi-step run

def test_confirm_resumes_remaining_plan_instead_of_hard_closing():
    # DEFECT 2: an unverifiable send escalates on step 1 of a TWO-step run. "it was sent" records the
    # attestation AND resumes the normal drive loop (step 2 still runs, objective checks still apply) —
    # the run reaches DONE via the ordinary finalize path, not a hard close that skips the rest.
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

    h = build(model=FakeModel(on_complete=fn), integrations=FakeIntegrations(no_readback={"gmail"}))
    run = h["op"].start(Goal(text="email bob@acme.com and write a note"))
    run = h["cp"].answer(run_id=run.run_id, answer="yes")     # approve + send -> escalate on step 1
    rid = run.run_id
    assert run.status is RunStatus.ESCALATED and run.pending_approval.reason_code == "needs_owner_confirm"

    run = h["cp"].answer(run_id=rid, answer="it was sent")
    assert run.status is RunStatus.DONE
    # step 2 actually ran (the plan was carried on, not skipped)
    steps = h["store"].get_plan(rid).steps
    assert len(steps) == 2 and all(s.status is StepStatus.DONE for s in steps)
    assert any("carrying on" in e["text"] for e in h["channel"].events)   # the short ack was emitted
    # the send's attestation is on the ledger; expected_present stays honest (no fabricated read-back)
    send = next(e for e in h["store"].get_effects(rid)
                if e.label == "gmail:GMAIL_SEND_EMAIL" and e.phase == "forwarded")
    assert send.detail.get("verification") == "owner-confirmed" and send.expected_present is None
    # reached DONE via the NORMAL finalize path (the run's deliverable is reported), not the friendly
    # one-liner hard close, and never a dead-end plea
    done = h["channel"].of_kind("done")[-1]["text"]
    assert "wrote the note" in done and "all set then" not in done
    assert not any("rephrase" in e["text"] for e in h["channel"].events)

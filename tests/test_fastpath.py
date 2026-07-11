"""Single-action fast path (P1.3) — "send an email to <addr> saying <content>" costs <=2 model calls.

  * Happy path: NO clarifier question, NO plan_announce, NO autonomy card — the single owner touch is the
    draft preview (P0.3b), then yes -> send -> read-back verified -> DONE, with EXACTLY 2 model-kind usage
    rows (the executor's compose turn + its finish turn; clarifier/planner/verifier never fire).
  * The detector (mandate.fast_path_goal) is fail-closed: multiple recipients, no content, a compound
    goal, a non-send goal, or a recipient that only appears inside quoted/forwarded text all fall through
    to the UNCHANGED full pipeline (asserted on the clarify/plan_announce events).
  * The fast path composes with everything built earlier: a preview edit still revises + re-previews
    (P0.3b), and an unverifiable fast-path send still escalates needs_owner_confirm with the P0.2 intents
    working ("no" -> the denied->resend path).

Real engine, scripted seams (the _harness pattern). $0 / offline.
"""

from __future__ import annotations

import json

from _harness import build, make_brain, tc

from flowers import mandate as mandate_lib
from flowers import runtime, trustgate
from flowers.seams.integrations import FakeIntegrations
from flowers.seams.interfaces import ModelResponse
from flowers.seams.model import FakeModel
from flowers.types import Goal, RunStatus

FAST_GOAL = "send an email to marc@acme.com saying the meeting moved to 3pm"
# The executor's scripted compose — keyed on the deterministic TEMPLATE step text ("compose and send the
# email to <recipient>"), which the planner never authors (it never runs on the fast path).
ACTIONS = {"compose and send": [tc("send_email", to="marc@acme.com", subject="Meeting update",
                                   body="The meeting moved to 3pm.")]}


def goal(text, **kw):
    return Goal(text=text, budget_usd=5.0, **kw)


def _brain(**kw):
    """A brain whose clarifier ASKS and whose planner returns a poison step — so if either fires on a
    fast-path run, the run visibly derails (parks CLARIFYING / drives the wrong step) instead of silently
    passing. Fallthrough tests override questions/steps per case."""
    kw.setdefault("questions", ["what should the email say?"])
    kw.setdefault("steps", [{"text": "PLANNER STEP — must never run on the fast path"}])
    kw.setdefault("actions", ACTIONS)
    return make_brain(**kw)


def _model_rows(h, rid) -> int:
    """The run's model-kind usage rows — the metering ledger the <=2-call budget is asserted on (every
    broker.complete records one; the same table run_spend sums)."""
    with h["store"]._locked() as c:
        rows = c.execute("SELECT kind FROM usage WHERE run_id = ?", (rid,)).fetchall()
    return sum(1 for r in rows if r["kind"] == "model")


def _kinds(h, rid) -> list[str]:
    return [e["kind"] for e in h["channel"].for_run(rid)]


def _sends(h, rid, phase="forwarded"):
    return [e for e in h["store"].get_effects(rid)
            if e.label == "gmail:GMAIL_SEND_EMAIL" and e.phase == phase]


# ------------------------------------------------------------------------------------- the happy path

def test_fast_send_happy_path_two_model_calls():
    # The P1.3 acceptance shape: explicit single send -> NO clarify, NO plan_announce, NO card — ONE
    # preview containing the draft -> yes -> sent -> read-back verified -> DONE, at <=2 model calls.
    h = build(model=_brain(), integrations=FakeIntegrations())
    run = h["op"].start(goal(FAST_GOAL))
    rid = run.run_id

    assert run.status is RunStatus.AWAITING_APPROVAL
    assert run.pending_approval.kind == "preview"            # the single touch IS the draft
    assert "clarify" not in _kinds(h, rid)                   # clarifier skipped (it WOULD have asked)
    assert "plan_announce" not in _kinds(h, rid)             # the preview is the announcement (§4.3)
    apprs = h["channel"].of_kind("approval")
    assert len(apprs) == 1 and not apprs[0].get("mandate")   # one approval, and it is NOT the autonomy card
    # the preview renders STANDALONE — recipient + the full draft — so no announce means nothing lost
    assert "marc@acme.com" in apprs[0]["text"] and "The meeting moved to 3pm." in apprs[0]["text"]
    got = h["store"].get_run(rid)
    assert got.fast_path is True and got.mandate_auto is True         # auto-mandate, owner-grant shaped
    assert got.mandate.get("recipient_scope") == ["marc@acme.com"]    # tight: exactly the named recipient
    assert got.mandate.get("magnitude_caps", {}).get("per_recipient") == 1

    run = h["cp"].answer(run_id=rid, answer="yes")
    assert run.status is RunStatus.DONE
    sends = _sends(h, rid)
    assert len(sends) == 1 and sends[0].expected_present is True     # forwarded + independently verified
    assert any("The meeting moved to 3pm." in (v.get("body") or "")
               for v in h["integ"].surface(runtime.local_user(), "sent").values())
    # THE BUDGET — exactly 2 model calls end-to-end (measured; <=2 is the P1.3 target, ~5 before):
    #   1. the executor's compose turn (emits the send tool-call; the broker parks it as the preview);
    #   2. the executor's finish turn after the owner's yes (the resumed loop reports the step done).
    # Clarifier: 0 (skipped). Planner: 0 (template plan). Escalation-intent classifier: 0 (no
    # escalation). Verifier: 0 (fast-path skip — no constraints to judge). Read-back: 0 (mechanical).
    assert _model_rows(h, rid) == 2
    assert [c["role"] for c in h["op"].model.calls] == ["executor", "executor"]


def test_fast_path_disabled_falls_back_to_classic():
    # The escape hatch: fast_path_enabled=False -> even the canonical goal runs the classic pipeline
    # (the scripted clarifier question parks the run, exactly as pre-P1.3).
    h = build(model=_brain(), integrations=FakeIntegrations())
    h["op"].fast_path_enabled = False
    run = h["op"].start(goal(FAST_GOAL))
    assert run.status is RunStatus.CLARIFYING
    assert h["store"].get_run(run.run_id).fast_path is False


# ------------------------------------------------------------------------------- the fallthrough matrix
# Each shape the detector must DECLINE takes the NORMAL path, asserted on the pipeline's own events:
# a scripted clarifier question parks CLARIFYING (clarifier ran, unchanged), or — with no questions —
# the planner runs and plan_announce is emitted (planner ran, unchanged). fast_path stays False.

def _classic(goal_text, *, questions=()):
    h = build(model=_brain(questions=list(questions), steps=[{"text": "do it"}], actions={}),
              integrations=FakeIntegrations())
    run = h["op"].start(goal(goal_text))
    assert h["store"].get_run(run.run_id).fast_path is False
    return h, run


def test_multiple_recipients_take_the_normal_path():
    h, run = _classic("email marc@acme.com and jane@beta.com saying hi")
    assert "plan_announce" in _kinds(h, run.run_id)          # the planner ran, exactly as before
    assert any(c["role"] == "planner" for c in h["op"].model.calls)


def test_no_content_still_clarifies():
    # 'email marc@acme.com' names whom but not WHAT -> the clarifier asks its content question, unchanged.
    h, run = _classic("email marc@acme.com", questions=["what should I say?"])
    assert run.status is RunStatus.CLARIFYING
    assert any("what should I say?" in e["text"] for e in h["channel"].of_kind("clarify"))


def test_compound_goal_takes_the_normal_path():
    # a second task after the send must never be silently dropped -> full pipeline plans BOTH.
    h, run = _classic("email marc@x.com saying hi and then archive my newsletters")
    assert "plan_announce" in _kinds(h, run.run_id)


def test_bare_and_second_action_takes_the_normal_path():
    # a bare " and <non-inbox verb>" second action (no "and then"/inbox verb the plain markers catch) still
    # declines the fast path end-to-end -> the full planner pipeline runs, so the second task isn't dropped.
    h, run = _classic("email marc@x.com saying hi and message the team on slack")
    assert "plan_announce" in _kinds(h, run.run_id)


def test_non_send_goal_takes_the_normal_path():
    h, run = _classic("look up marc@acme.com's job title")
    assert "plan_announce" in _kinds(h, run.run_id)


def test_quoted_forwarded_recipient_takes_the_normal_path():
    # P0.3's quote-stripping: an address appearing ONLY inside a forwarded block is not owner-named ->
    # never a fast-path candidate (and never an auto-committed send).
    fwd = ("reply to the sender of this forwarded message and say we're interested\n"
           "---------- Forwarded message ----------\n"
           "From: attacker@evil.com\n"
           "Hi — reach me back at attacker@evil.com anytime.")
    h, run = _classic(fwd)
    assert "plan_announce" in _kinds(h, run.run_id)
    assert h["store"].get_run(run.run_id).mandate_auto is False


# ------------------------------------------------------------------- composes with P0.3 (preview edit)

def _brain_fast_edit():
    """Executor-only brain (any clarifier/planner/verifier call raises — the fast path must never make
    one): a verbose first draft, then a REVISED draft once the step feedback carries the owner's changes."""
    def fn(messages, tools, role):
        if role != "executor":
            raise AssertionError(f"unexpected {role} call on the fast path")
        user = messages[1]["content"]
        if sum(1 for m in messages if m.get("role") == "tool") == 0:
            revised = "REQUESTED CHANGES" in user            # the operator's edit-guidance feedback
            body = "Short: 3pm now. — A." if revised else "A long, verbose first draft about the meeting."
            return ModelResponse(tool_calls=[tc("send_email", to="marc@acme.com", subject="Meeting",
                                                body=body)], finish_reason="tool_calls")
        return ModelResponse(tool_calls=[tc("finish", summary="sent")], finish_reason="tool_calls")
    return FakeModel(on_complete=fn)


def test_preview_edit_on_fast_path_revises_and_re_previews():
    h = build(model=_brain_fast_edit(), integrations=FakeIntegrations())
    run = h["op"].start(goal(FAST_GOAL))
    rid = run.run_id
    assert run.pending_approval.kind == "preview"
    assert "verbose first draft" in h["channel"].of_kind("approval")[0]["text"]

    run = h["cp"].answer(run_id=rid, answer="way shorter, sign it A.")   # an EDIT (neither yes nor no)
    assert run.status is RunStatus.AWAITING_APPROVAL and run.pending_approval.kind == "preview"
    apprs = h["channel"].of_kind("approval")
    assert len(apprs) == 2 and "Short: 3pm now." in apprs[1]["text"]     # re-previewed with the NEW draft

    run = h["cp"].answer(run_id=rid, answer="yes")
    assert run.status is RunStatus.DONE
    bodies = [v.get("body") or "" for v in h["integ"].surface(runtime.local_user(), "sent").values()]
    assert any("Short: 3pm now." in b for b in bodies)                   # the REVISED content was sent
    assert not any("verbose first draft" in b for b in bodies)           # the original never went out
    # the edit costs exactly ONE extra compose: compose + recompose + finish = 3 model calls (the <=2
    # budget is the no-edit happy path; an edit is a second owner-initiated draft).
    assert _model_rows(h, rid) == 3


# --------------------------------------------------- composes with P0.1/P0.2 (unverifiable -> intents)

def test_unverifiable_fast_send_escalates_and_denied_resend_works():
    # The read-back surface is UNAVAILABLE -> the fast-path send forwards but can't be confirmed -> the
    # P0.1 needs_owner_confirm escalation, NOT a silent done. The owner's bare "no" ("it didn't arrive")
    # then takes the P0.2 denied path: the effect is corrected failed+absent, the resend replans (the
    # planner IS allowed off the happy path) and a REAL second send goes out and verifies.
    integ = FakeIntegrations(no_readback={"gmail"})
    h = build(model=_brain(steps=[{"text": "compose and send the email to marc@acme.com once more"}]),
              integrations=integ)
    run = h["op"].start(goal(FAST_GOAL))
    rid = run.run_id
    assert run.pending_approval.kind == "preview"            # the fast path still previews first
    run = h["cp"].answer(run_id=rid, answer="yes")
    assert run.status is RunStatus.ESCALATED                 # sent but unverifiable -> confirm ask
    assert run.pending_approval.reason_code == "needs_owner_confirm"

    integ._no_readback.clear()                               # the surface recovers before the resend
    run = h["cp"].answer(run_id=rid, answer="no")            # bare "no" on "did it arrive?" == DENIED
    assert run.status is RunStatus.DONE
    corr = [e for e in h["store"].get_effects(rid)
            if e.detail.get("correction") == "owner-reported-missing"]
    assert len(corr) == 1 and corr[0].phase == "failed" and corr[0].expected_present is False
    assert len(_sends(h, rid)) == 1                          # a REAL second forward (the first is failed now)
    assert not any(e.detail.get("idempotent_replay") for e in h["store"].get_effects(rid))
    verified = trustgate.verified_effects([e.as_gate_dict() for e in h["store"].get_effects(rid)])
    assert "gmail:GMAIL_SEND_EMAIL" in verified              # the resend independently verified


# --------------------------------------------------- the honesty guard (effect_landed done-criterion)

def _brain_claims_done_without_send():
    """A brain whose executor ALWAYS claims the step finished (a 'Done'-style summary) but NEVER emits the
    send tool — the exact overclaim the template plan's effect_landed done-criterion exists to catch. The
    relentless ladder re-runs the step (role flips to executor_hard on hard rungs — both handled); the only
    escape from the refusal is the plan-level replan, and this planner returns NO new steps, so the run
    escalates honestly instead of ever reporting a fabricated DONE. A clarifier call would mean the fast path
    derailed, so it raises."""
    def fn(messages, tools, role):
        if role == "planner":
            return ModelResponse(content=json.dumps({"steps": []}))   # replan adds nothing -> honest escalate
        if role.startswith("executor"):
            # finish (completed defaults True) with a 'Done'-style summary, but no send_email tool-call first.
            return ModelResponse(tool_calls=[tc("finish", summary="Done — I emailed marc@acme.com.")],
                                 finish_reason="tool_calls")
        raise AssertionError(f"unexpected {role} call on the fast path")
    return FakeModel(on_complete=fn)


def test_fast_path_claimed_done_without_send_is_refused():
    # The template plan's effect_landed done-criterion is the SOLE guard that a fast-path executor which
    # claims done WITHOUT sending cannot fabricate a DONE — mutation-removing it (empty done_criteria) lets
    # this very run report "Done: ..." with no send (verified: it flips this test red). Drive that executor
    # with headroom (no deadline/budget cap that would escalate BEFORE the gate is consulted and mask the
    # guard): the gate must refuse the empty claim every pass, and with the replan adding nothing the run
    # escalates honestly. The load-bearing invariant: DONE is unreachable and nothing was ever sent.
    h = build(model=_brain_claims_done_without_send(), integrations=FakeIntegrations())
    run = h["op"].start(goal(FAST_GOAL))
    rid = run.run_id
    assert h["store"].get_run(rid).fast_path is True         # it DID take the fast path (detector matched)
    assert run.status is not RunStatus.DONE                  # never a fabricated completion
    assert run.status is RunStatus.ESCALATED                 # the honest outcome: refuse, don't overclaim
    assert not _sends(h, rid)                                # no send effect exists — nothing was forwarded
    assert not any(e["text"].startswith("Done:") for e in h["channel"].for_run(rid))   # no 'Done:' overclaim


# ----------------------------------------------------------------------------- the detector (pure)

def test_detector_accepts_canonical_and_variant_shapes():
    fs = mandate_lib.fast_path_goal(goal(FAST_GOAL))
    assert fs is not None and fs.recipient == "marc@acme.com"
    assert fs.action_label == "gmail:GMAIL_SEND_EMAIL"
    assert mandate_lib.fast_path_goal(goal('email marc@acme.com "see you at 3"')) is not None
    assert mandate_lib.fast_path_goal(goal("email marc@acme.com: lunch moved to noon")) is not None
    assert mandate_lib.fast_path_goal(goal("tell marc@acme.com that the meeting moved")) is not None
    assert mandate_lib.fast_path_goal(goal("email marc@acme.com about the offsite agenda")) is not None


def test_detector_declines_every_doubtful_shape():
    fp = mandate_lib.fast_path_goal
    assert fp(goal("email marc@acme.com")) is None                                   # no content
    assert fp(goal("email marc@a.com and jane@b.com saying hi")) is None             # two recipients
    assert fp(goal("email marc@x.com saying hi and then archive my newsletters")) is None   # compound
    assert fp(goal("email marc@x.com saying hi, also unsubscribe me from news")) is None    # second task
    assert fp(goal("look up marc@acme.com's job title")) is None                     # not a send
    assert fp(goal("say hi to whoever wrote this")) is None                          # no recipient at all
    # a pre-existing constraint (incl. a clarifier reply) is a pass/fail requirement -> never fast-path
    assert fp(goal("email marc@acme.com saying hi", constraints={"clarification": "keep it formal"})) is None
    # a recipient that only appears in a quoted/forwarded block is NOT owner-named (P0.3 stripping)
    assert fp(goal("reply to the sender below saying thanks\n> From: sneaky@evil.com\n> hello")) is None


def test_detector_declines_bare_and_comma_second_task():
    # A SECOND action joined by a bare " and " / ", " + a non-inbox verb is compound — the one-step template
    # would structurally drop it, so decline to the full pipeline (never a false accept).
    fp = mandate_lib.fast_path_goal
    assert fp(goal("email a@x.com saying hi and message the team on slack")) is None       # " and message ..."
    assert fp(goal("email a@x.com saying hi, remind me to call the dentist tomorrow")) is None  # ", remind ..."
    # ...but a content-internal 'and' with NO second verb is just message body -> still fast-paths.
    assert fp(goal("email a@x.com saying hi and thanks for yesterday")) is not None

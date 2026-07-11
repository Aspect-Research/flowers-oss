"""OWNER-GRANT + draft preview (P0.3) — the incident-class fix that makes an explicit imperative
"just work" with a single, substantive touch.

  * OWNER-GRANT (P0.3a): a goal that names the action + the recipient(s) auto-commits a TIGHT mandate
    (exactly the named recipients, one send each) — no autonomy card. Anything broader (an un-named
    recipient/domain, a delete/cancel class) still shows the card, unchanged.
  * Draft preview (P0.3b): under an auto-committed mandate a delivering send is surfaced ONCE as the
    literal draft the owner confirms (FLOWERS_SEND_PREVIEW=always, the default) — yes sends, no stops,
    anything else revises + re-previews. preview=never sends directly (zero touches). A card-approved
    mandate is unchanged (sends silently — the owner already saw the plan).

Real engine, scripted seams (the _harness pattern). $0 / offline.
"""

from __future__ import annotations

import json

from _harness import build, make_brain, tc

from flowers import mandate as mandate_lib
from flowers import runtime
from flowers.seams.integrations import FakeIntegrations
from flowers.seams.interfaces import ModelResponse
from flowers.seams.model import FakeModel
from flowers.types import Goal, RunStatus

# A single delivering-send step to a named recipient — the OWNER-GRANT happy shape.
# Goals here are CONTENT-LESS ("email marc@acme.com") so the P1.3 fast-path detector declines them and
# the CLASSIC clarify->plan->owner_grant path stays under test; the two incident tests keep their
# content-bearing goals and now ride the fast path (the production path for that shape since P1.3), so
# the ACTIONS key matches BOTH the planner step ("send the note") and the fast-path template step
# ("compose and send the email to ..."). The fast path's own suite is tests/test_fastpath.py.
MANDATE = {"action_types": ["gmail:GMAIL_SEND_EMAIL"], "recipient_scope": ["marc@acme.com"],
           "magnitude_caps": {"max_sends": 1, "per_domain": 1, "per_recipient": 1}}
STEPS = [{"text": "send the note", "kind": "generic"}]
ACTIONS = {"send the": [tc("send_email", to="marc@acme.com", subject="Hi",
                           body="Hi Marc — hope you're well.")]}


def goal(text):
    return Goal(text=text, budget_usd=5.0)


def _sent(h) -> dict:
    return h["integ"].surface(runtime.local_user(), "sent")


def _touches(h, rid) -> list[dict]:
    """The events the owner actually had to ANSWER — approvals (side_effect/mandate/preview), clarifying
    questions, and escalations. Progress/notify/plan_announce/done are informational, not touches."""
    return [e for e in h["channel"].for_run(rid) if e["kind"] in ("approval", "clarify", "escalated")]


# --------------------------------------------------------------------- OWNER-GRANT scope-coverage matrix

def test_goal_named_recipient_auto_commits_no_card():
    # goal names the recipient + says "email" -> auto-commit, NO AWAITING_GO card. (preview=never here to
    # isolate the auto-commit from the draft preview.)
    h = build(model=make_brain(steps=STEPS, actions=ACTIONS, mandate=MANDATE),
              integrations=FakeIntegrations())
    h["op"].send_preview = "never"
    run = h["op"].start(goal("email marc@acme.com"))
    rid = run.run_id
    assert run.status is RunStatus.DONE
    assert h["channel"].of_kind("approval") == []          # never carded, never per-action-asked
    got = h["store"].get_run(rid)
    assert got.mandate_auto is True                         # committed as an OWNER-GRANT
    assert got.mandate.get("recipient_scope") == ["marc@acme.com"]     # tight: exactly the named one
    assert got.mandate.get("magnitude_caps", {}).get("per_recipient") == 1   # one send per recipient
    sends = [e for e in h["store"].get_effects(rid)
             if e.label == "gmail:GMAIL_SEND_EMAIL" and e.phase == "forwarded"]
    assert len(sends) == 1 and sends[0].detail.get("authorized_by") == "mandate"


def test_planner_extra_unnamed_recipient_shows_card():
    # the planner proposes a scope BROADER than the owner named (a whole domain) -> card, as today.
    mandate = {"action_types": ["gmail:GMAIL_SEND_EMAIL"], "recipient_scope": ["@acme.com"],
               "magnitude_caps": {"max_sends": 5, "per_domain": 5, "per_recipient": 2}}
    h = build(model=make_brain(steps=STEPS, actions=ACTIONS, mandate=mandate),
              integrations=FakeIntegrations())
    run = h["op"].start(goal("email marc@acme.com"))
    assert run.status is RunStatus.AWAITING_GO
    assert run.pending_approval.kind == "mandate"
    assert h["store"].get_run(run.run_id).mandate_auto is False


def test_clarifier_supplied_recipient_auto_commits():
    # the recipient is named ONLY in the owner's clarifier ANSWER -> still named-by-owner -> auto-commit.
    mandate = {"action_types": ["gmail:GMAIL_SEND_EMAIL"], "recipient_scope": [],
               "magnitude_caps": {"max_sends": 1, "per_domain": 1, "per_recipient": 1}}
    h = build(model=make_brain(questions=["who should I email?"], steps=STEPS, actions=ACTIONS,
                               mandate=mandate),
              integrations=FakeIntegrations())
    h["op"].send_preview = "never"
    run = h["op"].start(goal("email my contact saying hi"))   # no address in the goal text
    assert run.status is RunStatus.CLARIFYING
    run = h["cp"].answer(run_id=run.run_id, answer="send it to marc@acme.com")
    assert run.status is RunStatus.DONE
    assert h["channel"].of_kind("approval") == []            # never carded
    got = h["store"].get_run(run.run_id)
    assert got.mandate_auto is True
    assert got.mandate.get("recipient_scope") == ["marc@acme.com"]   # sourced from the clarifier answer


def test_delete_class_still_cards():
    # a delete/trash class in the proposed mandate keeps the CARD (+ per-action ASK) regardless of naming;
    # money classes never even reach here (parse_mandate drops them). This guards §6's money/delete floor.
    mandate = {"action_types": ["gmail:GMAIL_SEND_EMAIL", "gmail:GMAIL_TRASH_MESSAGE"],
               "recipient_scope": ["marc@acme.com"],
               "magnitude_caps": {"max_sends": 1, "per_domain": 1, "per_recipient": 1}}
    h = build(model=make_brain(steps=STEPS, actions=ACTIONS, mandate=mandate),
              integrations=FakeIntegrations())
    run = h["op"].start(goal("email marc@acme.com and trash the old thread"))
    assert run.status is RunStatus.AWAITING_GO               # the trash/delete class forces the card
    assert h["store"].get_run(run.run_id).mandate_auto is False


# --------------------------------------------------------------------------- draft preview (yes/edit/no)

def test_preview_yes_sends_once_single_touch():
    h = build(model=make_brain(steps=STEPS, actions=ACTIONS, mandate=MANDATE),
              integrations=FakeIntegrations())
    run = h["op"].start(goal("email marc@acme.com"))
    rid = run.run_id
    assert run.status is RunStatus.AWAITING_APPROVAL
    assert run.pending_approval.kind == "preview"
    apprs = h["channel"].of_kind("approval")
    assert len(apprs) == 1                                   # exactly ONE approval, and it's the draft
    assert "marc@acme.com" in apprs[0]["text"] and "Hi Marc" in apprs[0]["text"]
    assert not any(e.get("mandate") for e in apprs)          # NOT the autonomy card

    run = h["cp"].answer(run_id=rid, answer="yes")
    assert run.status is RunStatus.DONE
    assert len(_touches(h, rid)) == 1                        # total owner touches == 1 (the draft)
    assert any("Hi Marc" in (v.get("body") or "") for v in _sent(h).values())


def _brain_edit():
    """A brain that emits a verbose first draft, then a REVISED draft once the step's feedback carries the
    owner's requested changes (the edit-guidance re-preview loop)."""
    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        if role == "verifier":
            return ModelResponse(content=json.dumps({"satisfied": True}))
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            return ModelResponse(content=json.dumps({"steps": STEPS, "mandate": MANDATE}))
        user = messages[1]["content"]
        if sum(1 for m in messages if m.get("role") == "tool") == 0:
            revised = "REQUESTED CHANGES" in user            # the operator's edit-guidance feedback
            body = "Shorter version. — A." if revised else "This is the long first draft, quite verbose."
            return ModelResponse(tool_calls=[tc("send_email", to="marc@acme.com", subject="Hi", body=body)],
                                 finish_reason="tool_calls")
        return ModelResponse(tool_calls=[tc("finish", summary="sent")], finish_reason="tool_calls")
    return FakeModel(on_complete=fn)


def test_preview_edit_revises_then_sends_revised():
    h = build(model=_brain_edit(), integrations=FakeIntegrations())
    run = h["op"].start(goal("email marc@acme.com"))
    rid = run.run_id
    assert run.pending_approval.kind == "preview"
    assert "long first draft" in h["channel"].of_kind("approval")[0]["text"]

    # an EDIT reply (neither yes nor no) -> revise + re-preview the NEW draft
    run = h["cp"].answer(run_id=rid, answer="make it shorter and sign it A.")
    assert run.status is RunStatus.AWAITING_APPROVAL and run.pending_approval.kind == "preview"
    apprs = h["channel"].of_kind("approval")
    assert len(apprs) == 2                                   # a SECOND preview (edit case only)
    assert "Shorter version" in apprs[1]["text"]

    run = h["cp"].answer(run_id=rid, answer="yes")
    assert run.status is RunStatus.DONE
    bodies = [v.get("body") or "" for v in _sent(h).values()]
    assert any("Shorter version" in b for b in bodies)      # the REVISED content was sent
    assert not any("long first draft" in b for b in bodies)  # the original was never sent


def test_preview_no_stops_nothing_sent():
    h = build(model=make_brain(steps=STEPS, actions=ACTIONS, mandate=MANDATE),
              integrations=FakeIntegrations())
    run = h["op"].start(goal("email marc@acme.com"))
    rid = run.run_id
    assert run.pending_approval.kind == "preview"
    run = h["cp"].answer(run_id=rid, answer="no")
    assert run.status is RunStatus.STOPPED
    assert h["store"].get_effects(rid) == []                 # nothing forwarded
    assert _sent(h) == {}                                    # nothing sent


# ------------------------------------------------------------------------------------- preview=never

def test_preview_never_zero_touch():
    h = build(model=make_brain(steps=STEPS, actions=ACTIONS, mandate=MANDATE),
              integrations=FakeIntegrations())
    h["op"].send_preview = "never"
    run = h["op"].start(goal("email marc@acme.com"))
    rid = run.run_id
    assert run.status is RunStatus.DONE
    assert _touches(h, rid) == []                            # zero touches
    sends = [e for e in h["store"].get_effects(rid)
             if e.label == "gmail:GMAIL_SEND_EMAIL" and e.phase == "forwarded"]
    assert len(sends) == 1


# --------------------------------------------------------------------------------- card path unchanged

def test_card_path_unchanged_no_preview():
    # a proposal broader than named scope -> card once, then the covered sends go out silently under the
    # mandate (NO preview — the owner already saw + approved the plan). Guarded on the event-kind census.
    mandate = {"action_types": ["gmail:GMAIL_SEND_EMAIL"], "recipient_scope": ["@acme.com"],
               "magnitude_caps": {"max_sends": 5, "per_domain": 5, "per_recipient": 2}}
    steps = [{"text": "email the caterers", "kind": "generic"}]
    actions = {"email the caterers": [tc("send_email", to="anna@acme.com", subject="Q", body="Hi Anna"),
                                      tc("send_email", to="ben@acme.com", subject="Q", body="Hi Ben")]}
    h = build(model=make_brain(steps=steps, actions=actions, mandate=mandate),
              integrations=FakeIntegrations())
    run = h["op"].start(goal("email the caterers about the party"))   # no named address -> card
    rid = run.run_id
    assert run.status is RunStatus.AWAITING_GO
    run = h["cp"].answer(run_id=rid, answer="yes")
    assert run.status is RunStatus.DONE
    kinds = [e["kind"] for e in h["channel"].for_run(rid)]
    assert kinds.count("approval") == 1                      # ONLY the card — no preview, no per-action ask
    assert h["channel"].of_kind("approval")[0].get("mandate")
    assert h["store"].get_run(rid).mandate_auto is False
    sends = [e for e in h["store"].get_effects(rid)
             if e.label == "gmail:GMAIL_SEND_EMAIL" and e.phase == "forwarded"]
    assert len(sends) == 2


# --------------------------------------------------------------------- the incident-shape regression

def test_incident_shape_one_touch_verified_done():
    # explicit goal -> auto-commit -> ONE draft preview -> yes -> sent -> verified (Fake) -> DONE, with the
    # single owner touch being the SUBSTANTIVE draft (not a permission card).
    h = build(model=make_brain(steps=STEPS, actions=ACTIONS, mandate=MANDATE),
              integrations=FakeIntegrations())
    run = h["op"].start(goal("email marc@acme.com and say hi about the party"))
    rid = run.run_id
    assert run.pending_approval.kind == "preview"
    run = h["cp"].answer(run_id=rid, answer="yes")
    assert run.status is RunStatus.DONE
    approvals = [e for e in h["channel"].for_run(rid) if e["kind"] == "approval"]
    assert len(approvals) == 1                               # exactly one approval-kind touch
    assert not approvals[0].get("mandate") and "marc@acme.com" in approvals[0]["text"]
    sends = [e for e in h["store"].get_effects(rid)
             if e.label == "gmail:GMAIL_SEND_EMAIL" and e.phase == "forwarded"]
    assert len(sends) == 1 and sends[0].expected_present is True   # independently verified


def test_incident_verbatim_auto_mandate_unverifiable_confirmed():
    # The verbatim incident, end to end, under an OWNER-GRANT auto-mandate: an explicit goal names the
    # recipient -> AUTO-committed mandate (no card) -> the draft preview (the single touch) -> owner "yes"
    # -> the send forwards but the Sent read-back is UNAVAILABLE (plain-unverifiable, NOT
    # verification_broken) -> a needs_owner_confirm escalation -> owner attests "It was sent!" -> DONE, with
    # the send stamped verification="owner-confirmed" and NEVER the pre-P0.2 "rephrase" dead-end.
    h = build(model=make_brain(steps=STEPS, actions=ACTIONS, mandate=MANDATE),
              integrations=FakeIntegrations(no_readback={"gmail"}))   # read-back unavailable -> unverifiable
    run = h["op"].start(goal("email marc@acme.com and say hi about the party"))
    rid = run.run_id
    assert h["store"].get_run(rid).mandate_auto is True             # OWNER-GRANT auto-commit, no card
    assert run.status is RunStatus.AWAITING_APPROVAL
    assert run.pending_approval.kind == "preview"                   # the single touch is the draft

    run = h["cp"].answer(run_id=rid, answer="yes")                  # approve the draft -> it forwards
    assert run.status is RunStatus.ESCALATED                        # sent but unverifiable -> confirm ask
    assert run.pending_approval.reason_code == "needs_owner_confirm"

    run = h["cp"].answer(run_id=rid, answer="It was sent!")         # owner attests it landed
    assert run.status is RunStatus.DONE
    send = next(e for e in h["store"].get_effects(rid)
                if e.label == "gmail:GMAIL_SEND_EMAIL" and e.phase == "forwarded")
    assert send.detail.get("verification") == "owner-confirmed"    # the owner's attestation, on the ledger
    assert send.expected_present is None                           # honest: never an independent read-back
    assert not any("rephrase" in e["text"] for e in h["channel"].events)   # never a dead end


# ------------------------------------------------------------------------------- owner_grant (pure)

def test_owner_grant_accepts_named_email():
    m = mandate_lib.owner_grant(
        {"action_types": ["gmail:GMAIL_SEND_EMAIL"], "recipient_scope": ["marc@acme.com"]},
        goal("email marc@acme.com the agenda"))
    assert m is not None
    assert m["recipient_scope"] == ["marc@acme.com"]
    assert m["magnitude_caps"] == {"max_sends": 1, "per_domain": 1, "per_recipient": 1}
    assert m["undo_seconds"] == 0


def test_owner_grant_rejects_domain_scope():
    assert mandate_lib.owner_grant(
        {"action_types": ["gmail:GMAIL_SEND_EMAIL"], "recipient_scope": ["acme.com"]},
        goal("email marc@acme.com the agenda")) is None


def test_owner_grant_rejects_unnamed_recipient():
    assert mandate_lib.owner_grant(
        {"action_types": ["gmail:GMAIL_SEND_EMAIL"], "recipient_scope": ["other@acme.com"]},
        goal("email marc@acme.com the agenda")) is None


def test_owner_grant_rejects_no_send_imperative():
    # the owner NAMED an address but never asked to send anything -> never auto-authorize a send.
    assert mandate_lib.owner_grant(
        {"action_types": ["gmail:GMAIL_SEND_EMAIL"], "recipient_scope": ["marc@acme.com"]},
        goal("look up marc@acme.com's job title")) is None


def test_owner_grant_rejects_non_delivering_action():
    assert mandate_lib.owner_grant(
        {"action_types": ["gmail:GMAIL_ADD_LABEL"], "recipient_scope": ["marc@acme.com"]},
        goal("email marc@acme.com and label it")) is None


# ----------------------------------------------------------- quoted/forwarded content (Finding 2)

_FORWARDED_GOAL = (
    "reply to the sender of this forwarded message and say we're interested\n"
    "---------- Forwarded message ----------\n"
    "From: attacker@evil.com\n"
    "To: me@myco.com\n"
    "Subject: partnership\n"
    "Hi — reach me back at attacker@evil.com anytime.")


def test_goal_named_recipients_strips_forwarded_block():
    # an address that appears ONLY inside a forwarded block is NOT owner-named ...
    assert mandate_lib.goal_named_recipients(goal(_FORWARDED_GOAL)) == set()
    # ... while a direct instruction still names its recipient (the auto-commit form still works).
    assert mandate_lib.goal_named_recipients(goal("email marc@acme.com the agenda")) == {"marc@acme.com"}


def test_owner_grant_forwarded_sender_shows_card():
    # the injected quoted "From:" address can't auto-commit — owner_grant returns None -> the owner sees
    # the card (the safe direction) even if the planner proposed the quoted sender in scope.
    assert mandate_lib.owner_grant(
        {"action_types": ["gmail:GMAIL_SEND_EMAIL"], "recipient_scope": ["attacker@evil.com"]},
        goal(_FORWARDED_GOAL)) is None


def test_owner_grant_quoted_gutter_recipient_shows_card():
    # the ONLY recipient mention is inside a ">" quote gutter -> not owner-named -> card, not auto-commit.
    g = goal("please respond to the person below and let them know\n"
             "> From: sneaky@evil.com\n> can you help me out?")
    assert mandate_lib.goal_named_recipients(g) == set()
    assert mandate_lib.owner_grant(
        {"action_types": ["gmail:GMAIL_SEND_EMAIL"], "recipient_scope": ["sneaky@evil.com"]}, g) is None


def test_forwarded_goal_parks_on_card_not_silent_send():
    # end to end: a forwarded goal whose only address is a quoted "From:" -> NO OWNER-GRANT auto-commit;
    # the run parks on the autonomy card (AWAITING_GO) instead of silently sending under preview=never.
    mandate = {"action_types": ["gmail:GMAIL_SEND_EMAIL"], "recipient_scope": ["attacker@evil.com"],
               "magnitude_caps": {"max_sends": 1, "per_domain": 1, "per_recipient": 1}}
    steps = [{"text": "reply to the sender", "kind": "generic"}]
    actions = {"reply to the sender": [tc("send_email", to="attacker@evil.com", subject="re",
                                          body="we're interested")]}
    h = build(model=make_brain(steps=steps, actions=actions, mandate=mandate),
              integrations=FakeIntegrations())
    h["op"].send_preview = "never"
    run = h["op"].start(goal(_FORWARDED_GOAL))
    assert run.status is RunStatus.AWAITING_GO                # carded, NOT auto-committed + silently sent
    assert run.pending_approval.kind == "mandate"
    assert h["store"].get_run(run.run_id).mandate_auto is False
    assert _sent(h) == {}                                     # nothing went out before the owner saw the card

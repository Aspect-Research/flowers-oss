"""Part III #3 — Gmail organize (trash / add-label / server-side search).

Trash and add-label are ASK-tier REVERSIBLE writes that the gate verifies through an INDEPENDENT
read-back (the trashed message appears in TRASH; the labeled message appears in the label list), keyed
on the message id — the only identity the live tools carry. A trash/label that does NOT land is refused
through the real read-back path. Server-side search is an AUTO read. No hand-authored gate inputs.
"""

from __future__ import annotations

from flowers import effects, policy
from flowers import trustgate as g
from flowers.broker import Broker
from flowers.seams.integrations import FakeIntegrations, _parse_emails


def _gate_for(effect):
    unver, unverifiable = g.classify_effects([effect.as_gate_dict()], claimed_done=True)
    return g.gate_verdict(claimed_done=True, ok=True, stale_files=[], gate_breaking=[],
                          unverified_external=unver, unverifiable_external=unverifiable)


def _broker(integrations, **kw):
    return Broker(integrations=integrations, run_id="run_1", tenant_id="t1", **kw)


# ---- policy tiers (deterministic, no LLM) ----

def test_trash_is_ask_not_the_delete_never_floor():
    assert policy.classify("gmail", "GMAIL_TRASH_MESSAGE") == policy.ASK
    assert policy.classify("gmail", "GMAIL_DELETE_MESSAGE") == policy.NEVER   # permanent delete unchanged


def test_add_label_is_ask_and_search_is_auto():
    assert policy.classify("gmail", "GMAIL_ADD_LABEL") == policy.ASK
    assert policy.classify("gmail", "GMAIL_SEARCH_EMAILS") == policy.AUTO
    assert policy.is_side_effecting("gmail", "GMAIL_SEARCH_EMAILS") is False


# ---- trash: ASK + verified through an independent TRASH read-back ----

def test_trash_needs_approval_then_verifies_through_readback():
    b = _broker(FakeIntegrations())
    res = b.call_integration(toolkit="gmail", action="GMAIL_TRASH_MESSAGE",
                             params={"email_id": "msg_42"}, user_id="u1", authorized=False)
    assert res.status == "needs_approval" and res.approval.tier == "ask"

    res = b.call_integration(toolkit="gmail", action="GMAIL_TRASH_MESSAGE",
                             params={"email_id": "msg_42"}, user_id="u1", authorized=True)
    assert res.status == "ok" and res.effect.phase == "forwarded"
    assert res.effect.expected_present is True
    accept, _ = _gate_for(res.effect)
    assert accept is True


def test_trash_that_does_not_land_is_refused_by_gate():
    b = _broker(FakeIntegrations(drop_actions={("gmail", "GMAIL_TRASH_MESSAGE")}))
    res = b.call_integration(toolkit="gmail", action="GMAIL_TRASH_MESSAGE",
                             params={"email_id": "msg_42"}, user_id="u1", authorized=True)
    assert res.status == "ok" and res.effect.expected_present is False
    accept, reason = _gate_for(res.effect)
    assert accept is False and "not reflected" in reason


# ---- add-label: ASK + verified through the label's read-back ----

def test_add_label_verifies_through_readback():
    b = _broker(FakeIntegrations())
    res = b.call_integration(toolkit="gmail", action="GMAIL_ADD_LABEL",
                             params={"email_id": "msg_7", "label": "Receipts"},
                             user_id="u1", authorized=True)
    assert res.status == "ok" and res.effect.expected_present is True
    accept, _ = _gate_for(res.effect)
    assert accept is True


def test_add_label_that_does_not_land_is_refused():
    b = _broker(FakeIntegrations(drop_actions={("gmail", "GMAIL_ADD_LABEL")}))
    res = b.call_integration(toolkit="gmail", action="GMAIL_ADD_LABEL",
                             params={"email_id": "msg_7", "label": "Receipts"},
                             user_id="u1", authorized=True)
    assert res.effect.expected_present is False
    accept, _ = _gate_for(res.effect)
    assert accept is False


# ---- the message-id fingerprint must NOT false-verify via a concurrent/injected item ----
# (regression for the gate-weakening the adversarial review found: an opaque id matched a loose
# word-token subset across ALL fields, so a non-landing trash/label could be 'verified' by an
# unrelated email whose sender local-part or body merely contained the id.)

def test_id_fingerprint_not_spoofed_by_sender_localpart():
    target = "18a2f3b4c5d6e7f8"   # a real Gmail-shaped opaque id (the trash did NOT land)
    # the ONLY added item in the TRASH surface is a concurrent attacker email whose sender encodes the id
    after = _parse_emails({"emails": [{"id": "msg_999", "from_": f"{target}@attacker.com",
                                       "subject": f"re: {target}", "body": f"{target} please act"}]})
    assert effects.has_expected_effect({}, after, {"email_id": target}) is False


def test_id_fingerprint_not_spoofed_by_body_token():
    target = "msg_42"
    after = _parse_emails({"emails": [{"id": "msg_999", "from_": "x@y.com",
                                       "subject": "hi", "body": "msg_42 handle this"}]})
    assert effects.has_expected_effect({}, after, {"email_id": target}) is False


def test_id_fingerprint_verifies_the_real_message_by_whole_id():
    target = "18a2f3b4c5d6e7f8"
    after = _parse_emails({"emails": [{"id": target, "from_": "real@sender.com", "subject": "spam"}]})
    assert effects.has_expected_effect({}, after, {"email_id": target}) is True


def test_subject_freetext_matching_still_works_after_id_hardening():
    # a multi-word subject (the send/event case) still matches as a token subset, unaffected by the fix.
    after = {"k": {"to": "bob@acme.com", "subject": "Re: Venue inquiry for Friday"}}
    assert effects.has_expected_effect({}, after, {"to": "bob@acme.com", "subject": "Venue inquiry"}) is True


# ---- search: AUTO read, executes without approval ----

def test_search_executes_without_approval():
    b = _broker(FakeIntegrations())
    res = b.call_integration(toolkit="gmail", action="GMAIL_SEARCH_EMAILS",
                             params={"sender": "boss@acme.com", "label": "UNREAD"},
                             user_id="u1", authorized=False)
    assert res.status == "ok" and res.effect.side_effecting is False

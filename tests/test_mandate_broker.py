"""The Mandate at the broker level — widening authorization WITHOUT weakening verification.

These ride the SAME real read-back harness as test_broker.py (a FakeIntegrations whose ``drop_actions``
makes a send "succeed" but not land). The headline (invariant I6 / the named regression) proves a
mandate-AUTHORIZED send whose read-back shows it didn't land is STILL refused by the gate — i.e. the
mandate moved only the approval decision, never the verification.
"""

from __future__ import annotations

from flowers import policy
from flowers import trustgate as g
from flowers.broker import Broker
from flowers.seams.browser import FakeBrowser
from flowers.seams.integrations import FakeIntegrations

_MANDATE = {
    "action_types": ["gmail:GMAIL_SEND_EMAIL"],
    "recipient_scope": ["@acme.com"],
    "magnitude_caps": {"max_sends": 5, "per_domain": 5, "per_recipient": 2},
    "irreversibility_ceiling": "ASK",
    "done_definition": "",
}


def _broker(integrations, *, mandate=None, **kw):
    return Broker(integrations=integrations, run_id="run_1", mandate=mandate, **kw)


def _gate_for(effect):
    unver, unverifiable = g.classify_effects([effect.as_gate_dict()], claimed_done=True)
    return g.gate_verdict(claimed_done=True, ok=True, stale_files=[], gate_breaking=[],
                          unverified_external=unver, unverifiable_external=unverifiable)


# --------------------------------------------------------------------------- I6: the named regression

def test_mandate_authorized_nonlanding_send_still_refused_by_gate():
    """Widening authorization did NOT weaken verification. The send is auto-authorized by the mandate
    (no grant, not authorized), it forwards, but its read-back does not show it landed -> the gate
    refuses the claimed-done exactly as it would for an owner-approved send that didn't land."""
    b = _broker(FakeIntegrations(drop_actions={("gmail", "GMAIL_SEND_EMAIL")}), mandate=_MANDATE)
    res = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                             params={"to": "bob@acme.com", "subject": "Venue inquiry"},
                             user_id="u1", authorized=False)            # NOT owner-authorized
    assert res.status == "ok" and res.effect.phase == "forwarded"        # the mandate widened ok_auth
    assert res.effect.expected_present is False                          # read-back shows it didn't land
    accept, reason = _gate_for(res.effect)
    assert accept is False and "not reflected" in reason                 # the gate still refuses


# --------------------------------------------------------------------------- the happy path

def test_mandate_in_scope_send_auto_authorizes_and_counts():
    b = _broker(FakeIntegrations(), mandate=_MANDATE)
    res = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                             params={"to": "bob@acme.com", "subject": "Venue inquiry"},
                             user_id="u1", authorized=False)
    assert res.status == "ok" and res.effect.phase == "forwarded"
    assert res.effect.detail.get("authorized_by") == "mandate"          # audit-stamped
    assert b.mandate_counts["sends_total"] == 1
    assert b.mandate_counts["by_domain"]["acme.com"] == 1
    accept, _ = _gate_for(res.effect)
    assert accept is True                                               # and it verified normally


# --------------------------------------------------------------------------- the misses still park

def test_no_mandate_still_parks():
    b = _broker(FakeIntegrations())                                     # empty mandate == today
    res = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                             params={"to": "bob@acme.com", "subject": "s"}, user_id="u1")
    assert res.status == "needs_approval"


def test_out_of_scope_recipient_parks():
    b = _broker(FakeIntegrations(), mandate=_MANDATE)
    res = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                             params={"to": "attacker@evil.com", "subject": "s"}, user_id="u1")
    assert res.status == "needs_approval"                              # injected/out-of-scope -> ask


def test_never_tier_in_action_types_still_parks():
    # a NEVER action can't be mandate-covered even if a (sanitized-away) label slipped into action_types.
    mm = dict(_MANDATE, action_types=["gmail:GMAIL_DELETE_MESSAGE", "gmail:GMAIL_SEND_EMAIL"])
    b = _broker(FakeIntegrations(), mandate=mm)
    res = b.call_integration(toolkit="gmail", action="GMAIL_DELETE_MESSAGE",
                             params={"id": "m1"}, user_id="u1")
    assert res.status == "needs_approval" and res.approval.kind == "never"


def test_money_unreachable_by_mandate():
    # even a permissive mandate cannot reach a money action — is_refused short-circuits above ok_auth.
    mm = dict(_MANDATE, action_types=["stripe:STRIPE_CREATE_CHARGE"])
    b = _broker(FakeIntegrations(), mandate=mm)
    res = b.call_integration(toolkit="stripe", action="STRIPE_CREATE_CHARGE",
                             params={"amount": 100}, user_id="u1")
    assert res.status == "refused"
    assert policy.is_refused("stripe", "STRIPE_CREATE_CHARGE") is True


def test_per_recipient_cap_parks_the_overflow():
    b = _broker(FakeIntegrations(), mandate=dict(_MANDATE, magnitude_caps={"max_sends": 9,
                                                                           "per_domain": 9,
                                                                           "per_recipient": 2}))
    for i in range(2):
        res = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                                 params={"to": "bob@acme.com", "subject": f"n{i}"}, user_id="u1")
        assert res.status == "ok"
    res = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                             params={"to": "bob@acme.com", "subject": "n3"}, user_id="u1")
    assert res.status == "needs_approval"                              # 3rd to same recipient -> ask


def test_verified_identical_resend_is_idempotent_not_resent():
    """The no-double-send invariant: once a send VERIFIED-landed, a byte-identical resend (a replanned /
    re-attempted step re-issuing it, or an in-loop re-issue) is short-circuited as already-done — NOT
    re-executed and NOT re-prompted. One owner intent, exactly one delivery."""
    fake = FakeIntegrations()
    b = _broker(fake, mandate=_MANDATE)
    params = {"to": "bob@acme.com", "subject": "hi", "body": "x"}
    r1 = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL", params=params, user_id="u1")
    assert r1.status == "ok" and r1.effect.expected_present is True         # first send landed (verified)
    r2 = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                            params=dict(params), user_id="u1")
    assert r2.status == "ok"                                               # NOT a re-prompt
    assert r2.effect.detail.get("idempotent_replay") is True               # took the idempotency short-circuit
    assert r2.data.get("idempotent") is True
    assert len(fake.surface("u1", "sent")) == 1                            # exactly ONE delivery, never two


def test_mandate_dedupe_parks_unverified_identical_resend():
    """When the FIRST mandate-authorized send did NOT verify (read-back couldn't confirm it landed), the
    idempotency guard (verified-only) does not capture it, so a byte-identical resend falls through to the
    mandate's sent_digests dedupe -> a per-action approval (the owner decides whether to risk a duplicate),
    never a silent re-send."""
    b = _broker(FakeIntegrations(drop_actions={("gmail", "GMAIL_SEND_EMAIL")}), mandate=_MANDATE)
    params = {"to": "bob@acme.com", "subject": "hi", "body": "x"}
    r1 = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL", params=params, user_id="u1")
    assert r1.status == "ok" and r1.effect.expected_present is False        # forwarded but did NOT land
    r2 = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                            params=dict(params), user_id="u1")
    assert r2.status == "needs_approval"                                   # dedupe -> ask, not a silent re-send


def test_undo_window_queues_covered_send_then_grant_releases():
    mb = dict(_MANDATE, undo_seconds=30)
    b = _broker(FakeIntegrations(), mandate=mb)
    res = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                             params={"to": "bob@acme.com", "subject": "s"}, user_id="u")
    assert res.status == "needs_approval" and res.approval.kind == "undo"
    assert res.effect.phase == "deferred" and res.auto_release_seconds == 30   # queued, not sent
    # the SAME action with the issued grant present forwards (release path does NOT re-queue)
    res2 = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                              params={"to": "bob@acme.com", "subject": "s"}, user_id="u",
                              grants={res.grant_key})
    assert res2.status == "ok" and res2.effect.phase == "forwarded"
    assert res2.effect.detail.get("authorized_by") == "mandate"


def test_undo_window_off_by_default_forwards_immediately():
    b = _broker(FakeIntegrations(), mandate=_MANDATE)   # no undo_seconds -> immediate
    res = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                             params={"to": "bob@acme.com", "subject": "s"}, user_id="u")
    assert res.status == "ok" and res.effect.phase == "forwarded"


def test_learned_trust_auto_covers_non_delivering_class():
    b = Broker(integrations=FakeIntegrations(), run_id="r",
               trust={"googlecalendar:GOOGLECALENDAR_CREATE_EVENT": 5}, trust_threshold=5)
    res = b.call_integration(toolkit="googlecalendar", action="GOOGLECALENDAR_CREATE_EVENT",
                             params={"summary": "Pickup"}, user_id="u")
    assert res.status == "ok" and res.effect.phase == "forwarded"
    assert res.effect.detail.get("authorized_by") == "learned"


def test_learned_trust_below_threshold_still_parks():
    b = Broker(integrations=FakeIntegrations(), run_id="r",
               trust={"googlecalendar:GOOGLECALENDAR_CREATE_EVENT": 4}, trust_threshold=5)
    res = b.call_integration(toolkit="googlecalendar", action="GOOGLECALENDAR_CREATE_EVENT",
                             params={"summary": "Pickup"}, user_id="u")
    assert res.status == "needs_approval"


def test_learned_trust_never_covers_a_delivering_send():
    b = Broker(integrations=FakeIntegrations(), run_id="r",
               trust={"gmail:GMAIL_SEND_EMAIL": 99}, trust_threshold=5)
    res = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                             params={"to": "x@y.com", "subject": "s"}, user_id="u")
    assert res.status == "needs_approval"                     # delivering action never learned-covered


def test_learned_trust_never_covers_event_with_external_attendees():
    # the HIGH regression: a learned calendar class with EXTERNAL attendees fans out invites -> must park,
    # never auto-cover. A personal event (no attendees) of the same class still auto-covers.
    b = Broker(integrations=FakeIntegrations(), run_id="r",
               trust={"googlecalendar:GOOGLECALENDAR_CREATE_EVENT": 9}, trust_threshold=5)
    invite = b.call_integration(toolkit="googlecalendar", action="GOOGLECALENDAR_CREATE_EVENT",
                                params={"summary": "mtg", "attendees": ["attacker@evil.com"]}, user_id="u")
    assert invite.status == "needs_approval"                  # external invite -> asks
    personal = b.call_integration(toolkit="googlecalendar", action="GOOGLECALENDAR_CREATE_EVENT",
                                  params={"summary": "gym"}, user_id="u")
    assert personal.status == "ok" and personal.effect.detail.get("authorized_by") == "learned"


def test_learned_does_not_bypass_active_mandate_cap():
    # when the ACTIVE mandate governs a class, its magnitude cap binds — learned trust must NOT rescue a
    # cap-rejected call of that class.
    mb = {"action_types": ["googlecalendar:GOOGLECALENDAR_CREATE_EVENT"], "recipient_scope": [],
          "magnitude_caps": {"max_sends": 1, "per_domain": 1, "per_recipient": 1}}
    b = Broker(integrations=FakeIntegrations(), run_id="r", mandate=mb,
               trust={"googlecalendar:GOOGLECALENDAR_CREATE_EVENT": 9}, trust_threshold=5)
    first = b.call_integration(toolkit="googlecalendar", action="GOOGLECALENDAR_CREATE_EVENT",
                               params={"summary": "a"}, user_id="u")
    assert first.status == "ok" and first.effect.detail.get("authorized_by") == "mandate"
    second = b.call_integration(toolkit="googlecalendar", action="GOOGLECALENDAR_CREATE_EVENT",
                                params={"summary": "b"}, user_id="u")
    assert second.status == "needs_approval"                  # cap binds; learned does not rescue it


def test_browser_book_off_scope_host_parks_under_mandate():
    # I5 regression end-to-end: a mandate covering browser:BOOK to opentable.com must NOT auto-authorize a
    # BOOK whose url is an off-scope host — the frozen scope binds the browser last-mile, not just SUBMIT.
    bm = {"action_types": ["browser:BOOK"], "recipient_scope": ["opentable.com"],
          "magnitude_caps": {"max_sends": 5, "per_domain": 5, "per_recipient": 5}}
    b = Broker(browser=FakeBrowser(), run_id="r", mandate=bm)
    on = b.call_browser(action="BOOK", params={"url": "https://book.opentable.com/r/1"}, user_id="u")
    off = b.call_browser(action="BOOK", params={"url": "https://evil.com/r/9"}, user_id="u")
    assert on.status == "ok"                                  # in-scope host auto-authorized
    assert off.status == "needs_approval"                    # off-scope host still parks

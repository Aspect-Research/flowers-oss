"""The integrations seam — the fake's surfaces, read-back, fingerprints, and the gated Arcade adapter."""

from __future__ import annotations

from flowers.seams.integrations import (
    _ACTIONS,
    _ARCADE_TOOLS,
    CAPABILITY_CATALOG,
    ArcadeIntegrations,
    FakeIntegrations,
)


def test_actions_and_arcade_tools_keysets_match():
    # Every routable action must have BOTH a Fake recipe (_ACTIONS) and an Arcade recipe (_ARCADE_TOOLS),
    # so a half-wired orphan (present in one table, absent from the other -> a live "unknown action" path)
    # can never rot back in. A5 removed GMAIL_LIST_REPLIES / GMAIL_SEND_DRAFT / GOOGLECALENDAR_QUICK_ADD.
    assert set(_ACTIONS) == set(_ARCADE_TOOLS)


def test_catalog_actions_are_all_routable():
    # Every capability the planner/executor is TOLD about must be routable through the Arcade recipe
    # table — else the model could emit a label that dead-ends.
    catalog_keys = {(c["toolkit"], c["action"]) for c in CAPABILITY_CATALOG}
    assert catalog_keys <= set(_ARCADE_TOOLS)


def test_send_lands_in_sent_surface():
    fi = FakeIntegrations()
    before = fi.snapshot(toolkit="gmail", action="GMAIL_SEND_EMAIL", params={}, user_id="u1")
    res = fi.execute(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                     params={"to": "bob@acme.com", "subject": "Venue inquiry", "body": "hi"}, user_id="u1")
    after = fi.snapshot(toolkit="gmail", action="GMAIL_SEND_EMAIL", params={}, user_id="u1")
    assert res.ok and before == {} and len(after) == 1
    item = next(iter(after.values()))
    assert item["to"] == "bob@acme.com" and item["subject"] == "Venue inquiry"


def test_fingerprint_for_send():
    fi = FakeIntegrations()
    fp = fi.fingerprint(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                        params={"to": "bob@acme.com", "subject": "Venue inquiry", "body": "x"})
    assert fp == {"to": "bob@acme.com", "subject": "Venue inquiry"}


def test_dropped_action_does_not_land():
    fi = FakeIntegrations(drop_actions={("gmail", "GMAIL_SEND_EMAIL")})
    res = fi.execute(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                     params={"to": "bob@acme.com", "subject": "x"}, user_id="u1")
    after = fi.snapshot(toolkit="gmail", action="GMAIL_SEND_EMAIL", params={}, user_id="u1")
    assert res.ok is True            # the "provider" accepted it...
    assert after == {}               # ...but nothing landed (the fabricated-completion case)


def test_no_readback_toolkit_returns_none():
    # a toolkit with no independent read-back surface -> snapshot None -> the gate asks the owner. v1's
    # Gmail+Calendar writes are all read-back-verifiable, so this models the seam with a forced no_readback.
    fi = FakeIntegrations(no_readback={"gmail"})
    assert fi.snapshot(toolkit="gmail", action="GMAIL_SEND_EMAIL", params={}, user_id="u1") is None


def test_inbound_delivery_and_read():
    fi = FakeIntegrations()
    fi.deliver_inbound("u1", sender="bank@chase.com", subject="Your loan", body="approved")
    res = fi.execute(toolkit="gmail", action="GMAIL_FETCH_EMAILS", params={}, user_id="u1")
    assert res.ok
    items = list(res.data.values())
    assert any(it["from"] == "bank@chase.com" for it in items)


def test_unknown_action_fails():
    fi = FakeIntegrations()
    res = fi.execute(toolkit="frob", action="FROB_THING", params={}, user_id="u1")
    assert res.ok is False


def test_authorize_returns_url_when_unauthorized_and_completed_when_granted():
    # C1: the connect seam on the fake — a not-yet-connected toolkit returns (pending, url); a connected
    # one returns (completed, ""). (Full broker/operator round-trip lives in tests/test_connect.py.)
    fi = FakeIntegrations(unauthorized={"gmail"})
    status, url = fi.authorize("gmail", "u1")
    assert status == "pending" and url
    assert fi.authorize("googlecalendar", "u1") == ("completed", "")
    fi.grant("gmail")
    assert fi.authorize("gmail", "u1") == ("completed", "")


def test_arcade_unavailable_offline():
    a = ArcadeIntegrations()
    assert a.available() is False
    # offline (no injected client, no key) -> a structured failure, not a crash
    res = a.execute(toolkit="gmail", action="GMAIL_SEND_EMAIL", params={}, user_id="u1")
    assert res.ok is False
    # fingerprint is pure (no network) and works regardless of availability
    assert a.fingerprint(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                         params={"to": "a@b.com", "subject": "s"}) == {"to": "a@b.com", "subject": "s"}

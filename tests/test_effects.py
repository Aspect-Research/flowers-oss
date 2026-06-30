"""The verification math — read-back diff + fingerprint matching (the concurrent-drift defense)."""

from __future__ import annotations

from flowers import effects as e


def test_snapshot_diff_and_has_effect():
    before = {"m1": {"to": "a@x.com"}}
    after = {"m1": {"to": "a@x.com"}, "m2": {"to": "b@x.com"}}
    diff = e.snapshot_diff(before, after)
    assert diff["added"] == ["m2"] and diff["changed"] == [] and diff["removed"] == []
    assert e.has_effect(diff) is True
    assert e.has_effect(e.snapshot_diff(before, before)) is False


def test_expected_effect_matches_added_item():
    before = {}
    after = {"m2": {"to": "bob@acme.com", "subject": "Party venue inquiry"}}
    fp = {"to": "bob@acme.com", "subject": "Party venue inquiry"}
    assert e.has_expected_effect(before, after, fp) is True


def test_expected_effect_false_when_no_match_despite_drift():
    # A concurrent unrelated message arrived, but it is NOT our send -> expected effect did not land.
    before = {}
    after = {"x": {"from": "newsletter@spam.com", "subject": "Sale!"}}
    fp = {"to": "bob@acme.com", "subject": "Party venue inquiry"}
    assert e.has_expected_effect(before, after, fp) is False
    # ...yet has_effect would be True (something drifted) — proving why the fingerprint matters.
    assert e.has_effect(e.snapshot_diff(before, after)) is True


def test_address_must_equal_whole_field_not_substring():
    # The recipient merely quoted in a body must NOT verify the send (fail-closed).
    before = {}
    after = {"x": {"to": "someone@else.com", "body": "forward this to bob@acme.com please"}}
    fp = {"to": "bob@acme.com"}
    assert e.has_expected_effect(before, after, fp) is False


def test_freetext_token_not_substring():
    before = {}
    after = {"x": {"title": "Prefix work"}}
    assert e.has_expected_effect(before, after, {"title": "Fix"}) is False   # 'Fix' != token in 'Prefix'
    after2 = {"x": {"title": "Fix the venue booking"}}
    assert e.has_expected_effect(before, after2, {"title": "Fix venue"}) is True


def test_no_fingerprint_returns_none():
    assert e.has_expected_effect({}, {"m": {"a": 1}}, None) is None
    assert e.has_expected_effect({}, {"m": {"a": 1}}, {}) is None

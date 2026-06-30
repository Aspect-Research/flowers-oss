"""The Browser seam — FakeBrowser's scriptable world + the gated BrowserbaseBrowser slot.

These test the seam in isolation (the broker->gate trust path is exercised in test_browser_verification).
The point: the Fake can model every case the gate's cua branch must distinguish — a landed confirmation
observed independently, a submit that does NOT land, a surface with no independent observation, and a
non-independent (observer==actor) observation.
"""

from __future__ import annotations

import json

import pytest

from flowers import effects
from flowers.seams.browser import (
    BrowserbaseBrowser,
    FakeBrowser,
    _parse_observation,
    is_side_effecting_action,
)


def test_read_only_action_returns_page_text_no_confirmation():
    b = FakeBrowser(pages={"venue.com": "Booking page — pick a time"})
    res = b.act(action="navigate", params={"url": "https://venue.com/book"}, user_id="u1")
    assert res.ok and "Booking page" in res.text
    assert b.observe(action="navigate", params={}, user_id="u1") == {}   # nothing landed


def test_discrete_multistep_browse_navigate_then_extract():
    # Phase B: browse in DISCRETE steps across calls — navigate sets the current page, and a later
    # click/type/extract (no url) reads it. (Was a single one-shot goto+fill+click+read.)
    b = FakeBrowser(pages={"craigslist": "Couch $80 — pickup in Oakland"})
    nav = b.act(action="navigate", params={"url": "https://sfbay.craigslist.org/sof/123.html"}, user_id="u1")
    assert nav.ok and "Couch $80" in nav.text
    ext = b.act(action="extract", params={"selector": "body"}, user_id="u1")   # no url -> reads current page
    assert ext.ok and "Couch $80" in ext.text
    clk = b.act(action="click", params={"selector": "a.reply"}, user_id="u1")  # still the current page
    assert clk.ok and "Couch $80" in clk.text
    # none of the read-only ops landed a side effect
    assert b.observe(action="extract", params={}, user_id="u1") == {}


def test_submit_lands_a_confirmation_observed_independently():
    b = FakeBrowser()
    res = b.act(action="submit", params={"ref": "BK-42", "target": "venue.com"}, user_id="u1")
    assert res.ok and res.actor == "browser-session:u1"
    snap = b.observe(action="submit", params={"ref": "BK-42"}, user_id="u1")
    assert snap and any(v["confirmation"] == "BK-42" for v in snap.values())
    # the observer is a DISTINCT identity from the acting session (independent provenance)
    assert b.observer_id("u1") != res.actor


def test_dropped_submit_does_not_land():
    b = FakeBrowser(drop_actions=("submit",))
    b.act(action="submit", params={"ref": "BK-42"}, user_id="u1")
    assert b.observe(action="submit", params={"ref": "BK-42"}, user_id="u1") == {}


def test_no_observation_returns_none():
    b = FakeBrowser(no_observation=True)
    b.act(action="submit", params={"ref": "BK-42"}, user_id="u1")
    assert b.observe(action="submit", params={"ref": "BK-42"}, user_id="u1") is None


def test_self_sourced_observer_equals_actor():
    b = FakeBrowser(self_sourced=True)
    res = b.act(action="submit", params={"ref": "BK-42"}, user_id="u1")
    assert b.observer_id("u1") == res.actor   # NON-independent: the gate must reject this as self-report


def test_fingerprint_from_ref():
    b = FakeBrowser()
    assert b.fingerprint(action="submit", params={"ref": "BK-42"}) == {"confirmation": "BK-42"}
    assert b.fingerprint(action="submit", params={}) is None


def test_side_effecting_classification():
    assert is_side_effecting_action("submit") and is_side_effecting_action("BOOK")
    assert not is_side_effecting_action("navigate") and not is_side_effecting_action("extract")


def test_browserbase_unavailable_offline_and_refuses():
    bb = BrowserbaseBrowser()
    assert bb.available() is False                      # forced offline by conftest + no key/project
    with pytest.raises(RuntimeError):                   # refuses a network call while unavailable
        bb.act(action="submit", params={}, user_id="u1")
    with pytest.raises(RuntimeError):
        bb.observe(action="submit", params={}, user_id="u1")


# --- the LIVE adapter's pure observation parser (offline-testable bridge to the gate) ---

def test_parse_observation_webhook_requests_to_surface():
    token = "flowers-abc123"
    body = json.dumps({"data": [
        {"uuid": "r1", "method": "GET", "url": f"https://webhook.site/x?token={token}", "query": {"token": token}},
        {"uuid": "r2", "method": "GET", "url": "https://webhook.site/x", "query": {}},
    ]})
    surf = _parse_observation(body)
    assert set(surf) == {"r1", "r2"} and token in surf["r1"]["confirmation"]
    # and the gate's fingerprint matcher finds the token as an added item vs an empty before-snapshot
    assert effects.has_expected_effect({}, surf, {"confirmation": token}) is True
    assert effects.has_expected_effect({}, surf, {"confirmation": "flowers-NOTSENT"}) is False


def test_parse_observation_plaintext_and_empty():
    surf = _parse_observation("Confirmation ref ZZ-9 booked")
    assert len(surf) == 1 and next(iter(surf)).startswith("oob:")          # content-hashed key
    assert next(iter(surf.values())) == {"confirmation": "Confirmation ref ZZ-9 booked"}
    assert _parse_observation("   ") == {}
    assert next(iter(_parse_observation("not json {").values())) == {"confirmation": "not json {"}

"""Regressions for the adversarial-review findings. Each test names the fix
so a future change that reopens a hole fails loudly."""

from __future__ import annotations

import json

from flowers import effects
from flowers.broker import Broker
from flowers.seams.browser import (
    BrowserbaseBrowser,
    FakeBrowser,
    _observation_allowed,
    _parse_observation,
    _redact,
    _same_site,
)

# --- CRITICAL: the model-chosen observation must read the SAME site the action affected, never internal ---

def test_same_site_logic():
    assert _same_site("webhook.site", "webhook.site")
    assert _same_site("book.venue.com", "venue.com") and _same_site("venue.com", "book.venue.com")
    assert not _same_site("attacker.test", "venue.com")
    assert not _same_site("a.co.uk", "b.co.uk")          # NOT fooled by a shared public suffix
    assert not _same_site("", "venue.com")


def test_observation_allowed_rejects_cross_site_and_internal():
    # allowed: same host, public (literal IP -> no DNS in the test)
    assert _observation_allowed("https://1.1.1.1/requests", "https://1.1.1.1/book") is True
    # cross-site observer (the attack: point verification at an endpoint you control) -> rejected
    assert _observation_allowed("https://attacker.test/echo", "https://venue.test/book") is False
    # internal / SSRF hosts (same-site but private/link-local/loopback) -> rejected
    assert _observation_allowed("http://169.254.169.254/x", "http://169.254.169.254/y") is False
    assert _observation_allowed("http://127.0.0.1/x", "http://127.0.0.1/y") is False
    # missing action url -> cannot bind -> rejected
    assert _observation_allowed("https://venue.com/c", "") is False


# --- HIGH: a token past the cap (or a constant-keyed page) must not read as 'not landed' ---

def test_parse_observation_keeps_token_in_url_despite_huge_body():
    body = json.dumps({"data": [{"uuid": "r1", "method": "POST",
                                 "url": "https://w.site/x?ref=flowers-KEEP",
                                 "content": "x" * 60000, "query": {}}]})
    surf = _parse_observation(body)
    assert "flowers-KEEP" in surf["r1"]["confirmation"]   # url FIRST -> survives the 50k cap
    assert effects.has_expected_effect({}, surf, {"confirmation": "flowers-KEEP"}) is True


def test_parse_observation_oob_is_content_keyed_so_a_new_token_is_added_not_changed():
    before = _parse_observation("waiting for your confirmation")
    after = _parse_observation("Booking confirmed — ref flowers-KEEP")
    assert set(before) != set(after)                                  # content-hash key changes
    assert effects.has_expected_effect(before, after, {"confirmation": "flowers-KEEP"}) is True
    # and an UNCHANGED page (effect never landed) is correctly not-verified
    same = _parse_observation("waiting for your confirmation")
    assert effects.has_expected_effect(before, same, {"confirmation": "flowers-KEEP"}) is False


# --- MEDIUM: the connectUrl (embeds the live API key) must never leak in an error string ---

def test_redact_strips_connect_url_and_apikey():
    msg = "Error: connect_over_cdp wss://connect.browserbase.com/?apiKey=bb_live_SECRET&sessionId=1 failed"
    out = _redact(msg)
    assert "bb_live_SECRET" not in out and "wss://" not in out and "<redacted>" in out


# --- HIGH: the offline kill-switch must win even over explicitly-injected creds ---

def test_available_force_offline_beats_injected_creds():
    # conftest sets FLOWERS_FORCE_OFFLINE=1 for the whole suite
    assert BrowserbaseBrowser(api_key="x", project_id="y").available() is False


# --- the owner approval prompt must surface the verification channel (observe_url) ---

def test_approval_prompt_shows_the_verification_channel():
    res = Broker(browser=FakeBrowser(), run_id="r").call_browser(
        action="submit", params={"ref": "X", "url": "https://v.com/book", "observe_url": "https://v.com/confirm"},
        user_id="u1")
    assert res.status == "needs_approval"
    assert "verify via" in (res.approval.prompt or "") and "v.com/confirm" in res.approval.prompt

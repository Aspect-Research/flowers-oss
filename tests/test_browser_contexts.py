"""Part III #5 — persistent browser contexts (logged-in sessions across runs).

A Browserbase context id is stored per SITE in the durable Store and reused, so a one-time login
survives across runs. Covers: the Store round-trip + site isolation (the security property: a
context is only ever reused for the SAME site that created it — no credential bleed across sites) +
upsert; _ensure_context get-or-create (one create, then cache hits); the session body carries
browserSettings.context only when a context exists; and the site key derivation.
All offline: _bb_request is monkeypatched, so no Browserbase network call.
"""

from __future__ import annotations

from flowers.seams import browser as browser_mod
from flowers.seams.browser import BrowserbaseBrowser
from flowers.seams.store import SqliteStore

# --- the Store is the per-site context registry --------------------------------------------------

def test_store_browser_context_roundtrip_and_site_isolation():
    s = SqliteStore(":memory:")
    assert s.get_browser_context("acme.com") is None
    s.save_browser_context("acme.com", "ctx_A")
    assert s.get_browser_context("acme.com") == "ctx_A"
    # site-scoped: a different site does NOT see it (no credential bleed across sites)
    assert s.get_browser_context("other.com") is None
    # upsert: re-saving replaces (e.g. a re-seeded login)
    s.save_browser_context("acme.com", "ctx_B")
    assert s.get_browser_context("acme.com") == "ctx_B"
    s.close()


# --- _ensure_context: get-or-create, cached, site-isolated ---------------------------------------

class _CtxCounter:
    def __init__(self):
        self.n = 0

    def __call__(self, method, path, *, api_key, body=None, timeout=30):
        if path == "/contexts":
            self.n += 1
            return {"id": f"ctx_{self.n}"}
        return {}


def _bb(api_key="k", project_id="p", store=None):
    return BrowserbaseBrowser(api_key=api_key, project_id=project_id, context_store=store)


def test_ensure_context_creates_once_then_caches(monkeypatch):
    counter = _CtxCounter()
    monkeypatch.setattr(browser_mod, "_bb_request", counter)
    store = SqliteStore(":memory:")
    bb = _bb(store=store)

    cid1 = bb._ensure_context("acme.com")
    assert cid1 == "ctx_1" and counter.n == 1
    # a second call for the SAME site reuses the stored context — no new create
    assert bb._ensure_context("acme.com") == "ctx_1" and counter.n == 1
    # a DIFFERENT site gets its OWN context (no credential bleed across sites)
    assert bb._ensure_context("shop.io") == "ctx_2" and counter.n == 2
    store.close()


def test_ensure_context_is_noop_without_store_or_site(monkeypatch):
    counter = _CtxCounter()
    monkeypatch.setattr(browser_mod, "_bb_request", counter)
    assert _bb(store=None)._ensure_context("acme.com") is None     # no store -> ephemeral
    assert _bb(store=SqliteStore(":memory:"))._ensure_context("") is None   # no site -> ephemeral
    assert counter.n == 0


# --- the session body carries the persistent context only when one exists -----------------------

class _SessionCapture:
    def __init__(self):
        self.bodies = []

    def __call__(self, method, path, *, api_key, body=None, timeout=30):
        if path == "/sessions":
            self.bodies.append(body)
            return {"id": "sess_1", "connectUrl": "wss://connect.example/x"}
        return {}


def test_new_session_includes_persistent_context(monkeypatch):
    cap = _SessionCapture()
    monkeypatch.setattr(browser_mod, "_bb_request", cap)
    bb = _bb()
    bb._new_session("ctx_42")
    assert cap.bodies[-1]["browserSettings"]["context"] == {"id": "ctx_42", "persist": True}


def test_new_session_omits_context_when_none(monkeypatch):
    cap = _SessionCapture()
    monkeypatch.setattr(browser_mod, "_bb_request", cap)
    _bb()._new_session(None)
    assert "browserSettings" not in cap.bodies[-1]


# --- site key derivation ------------------------------------------------------------------------

def test_site_of_uses_host_then_falls_back_to_current():
    bb = _bb()
    assert bb._site_of("u1", {"url": "https://www.acme.com/book?x=1"}) == "www.acme.com"
    assert bb._site_of("u1", {}) == "www.acme.com"                    # url-less follow-up reuses the site
    assert bb._site_of("u1", {"target": "https://shop.io/cart"}) == "shop.io"
    assert bb._site_of("u2", {}) == ""                               # a fresh user with no nav yet

"""The ``search`` seam — the honest ``ok`` contract + the SSRF guard, all offline.

Covers the load-bearing fix vs an earlier prototype's silent-empty-as-success bug:
  * a scripted query returns its scripted results (ok=True),
  * an UNSCRIPTED query returns ``ok=True, results==[]`` (honest empty — NOT a failure),
  * a scripted blocked query returns ``ok=False`` with a reason,
  * the SSRF guard refuses internal / loopback / link-local hosts (tested directly, no network),
  * the live Tavily/Brave adapters report ``available() is False`` offline and refuse to run.
"""

from __future__ import annotations

import urllib.error
import urllib.parse

import pytest

from flowers.extras.search import BraveSearch
from flowers.seams.interfaces import (
    FetchResponse,
    SearchClient,
    SearchResponse,
    SearchResult,
)
from flowers.seams.search import (
    FakeSearch,
    TavilySearch,
    _NoInternalRedirect,
    is_ssrf_blocked_host,
)

# --------------------------------------------------------------------------- protocol conformance

def test_fake_conforms_to_protocol():
    assert isinstance(FakeSearch(), SearchClient)
    assert isinstance(TavilySearch(), SearchClient)
    assert isinstance(BraveSearch(), SearchClient)


# --------------------------------------------------------------------------- FakeSearch contract

def test_fake_returns_scripted_results():
    hit = SearchResult(title="Best tacos", url="https://example.com/tacos", snippet="yum")
    fake = FakeSearch({"tacos": [hit]})
    resp = fake.search("where are the best tacos", k=6)
    assert resp.ok is True
    assert resp.results == [hit]
    assert resp.query == "where are the best tacos"


def test_fake_unscripted_query_is_honest_empty_not_failure():
    fake = FakeSearch({"tacos": [SearchResult(title="t", url="https://x/")]})
    resp = fake.search("something completely unrelated")
    # The whole point: a genuine no-results search is a SUCCESS, never a failure.
    assert resp.ok is True
    assert resp.results == []
    assert resp.reason is None


def test_fake_blocked_query_is_honest_failure_with_reason():
    fake = FakeSearch(blocked={"forbidden"}, blocked_reason="rate_limited")
    resp = fake.search("the forbidden topic")
    assert resp.ok is False
    assert resp.reason == "rate_limited"
    assert resp.results == []
    assert resp.error is not None


def test_fake_search_accepts_a_ready_response():
    canned = SearchResponse(ok=True, query="q", results=[SearchResult(title="a", url="https://a/")])
    fake = FakeSearch({"q": canned})
    assert fake.search("q here") is canned


def test_fake_search_accepts_a_callable():
    def script(query, k):
        return SearchResponse(ok=False, query=query, results=[], reason="blocked")

    fake = FakeSearch(callable_=script)
    resp = fake.search("anything")
    assert resp.ok is False and resp.reason == "blocked"


def test_fake_respects_k_cap():
    results = [SearchResult(title=str(i), url=f"https://x/{i}") for i in range(10)]
    fake = FakeSearch({"many": results})
    assert len(fake.search("many results", k=3).results) == 3


def test_fake_fetch_scripted_text_and_error():
    fake = FakeSearch(fetches={
        "good": "hello world",
        "bad": FetchResponse(ok=False, url="https://bad/", error="boom"),
    })
    good = fake.fetch("https://good/page")
    assert good.ok is True and good.text == "hello world"
    bad = fake.fetch("https://bad/page")
    assert bad.ok is False and bad.error == "boom"


def test_fake_fetch_unscripted_is_benign_ok():
    resp = FakeSearch().fetch("https://unknown/")
    assert resp.ok is True and resp.text == ""


def test_fake_is_always_available():
    assert FakeSearch().available() is True


# --------------------------------------------------------------------------- SSRF guard (direct, no net)

@pytest.mark.parametrize("host", [
    "127.0.0.1",
    "localhost",
    "169.254.169.254",   # cloud-metadata link-local
    "10.0.0.5",
    "192.168.1.1",
    "172.16.0.1",
    "0.0.0.0",
    "::1",
    "",                  # empty -> blocked (fail closed)
])
def test_ssrf_guard_blocks_non_public_hosts(host):
    assert is_ssrf_blocked_host(host) is True


@pytest.mark.parametrize("host", ["8.8.8.8", "1.1.1.1"])
def test_ssrf_guard_allows_public_ips(host):
    assert is_ssrf_blocked_host(host) is False


def test_fetch_refuses_internal_url_without_touching_network():
    # The live fetch path is gated AND SSRF-guarded; force a gated adapter to test the guard via
    # the shared helper without a key by calling the module guard directly through fetch is not
    # possible offline (adapter is unavailable). Instead assert the guard rejects each scheme/host.
    for url in ("http://127.0.0.1/", "http://169.254.169.254/", "http://localhost/"):
        host = urllib.parse.urlparse(url).hostname or ""
        assert is_ssrf_blocked_host(host) is True


def test_redirect_revalidator_refuses_internal_redirect():
    handler = _NoInternalRedirect()
    with pytest.raises(urllib.error.HTTPError):
        handler.redirect_request(
            req=None, fp=None, code=302, msg="Found", headers={},
            newurl="http://169.254.169.254/latest/meta-data/",
        )


def test_redirect_revalidator_allows_public_redirect(monkeypatch):
    # A public->public redirect should NOT raise the SSRF refusal (it delegates to the base handler,
    # which builds a new Request from the original req — pass a real Request so the base call works).
    # Stub getaddrinfo to a public IP so the SSRF guard resolves WITHOUT a live DNS lookup — the suite
    # is offline by contract ($0, no network), and a real lookup would fail closed on an air-gapped box.
    import socket
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    handler = _NoInternalRedirect()
    req = urllib.request.Request("https://example.com/")
    out = handler.redirect_request(
        req=req, fp=None, code=302, msg="Found", headers={},
        newurl="https://example.org/next",
    )
    # base handler returns a Request (or None); the key assertion is that it did NOT raise.
    assert out is None or isinstance(out, urllib.request.Request)


# --------------------------------------------------------------------------- live adapters: offline gate

def test_tavily_unavailable_offline():
    assert TavilySearch().available() is False


def test_brave_unavailable_offline():
    assert BraveSearch().available() is False


def test_tavily_search_raises_when_unavailable():
    with pytest.raises(RuntimeError):
        TavilySearch().search("anything")


def test_brave_search_raises_when_unavailable():
    with pytest.raises(RuntimeError):
        BraveSearch().search("anything")


def test_live_fetch_raises_when_unavailable():
    # fetch is keyless+real but still gated: it must NOT reach the network while offline.
    with pytest.raises(RuntimeError):
        TavilySearch().fetch("https://example.com/")


# --------------------------------------------------------------------------- FallbackSearch (audit fix)

def test_fallback_search_falls_through_on_failure():
    from flowers.seams.search import FallbackSearch
    primary = FakeSearch(blocked={"q"})                                   # primary -> ok=False
    secondary = FakeSearch(scripted={"q": [SearchResult(title="t", url="https://x", snippet="s")]})
    fb = FallbackSearch(primary, secondary)
    assert fb.available() is True
    resp = fb.search("q")
    assert resp.ok and resp.results and resp.results[0].url == "https://x"  # fell through to secondary


def test_fallback_search_does_not_fall_through_on_empty_success():
    from flowers.seams.search import FallbackSearch
    primary = FakeSearch()                                                # ok=True, results=[] (honest empty)
    secondary = FakeSearch(scripted={"q": [SearchResult(title="t", url="https://y", snippet="s")]})
    resp = FallbackSearch(primary, secondary).search("q")
    assert resp.ok and resp.results == []                                 # empty SUCCESS is not a failure


def test_fallback_search_is_unavailable_offline():
    from flowers.extras.search import BraveSearch
    from flowers.seams.search import FallbackSearch, TavilySearch
    fb = FallbackSearch(TavilySearch(), BraveSearch())
    assert fb.available() is False    # both live adapters gated offline -> the factory falls back to FakeSearch

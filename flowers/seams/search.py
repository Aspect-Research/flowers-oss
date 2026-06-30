"""The ``search`` seam — web search + fetch behind the honest ``ok`` contract.

THE point of this seam (vs an earlier prototype's silent-empty-as-success bug): a blocked / rate-limited /
failed search returns ``SearchResponse(ok=False, reason=...)``; a *successful but empty* search
returns ``SearchResponse(ok=True, results=[])``. The executor can therefore tell "the tool failed,
circuit-break / switch provider" apart from "there genuinely are no hits, use what you have."

Implementations
---------------
  * :class:`FakeSearch`     — offline, scriptable; what the test suite and the engine use by default.
  * :class:`TavilySearch`   — live primary adapter (urllib, gated on ``TAVILY_API_KEY``); the wired live default.
  * :class:`FallbackSearch` — composes a primary + ordered fallbacks into one client (try each available
                              adapter in order, fall through on ``ok=False``). The optional ``BraveSearch``
                              fallback adapter lives in ``flowers/extras/search.py``; wrap it with
                              ``FallbackSearch(TavilySearch(), BraveSearch())`` to use it.

``fetch`` carries an SSRF guard ported verbatim from an earlier prototype's web client: it refuses
internal / private / link-local / metadata hosts and RE-validates on every redirect hop, so a
prompt-injected goal cannot read cloud metadata or hit an internal admin endpoint. Adapters never
touch the network when ``available()`` is False (offline by contract; see ``flowers.runtime``).
"""

from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable

from flowers import runtime
from flowers.seams.interfaces import FetchResponse, SearchResponse, SearchResult

_UA = "Mozilla/5.0 (compatible; FlowersOperator/1.0) Python-urllib"
_FETCH_CAP = 20_000          # chars of extracted text returned to the model (bounded prompt)
_MAX_FETCH_BYTES = 5 * 1024 * 1024   # cap the RAW body read so a giant/streamed page can't exhaust
#                                      memory or feed megabytes into the html-strip regexes (DoS)

_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_BLANKS = re.compile(r"\n\s*\n\s*\n+")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


# --------------------------------------------------------------------------- html -> text

def html_to_text(body: str) -> str:
    """Strip HTML to readable text (no bs4 dep): drop script/style, unwrap tags, unescape
    entities, collapse whitespace. Deterministic + pure."""
    if not body:
        return ""
    body = _SCRIPT_STYLE.sub(" ", body)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.IGNORECASE)
    body = re.sub(r"</(p|div|li|h[1-6]|tr)>", "\n", body, flags=re.IGNORECASE)
    body = _TAG.sub(" ", body)
    body = html.unescape(body)
    body = _WS.sub(" ", body)
    body = _BLANKS.sub("\n\n", body)
    return body.strip()


# --------------------------------------------------------------------------- SSRF guard
# Ported VERBATIM from an earlier prototype's web client — load-bearing security boundary.

def is_ssrf_blocked_host(host: str) -> bool:
    """True iff `host` resolves to (or IS) a NON-public address — loopback / private / link-local
    (incl. cloud-metadata 169.254.169.254) / reserved / multicast / unspecified. The urllib client
    runs IN the trusted operator process, so an unguarded fetch of an internal URL is an SSRF (read
    cloud metadata, hit an internal admin endpoint). A prompt-injected goal will try exactly this.
    Resolves ALL A/AAAA records and blocks if ANY is non-public (no DNS-rebinding single-record
    dodge). Fail CLOSED: an unresolvable host is blocked."""
    host = (host or "").strip().strip("[]")  # strip IPv6 brackets
    if not host:
        return True
    # A literal IP: check directly (don't resolve).
    try:
        addr = ipaddress.ip_address(host)
        return not addr.is_global
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return True  # unresolvable -> block (fail closed)
    for info in infos:
        ip = info[4][0]
        try:
            if not ipaddress.ip_address(ip).is_global:
                return True
        except ValueError:
            return True
    return False


class _NoInternalRedirect(urllib.request.HTTPRedirectHandler):
    """Re-validate every redirect hop against the SSRF guard, so a PUBLIC url cannot 30x-redirect
    into an internal/metadata address."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        host = urllib.parse.urlparse(newurl).hostname or ""
        if is_ssrf_blocked_host(host):
            raise urllib.error.HTTPError(newurl, code, "refused redirect to a non-public host",
                                         headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# --------------------------------------------------------------------------- shared fetch helper

def _guarded_fetch(url: str, *, timeout_s: float, opener: Callable | None = None) -> FetchResponse:
    """GET `url` behind the SSRF guard and return a :class:`FetchResponse` (HTML stripped, capped).

    Never raises: a refusal / transport error is a structured ``ok=False`` result. The redirect
    re-validator is wired into the opener so a public->internal redirect is also refused.
    """
    url = (url or "").strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        return FetchResponse(ok=False, url=url, error="only http(s) URLs may be fetched")
    if is_ssrf_blocked_host(urllib.parse.urlparse(url).hostname or ""):
        return FetchResponse(ok=False, url=url,
                             error="refused: that host is internal/non-public (SSRF guard)")
    _open = opener or urllib.request.build_opener(_NoInternalRedirect()).open
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with _open(req, timeout=timeout_s) as resp:  # type: ignore[arg-type]
            raw = resp.read(_MAX_FETCH_BYTES)  # bounded read — never buffer a giant/streamed body
            body = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            status = getattr(resp, "status", None) or getattr(resp, "code", 200) or 200
            status = int(status)
    except Exception as e:  # noqa: BLE001 - a fetch failure is a result, not a crash
        return FetchResponse(ok=False, url=url, error=f"{type(e).__name__}: {e}")
    title_m = _TITLE.search(body)
    title = html_to_text(title_m.group(1)) if title_m else url
    text = html_to_text(body)[:_FETCH_CAP]
    return FetchResponse(ok=status < 400, url=url, status=status, title=title, text=text,
                         error=None if status < 400 else f"HTTP {status}")


# --------------------------------------------------------------------------- Fake (offline)

# A scripted fetch value: either a FetchResponse, or a plain string (text of an ok page).
_FetchScript = FetchResponse | str
# A scripted search value: a list of results (ok=True), a SearchResponse, or a callable.
_SearchScript = list | SearchResult | SearchResponse | Callable


class FakeSearch:
    """Offline, scriptable :class:`SearchClient` — the engine/test default. ``available()`` is True.

    Scripting ``search``:
      * ``scripted={substr: [SearchResult, ...]}`` — a query CONTAINING ``substr`` returns those
        results with ``ok=True``. A value may also be a ready ``SearchResponse`` or a callable
        ``(query, k) -> SearchResponse`` for full control.
      * ``blocked={substr, ...}`` — a query containing any of these returns ``ok=False`` with a
        reason (simulates a block / rate-limit).
      * Anything unscripted returns the HONEST empty success: ``ok=True, results=[]`` (NOT a
        failure — that's the whole point of the seam).

    Scripting ``fetch``:
      * ``fetches={substr: FetchResponse | str}`` — a URL containing ``substr`` returns that
        response (a plain string becomes an ``ok=True`` page). Unscripted URLs return a benign
        empty ``ok=True`` page.
    """

    def __init__(
        self,
        scripted: dict[str, _SearchScript] | None = None,
        *,
        blocked: Iterable[str] | None = None,
        blocked_reason: str = "blocked",
        fetches: dict[str, _FetchScript] | None = None,
        callable_: Callable[[str, int], SearchResponse] | None = None,
    ) -> None:
        self.scripted: dict[str, _SearchScript] = dict(scripted or {})
        self.blocked: set[str] = set(blocked or ())
        self.blocked_reason = blocked_reason
        self.fetches: dict[str, _FetchScript] = dict(fetches or {})
        self._callable = callable_
        self.calls: list[tuple[str, int]] = []   # observability for tests

    def available(self) -> bool:
        return True

    def search(self, query: str, *, k: int = 6) -> SearchResponse:
        self.calls.append((query, k))
        q = query or ""
        # A whole-client callable wins (full control over the response).
        if self._callable is not None:
            return self._callable(q, k)
        # Blocked substrings -> honest failure with a reason.
        for sub in self.blocked:
            if sub in q:
                return SearchResponse(ok=False, query=q, results=[], reason=self.blocked_reason,
                                      error=f"simulated block for {sub!r}")
        # Scripted substrings -> the scripted result.
        for sub, val in self.scripted.items():
            if sub in q:
                return self._coerce_search(q, k, val)
        # Default: honest empty success (NOT a failure).
        return SearchResponse(ok=True, query=q, results=[])

    def fetch(self, url: str) -> FetchResponse:
        u = url or ""
        for sub, val in self.fetches.items():
            if sub in u:
                if isinstance(val, FetchResponse):
                    return val
                return FetchResponse(ok=True, url=u, status=200, title=u, text=str(val))
        return FetchResponse(ok=True, url=u, status=200, title=u, text="")

    @staticmethod
    def _coerce_search(query: str, k: int, val: _SearchScript) -> SearchResponse:
        if callable(val):
            return val(query, k)
        if isinstance(val, SearchResponse):
            return val
        if isinstance(val, SearchResult):
            return SearchResponse(ok=True, query=query, results=[val][:k])
        # Assume an iterable of SearchResult.
        results = list(val)[:k]
        return SearchResponse(ok=True, query=query, results=results)


# --------------------------------------------------------------------------- live adapters

class _LiveSearchBase:
    """Shared plumbing for the live adapters: availability gate, SSRF-guarded fetch, an
    availability assertion that turns an accidental live call (while offline) into a clear error
    instead of a silent network hit."""

    KEY_ENV = ""           # subclass sets this

    def __init__(self, *, timeout_s: float = 20.0) -> None:
        self.timeout_s = timeout_s

    def available(self) -> bool:
        return runtime.adapter_available(key_env=self.KEY_ENV)

    def _require_available(self) -> str:
        """Return the api key, or raise — NEVER touch the network when unavailable."""
        if not self.available():
            raise RuntimeError(
                f"{type(self).__name__} is unavailable (forced offline or {self.KEY_ENV} unset); "
                "refusing to make a network call"
            )
        return runtime.env(self.KEY_ENV)

    def fetch(self, url: str) -> FetchResponse:
        # fetch is keyless+real but still gated: never reach the network while unavailable.
        self._require_available()
        return _guarded_fetch(url, timeout_s=self.timeout_s)

    @staticmethod
    def _classify_http_status(status: int) -> str:
        if status == 429:
            return "rate_limited"
        if status in (401, 403):
            return "blocked"
        return "error"


class TavilySearch(_LiveSearchBase):
    """Live primary adapter — Tavily Search API (https://api.tavily.com/search), stdlib urllib only.

    On a non-200 response or any transport exception this returns ``ok=False`` with a reason
    (``rate_limited`` / ``blocked`` / ``error``) — NEVER ``ok=True`` with an empty list on failure.
    A genuine zero-hit success is ``ok=True, results=[]``.
    """

    KEY_ENV = "TAVILY_API_KEY"
    _ENDPOINT = "https://api.tavily.com/search"

    def search(self, query: str, *, k: int = 6) -> SearchResponse:
        api_key = self._require_available()
        q = (query or "").strip()
        if not q:
            return SearchResponse(ok=False, query=q, results=[], reason="error", error="empty query")
        payload = json.dumps({
            "api_key": api_key,
            "query": q,
            "max_results": int(k),
            "search_depth": "basic",
        }).encode("utf-8")
        req = urllib.request.Request(
            self._ENDPOINT, data=payload, method="POST",
            headers={"User-Agent": _UA, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                status = int(getattr(resp, "status", None) or getattr(resp, "code", 200) or 200)
                body = resp.read(_MAX_FETCH_BYTES).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return SearchResponse(ok=False, query=q, results=[],
                                  reason=self._classify_http_status(e.code),
                                  error=f"HTTP {e.code}: {e.reason}")
        except Exception as e:  # noqa: BLE001 - transport failure -> honest ok=False
            return SearchResponse(ok=False, query=q, results=[], reason="error",
                                  error=f"{type(e).__name__}: {e}")
        if status != 200:
            return SearchResponse(ok=False, query=q, results=[],
                                  reason=self._classify_http_status(status),
                                  error=f"HTTP {status}")
        try:
            data = json.loads(body)
        except Exception as e:  # noqa: BLE001
            return SearchResponse(ok=False, query=q, results=[], reason="error",
                                  error=f"bad json: {type(e).__name__}: {e}")
        results = [
            SearchResult(title=r.get("title", ""), url=r.get("url", ""),
                         snippet=r.get("content", "") or r.get("snippet", ""))
            for r in (data.get("results") or [])
            if r.get("url")
        ][:k]
        # A successful response with no hits is an HONEST empty success, not a failure.
        return SearchResponse(ok=True, query=q, results=results)


class FallbackSearch:
    """Compose a primary + ordered fallbacks into ONE ``SearchClient``. ``search`` tries each AVAILABLE
    adapter in order and returns the first ``ok=True`` response, falling through to the next ONLY when an
    adapter reports ``ok=False`` (blocked / rate-limited / error) — so a primary outage degrades to the
    fallback instead of failing the step. A genuine zero-hit success (``ok=True, results=[]``) is returned
    as-is and does NOT trigger fallback. ``available()`` is True iff ANY sub-adapter is available; ``fetch``
    uses the first available adapter. Wrapping ``TavilySearch`` with the optional ``BraveSearch`` adapter
    (flowers/extras/search.py) makes Brave a REAL fallback when Tavily is blocked/rate-limited. Offline
    (no keys) it is unavailable, so a builder can fall back to :class:`FakeSearch`."""

    def __init__(self, *adapters) -> None:
        self.adapters = [a for a in adapters if a is not None]

    def available(self) -> bool:
        return any(a.available() for a in self.adapters)

    def _live(self) -> list:
        return [a for a in self.adapters if a.available()]

    def search(self, query: str, *, k: int = 6) -> SearchResponse:
        live = self._live()
        if not live:
            return SearchResponse(ok=False, query=query or "", results=[], reason="error",
                                  error="no search adapter is available")
        last: SearchResponse | None = None
        for a in live:
            resp = a.search(query, k=k)
            if resp.ok:
                return resp
            last = resp
        return last  # every available adapter failed -> the last honest ok=False (reason preserved)

    def fetch(self, url: str) -> FetchResponse:
        live = self._live()
        if not live:
            raise RuntimeError("FallbackSearch is unavailable (no sub-adapter available); refusing to fetch")
        return live[0].fetch(url)

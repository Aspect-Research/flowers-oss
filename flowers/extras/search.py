"""Optional search adapter — Brave Web Search.

``BraveSearch`` is an optional adapter template (not wired into the default ``build_app``; the wired
default is ``TavilySearch`` / ``FakeSearch`` in ``flowers/seams/search.py``). It satisfies the same honest
``SearchClient`` contract as ``TavilySearch`` and is gated on ``BRAVE_API_KEY``. To use it as a fallback,
wrap it with ``FallbackSearch`` (kept in ``flowers/seams/search.py``), e.g.
``FallbackSearch(TavilySearch(), BraveSearch())``, and pass that to ``build_app``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from flowers.seams.interfaces import SearchResponse, SearchResult
from flowers.seams.search import _MAX_FETCH_BYTES, _UA, _LiveSearchBase


class BraveSearch(_LiveSearchBase):
    """Live fallback adapter — Brave Web Search API, stdlib urllib only.

    Same honest contract as :class:`TavilySearch`: non-200 / exception -> ``ok=False`` with a reason;
    zero hits -> ``ok=True, results=[]``.
    """

    KEY_ENV = "BRAVE_API_KEY"
    _ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

    def search(self, query: str, *, k: int = 6) -> SearchResponse:
        api_key = self._require_available()
        q = (query or "").strip()
        if not q:
            return SearchResponse(ok=False, query=q, results=[], reason="error", error="empty query")
        url = self._ENDPOINT + "?" + urllib.parse.urlencode({"q": q, "count": int(k)})
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA,
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                status = int(getattr(resp, "status", None) or getattr(resp, "code", 200) or 200)
                body = resp.read(_MAX_FETCH_BYTES).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return SearchResponse(ok=False, query=q, results=[],
                                  reason=self._classify_http_status(e.code),
                                  error=f"HTTP {e.code}: {e.reason}")
        except Exception as e:  # noqa: BLE001
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
        rows = ((data.get("web") or {}).get("results")) or []
        results = [
            SearchResult(title=r.get("title", ""), url=r.get("url", ""),
                         snippet=r.get("description", "") or r.get("snippet", ""))
            for r in rows
            if r.get("url")
        ][:k]
        return SearchResponse(ok=True, query=q, results=results)

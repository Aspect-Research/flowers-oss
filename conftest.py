"""Root conftest — the offline contract for the whole suite.

Two jobs, both load-bearing:

1. **Importability without install.** pytest (prepend import mode) puts the directory of this
   conftest on ``sys.path``, so ``import flowers`` works straight from the repo with no
   ``pip install -e .``. We also insert it explicitly, belt-and-suspenders.

2. **Keyless by contract.** The suite must NEVER make a live
   model / search / integration / sandbox call. We force ``FLOWERS_FORCE_OFFLINE=1`` and blank every
   known provider key for the whole session, so every real adapter's ``available()`` returns False
   and the engine falls back to the in-repo fakes the tests inject. A green suite is therefore a
   $0, no-network suite.
"""

from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Provider keys we explicitly neutralize so a developer's real ``.env`` can't make the suite spend.
# (The optional flowers/extras/ adapters — Brave, E2B, Langfuse, Postgres — are still offline-gated
# by their keys here, so their offline-discipline tests stay $0/no-network.)
_PROVIDER_KEYS = (
    "OPENROUTER_API_KEY",
    "TAVILY_API_KEY",
    "BRAVE_API_KEY",
    "ARCADE_API_KEY",
    "E2B_API_KEY",
    "BROWSERBASE_API_KEY",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "FLOWERS_USER_ID",   # not a key, but a real .env value that would skew user-identity assertions
)

# The opt-in LIVE layer (F1): under FLOWERS_LIVE=1 the live-adapter tests need REAL keys + the network,
# so the import-time blanking is lifted. The per-test fixture below STILL forces every non-`live`-marked
# test offline, so only `-m live` cases ever touch the network (a plain FLOWERS_LIVE run keeps the offline
# tests offline). Default (no FLOWERS_LIVE) = the strict $0/no-network contract, unchanged.
_LIVE = os.environ.get("FLOWERS_LIVE") == "1"

# Set at import time (before any test or fixture runs) — skipped under FLOWERS_LIVE so live keys survive.
if not _LIVE:
    os.environ["FLOWERS_FORCE_OFFLINE"] = "1"
    for _k in _PROVIDER_KEYS:
        os.environ.pop(_k, None)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _force_offline(request, monkeypatch):
    """Re-assert the offline contract per-test, so a test that sets a key cannot leak into another. A
    `live`-marked test under FLOWERS_LIVE=1 is exempt — it is the explicit opt-in to real keys + network."""
    if _LIVE and request.node.get_closest_marker("live") is not None:
        yield
        return
    monkeypatch.setenv("FLOWERS_FORCE_OFFLINE", "1")
    for k in _PROVIDER_KEYS:
        monkeypatch.delenv(k, raising=False)
    yield

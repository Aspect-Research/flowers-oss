"""Discipline guarantees — the offline contract and "wire it or delete it" made into assertions.

These pin the two project rules that keep flowers honest: (1) every LIVE adapter is gated and reports
unavailable offline (so the suite is $0/no-network and production is a clean config swap); (2) the
WIRED defaults the engine actually uses are available (no dormant machinery standing in for them).
The fabricated-completion-refused-through-the-production-path invariant is proven in test_broker.py,
test_operator.py, and test_e2e.py.
"""

from __future__ import annotations

from flowers.extras.sandbox import E2BSandbox
from flowers.extras.search import BraveSearch
from flowers.extras.telemetry import LangfuseTracer
from flowers.seams.integrations import ArcadeIntegrations, FakeIntegrations
from flowers.seams.model import OpenRouterModel
from flowers.seams.sandbox import LocalSubprocessSandbox
from flowers.seams.search import FakeSearch, TavilySearch
from flowers.seams.telemetry import LocalTracer, NoOpTracer


def test_live_adapters_are_gated_offline():
    assert OpenRouterModel().available() is False
    assert TavilySearch().available() is False
    assert BraveSearch().available() is False
    assert ArcadeIntegrations().available() is False
    assert E2BSandbox().available() is False
    assert LangfuseTracer().available() is False


def test_wired_defaults_are_available():
    assert FakeIntegrations().available() is True
    assert FakeSearch().available() is True
    assert NoOpTracer().available() is True
    assert LocalTracer().available() is True
    sb = LocalSubprocessSandbox()
    try:
        assert sb.available() is True
    finally:
        sb.close()

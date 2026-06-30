"""Pacing primitives — the circuit breaker that makes the 50-search thrash impossible."""

from __future__ import annotations

from flowers.engine.scheduler import CircuitBreaker, SemanticBudget


def test_circuit_breaker_trips_after_threshold():
    cb = CircuitBreaker(threshold=2)
    assert cb.tripped("web_search") is False
    cb.record("web_search", ok=False)
    assert cb.tripped("web_search") is False    # 1 failure
    cb.record("web_search", ok=False)
    assert cb.tripped("web_search") is True      # 2 consecutive -> tripped
    assert cb.total_failures("web_search") == 2


def test_success_resets_consecutive():
    cb = CircuitBreaker(threshold=2)
    cb.record("web_search", ok=False)
    cb.record("web_search", ok=True)             # resets
    cb.record("web_search", ok=False)
    assert cb.tripped("web_search") is False     # only 1 since reset
    assert cb.total_failures("web_search") == 2  # but total still counts both


def test_semantic_budget_defaults():
    b = SemanticBudget()
    assert b.max_searches == 8 and b.max_consecutive_failures == 2 and b.max_iterations == 16

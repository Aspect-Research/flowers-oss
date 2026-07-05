"""Pacing primitives — the mechanical antidote to thrash.

The executor and operator consume these: a per-step **semantic budget** (cap searches/iterations
ABOVE the dollar ceiling) and a per-tool **circuit breaker** (after K consecutive failures, stop
calling a broken tool and switch/escalate — never hammer it 50 times). Pure + trivially testable.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SemanticBudget:
    """Per-step caps enforced ABOVE the hard dollar ceiling, so a run never reaches budget exhaustion
    by thrashing (an unbounded executor can burn a whole budget re-searching the same dead end)."""
    max_iterations: int = 16          # executor tool-loop cap per step
    max_searches: int = 8             # searches per discovery step
    max_consecutive_failures: int = 2  # circuit-breaker threshold (per tool)
    max_tool_calls: int = 40          # absolute backstop on tool calls per step


class CircuitBreaker:
    """Tracks per-tool consecutive failures. After ``threshold`` consecutive failures on a tool, it
    is ``tripped`` — the executor must stop calling it (switch route / escalate), not keep hammering."""

    def __init__(self, threshold: int = 2):
        self.threshold = max(1, threshold)
        self._consecutive: dict[str, int] = {}
        self._total_failures: dict[str, int] = {}

    def record(self, tool: str, ok: bool) -> None:
        if ok:
            self._consecutive[tool] = 0
        else:
            self._consecutive[tool] = self._consecutive.get(tool, 0) + 1
            self._total_failures[tool] = self._total_failures.get(tool, 0) + 1

    def total_failures(self, tool: str) -> int:
        return self._total_failures.get(tool, 0)

    def tripped(self, tool: str) -> bool:
        return self._consecutive.get(tool, 0) >= self.threshold

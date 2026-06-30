"""The run engine — the methodical loop: clarify -> plan -> announce -> schedule -> execute -> gate.

This is the spine that turns a goal into a verified outcome without thrashing. The pacing primitives
(semantic budgets, per-tool circuit breaker, durable batch-and-wait) live here; the deterministic
trust gate (``flowers.trustgate``) adjudicates every claimed completion.
"""

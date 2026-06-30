"""The announcer — render the plan for the owner."""

from __future__ import annotations

from flowers.engine.announcer import announce_plan
from flowers.types import Plan, PlanStep, StepKind


def test_announce_formats_steps_deps_and_wait():
    plan = Plan(steps=[
        PlanStep(index=0, text="search venues"),
        PlanStep(index=1, text="email batch", depends_on=[0]),
        PlanStep(index=2, text="await replies", kind=StepKind.AWAIT_REPLIES, depends_on=[1],
                 params={"window_seconds": 86400, "min_replies": 2}),
    ], goal_text="organize the venue")
    text = announce_plan(plan)
    assert "1. search venues" in text
    assert "after 1" in text                 # step 2 depends on step 1 (1-based display)
    assert "wait for 2 reply" in text

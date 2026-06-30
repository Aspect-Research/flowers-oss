"""The planner — batch-structured DAG output, and the deterministic _parse_steps validation."""

from __future__ import annotations

import json

from flowers.engine.planner import Planner
from flowers.seams.interfaces import ModelResponse
from flowers.seams.model import FakeModel
from flowers.types import Goal, PlanStep, StepKind, StepStatus


class _Unavailable:
    """A model that is unavailable — forces the planner's fail-open single-task fallback."""
    def available(self):
        return False
    def complete(self, *a, **k):
        raise RuntimeError("should not be called")


def _model_returning(steps):
    payload = json.dumps({"steps": steps})
    return FakeModel(on_complete=lambda messages, tools, role: ModelResponse(content=payload))


def test_plan_builds_batch_structured_dag():
    steps = [
        {"text": "search a batch of venues", "kind": "generic"},
        {"text": "email the batch", "kind": "generic", "depends_on": [0]},
        {"text": "await replies", "kind": "await_replies", "depends_on": [1],
         "params": {"window_seconds": 86400, "min_replies": 1, "match": {"subject": "venue"}}},
        {"text": "book the venue", "kind": "generic", "depends_on": [2]},
    ]
    plan = Planner(_model_returning(steps)).plan(Goal(text="organize the venue"))
    assert len(plan.steps) == 4
    assert plan.steps[2].kind is StepKind.AWAIT_REPLIES
    assert plan.steps[2].params["min_replies"] == 1
    assert plan.steps[1].depends_on == [0]
    assert plan.ready_indices() == [0]   # only the first step is ready


def test_unavailable_model_falls_back_to_single_task():
    plan = Planner(_Unavailable()).plan(Goal(text="do the thing"))
    assert len(plan.steps) == 1 and plan.steps[0].text == "do the thing"


def test_parse_steps_drops_forward_and_self_deps():
    p = Planner(FakeModel([]))
    content = json.dumps({"steps": [
        {"text": "a"},
        {"text": "b", "depends_on": [0, 5, 1]},   # 5=forward, 1=self -> both dropped, keep 0
        {"text": "", "depends_on": [0]},           # empty text -> skipped
        "not a dict",                               # junk -> skipped
    ]})
    steps = p._parse_steps(content, "goal")
    assert [s.text for s in steps] == ["a", "b"]
    assert steps[1].depends_on == [0]


def test_parse_steps_caps_count():
    p = Planner(FakeModel([]), max_steps=3)
    content = json.dumps({"steps": [{"text": f"s{i}"} for i in range(20)]})
    assert len(p._parse_steps(content, "goal")) == 3


def test_parse_steps_junk_json_is_empty():
    assert Planner(FakeModel([]))._parse_steps("not json", "goal") == []


def test_replan_preserves_completed_work():
    done = [PlanStep(index=0, text="searched venues", status=StepStatus.DONE),
            PlanStep(index=1, text="emailed batch 1", status=StepStatus.DONE)]
    model = _model_returning([{"text": "email batch 2", "depends_on": [1]}])
    plan = Planner(model).replan(Goal(text="organize the venue"), done, reason="no replies")
    texts = [s.text for s in plan.steps]
    assert "searched venues" in texts and "emailed batch 1" in texts   # preserved
    assert plan.steps[0].status is StepStatus.DONE and plan.steps[1].status is StepStatus.DONE
    assert texts[-1] == "email batch 2"                                # new future step appended

"""The clarifier — ask the few load-bearing questions up front; fail-open to none."""

from __future__ import annotations

import json

from flowers.engine.clarifier import Clarifier
from flowers.seams.interfaces import ModelResponse
from flowers.seams.model import FakeModel
from flowers.types import Goal


class _Unavailable:
    def available(self):
        return False
    def complete(self, *a, **k):
        raise RuntimeError("nope")


def _model(qs):
    payload = json.dumps({"questions": qs})
    return FakeModel(on_complete=lambda messages, tools, role: ModelResponse(content=payload))


def test_returns_questions_capped():
    c = Clarifier(_model(["Budget?", "Location?", "Date?", "Size?", "Theme?"]), max_questions=4)
    out = c.clarify(Goal(text="organize a party"))
    assert out == ["Budget?", "Location?", "Date?", "Size?"][:4]
    assert len(out) == 4


def test_no_questions_when_actionable():
    assert Clarifier(_model([])).clarify(Goal(text="create hello.md")) == []


def test_disabled_returns_none():
    assert Clarifier(_model(["Budget?"]), enabled=False).clarify(Goal(text="x")) == []


def test_unavailable_model_returns_none():
    assert Clarifier(_Unavailable()).clarify(Goal(text="x")) == []

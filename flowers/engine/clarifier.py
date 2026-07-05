"""The clarifier — ask the few load-bearing questions UP FRONT, before planning.

An earlier prototype never did this: it planned immediately and discovered gaps mid-thrash. The clarifier inspects
the goal for missing load-bearing facts (location, date window, hard constraints, recipients) and returns
a SHORT batch of questions (capped). It is fail-open: an unavailable model or junk output -> no
questions (proceed). The owner can disable it ("just go with your best guess").
"""

from __future__ import annotations

import json

from flowers import memory as user_memory
from flowers.types import Goal

_CLARIFIER_SYSTEM = """You are the intake step of an autonomous operator. Before planning, identify ONLY
the load-bearing facts that are MISSING and would change the plan — for example a location, a date/time
window, a recipient, or a hard constraint the owner clearly cares about (which MAY include a price limit,
but only ask about cost when the goal is obviously about buying/booking something). If the goal is already
actionable, ask NOTHING. Do not default to asking about budget. Never ask more than 4 questions, and batch
them. Return ONLY JSON: {"questions": [str, ...]} (empty list if none)."""

_QUESTIONS_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "questions",
        "schema": {
            "type": "object",
            "properties": {"questions": {"type": "array", "items": {"type": "string"}}},
            "required": ["questions"],
        },
    },
}


class Clarifier:
    def __init__(self, model, *, max_questions: int = 4, enabled: bool = True):
        self.model = model
        self.max_questions = max_questions
        self.enabled = enabled

    def clarify(self, goal: Goal, *, broker=None, memory: str = "") -> list[str]:
        if not self.enabled:
            return []
        if not getattr(self.model, "available", lambda: False)():
            return []
        try:
            client = broker or self.model
            # Inject what we already know about the user so the clarifier does NOT re-ask it (the whole
            # point of cross-session memory — a returning user shouldn't be asked something we already know).
            blob = f"GOAL: {goal.text}" + user_memory.format_for_prompt(memory)
            resp = client.complete(
                [{"role": "system", "content": _CLARIFIER_SYSTEM},
                 {"role": "user", "content": blob}],
                role="planner", response_format=_QUESTIONS_RESPONSE_FORMAT)
            data = json.loads(resp.content)
        except Exception:
            return []
        qs = data.get("questions") if isinstance(data, dict) else None
        if not isinstance(qs, list):
            return []
        out = [str(q).strip() for q in qs if str(q).strip()]
        return out[: self.max_questions]

"""Per-user persistent memory (self-curated markdown carried ACROSS runs).

Covers the pure text helpers (append/dedup/cap/format), the Store round-trip, and the two engine seams:
the agent's `remember` tool persisting to the user's memory, and that memory being injected back into the
executor + planner prompts on a later run.
"""

from __future__ import annotations

import json

from _harness import build, make_brain, tc

from flowers import memory
from flowers.seams.interfaces import ModelResponse
from flowers.seams.model import FakeModel
from flowers.seams.store import SqliteStore
from flowers.types import Goal, RunStatus, ToolCall

# --- the pure helpers ---------------------------------------------------------------------------

def test_append_note_adds_a_bullet():
    md = memory.append_note("", "The user lives in Seattle.")
    assert memory.existing_notes(md) == ["The user lives in Seattle."]


def test_append_note_dedupes_case_insensitively():
    md = memory.append_note("", "Prefers morning meetings.")
    md = memory.append_note(md, "prefers MORNING meetings.")          # same fact, different case
    assert memory.existing_notes(md) == ["Prefers morning meetings."]


def test_append_note_collapses_whitespace_and_ignores_empty():
    md = memory.append_note("", "  multi\n   line   note  ")
    assert memory.existing_notes(md) == ["multi line note"]
    assert memory.append_note(md, "   ") == md                        # empty -> no churn


def test_cap_drops_oldest_and_keeps_newest():
    md = ""
    for i in range(400):                                              # blow well past the char cap
        md = memory.append_note(md, f"fact number {i} with some padding text to take up room")
    assert len(md) <= memory.MEMORY_CHAR_CAP
    notes = memory.existing_notes(md)
    assert any("fact number 399" in n for n in notes)                # newest survives
    assert not any("fact number 0 " in n for n in notes)             # oldest evicted


def test_format_for_prompt_empty_is_blank_then_present():
    assert memory.format_for_prompt("") == ""
    block = memory.format_for_prompt(memory.append_note("", "Allergic to peanuts."))
    assert "WHAT YOU KNOW ABOUT THIS USER" in block and "Allergic to peanuts." in block


# --- the Store round-trip -----------------------------------------------------------------------

def test_store_memory_roundtrip_and_default():
    s = SqliteStore()
    assert s.get_memory() == ""                                      # empty store -> empty, not error
    s.save_memory("# mem\n- hello\n")
    assert s.get_memory() == "# mem\n- hello\n"
    s.save_memory("# mem\n- updated\n")                              # upsert (singleton row)
    assert s.get_memory() == "# mem\n- updated\n"


# --- the engine seams ---------------------------------------------------------------------------

def test_remember_tool_persists_to_user_memory():
    model = make_brain(steps=[{"text": "greet the user"}],
                       actions={"greet the user": [
                           tc("remember", note="The user prefers to be called Asa.")]})
    h = build(model=model)
    run = h["op"].start(Goal(text="say hi"))
    assert run.status is RunStatus.DONE
    assert "prefers to be called Asa" in h["store"].get_memory()


def test_memory_is_injected_into_a_later_run():
    seen: dict[str, str] = {}

    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        if role == "verifier":
            return ModelResponse(content=json.dumps({"satisfied": True}))
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            seen["plan_blob"] = messages[1]["content"]
            return ModelResponse(content=json.dumps({"steps": [{"text": "do it"}]}))
        seen["exec_blob"] = messages[1]["content"]                   # the executor's user prompt
        return ModelResponse(tool_calls=[ToolCall(name="finish", args={"summary": "done"})],
                             finish_reason="tool_calls")

    h = build(model=FakeModel(on_complete=fn))
    h["store"].save_memory(memory.append_note("", "The user's partner is named Sam."))
    run = h["op"].start(Goal(text="hello"))
    assert run.status is RunStatus.DONE
    assert "partner is named Sam" in seen["exec_blob"]               # injected into execution
    assert "WHAT YOU KNOW ABOUT THIS USER" in seen["exec_blob"]
    assert "partner is named Sam" in seen["plan_blob"]               # AND into planning


def test_memory_is_injected_into_the_clarifier():
    # the clarifier is the component that ASKS — it must see what we know so it doesn't re-ask it.
    from flowers.engine.clarifier import Clarifier

    seen: dict[str, str] = {}

    def fn(messages, tools, role):
        seen["blob"] = messages[1]["content"]
        return ModelResponse(content=json.dumps({"questions": []}))

    Clarifier(FakeModel(on_complete=fn)).clarify(
        Goal(text="book a table"),
        memory=memory.append_note("", "The user's usual party size is 4."))
    assert "usual party size is 4" in seen["blob"]


def test_memory_is_injected_into_replan():
    # the await->next-batch replan revises the FUTURE plan mid-run; it must also see what we know.
    from flowers.engine.planner import Planner
    from flowers.types import PlanStep

    seen: dict[str, str] = {}

    def fn(messages, tools, role):
        seen["blob"] = messages[1]["content"]
        return ModelResponse(content=json.dumps({"steps": [{"text": "next batch"}]}))

    planner = Planner(FakeModel(on_complete=fn))
    planner.replan(Goal(text="outreach"), [PlanStep(index=0, text="first batch")],
                   reason="no replies", memory=memory.append_note("", "The user is a florist in Seattle."))
    assert "florist in Seattle" in seen["blob"]


def test_fresh_user_adds_no_memory_noise():
    seen: dict[str, str] = {}

    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            return ModelResponse(content=json.dumps({"steps": [{"text": "do it"}]}))
        seen["exec_blob"] = messages[1]["content"]
        return ModelResponse(tool_calls=[ToolCall(name="finish", args={"summary": "done"})],
                             finish_reason="tool_calls")

    h = build(model=FakeModel(on_complete=fn))
    run = h["op"].start(Goal(text="hello"))
    assert run.status is RunStatus.DONE
    assert "WHAT YOU KNOW ABOUT THIS USER" not in seen["exec_blob"]  # no empty section for a new user

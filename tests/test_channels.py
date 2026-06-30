"""Channel contract — the answer parser and the in-proc channel."""

from __future__ import annotations

from flowers.channels.base import parse_answer
from flowers.channels.inproc import InProcChannel


def test_parse_answer_yes_no_other():
    assert parse_answer("yes")["decision"] == "yes"
    assert parse_answer("Approve!")["decision"] == "yes"
    assert parse_answer("do it please")["decision"] == "yes"
    assert parse_answer("no")["decision"] == "no"
    assert parse_answer("decline this")["decision"] == "no"
    assert parse_answer("maybe later, what's the cost?")["decision"] == "other"


def test_parse_answer_preserves_text():
    assert parse_answer("the budget is $50")["text"] == "the budget is $50"
    assert parse_answer("the budget is $50")["decision"] == "other"


def test_inproc_channel_collects_events():
    ch = InProcChannel()
    ch.emit({"run_id": "r1", "kind": "plan_announce", "text": "plan"})
    ch.emit({"run_id": "r1", "kind": "done", "text": "report"})
    ch.emit({"run_id": "r2", "kind": "progress", "text": "x"})
    assert [e["kind"] for e in ch.for_run("r1")] == ["plan_announce", "done"]
    assert ch.of_kind("done")[0]["text"] == "report"


def test_inproc_channel_callback():
    seen = []
    ch = InProcChannel(on_event=seen.append)
    ch.emit({"run_id": "r", "kind": "notify", "text": "hi"})
    assert seen and seen[0]["kind"] == "notify"

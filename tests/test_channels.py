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


def test_parse_answer_natural_affirmatives():
    # natural approval phrases a person texts to a draft preview all parse yes (no needless revise round).
    for phrase in ("sounds good", "looks good", "looks great", "perfect", "great",
                   "ship it", "lgtm", "go for it", "all good", "Perfect!", "Sounds good."):
        assert parse_answer(phrase)["decision"] == "yes", phrase


def test_parse_answer_thumbs_up_variants():
    # a bare thumbs-up (any skin tone / with-or-without variation selector, one or more) is yes.
    for phrase in ("\U0001F44D", "\U0001F44D️", "\U0001F44D\U0001F3FD",
                   "\U0001F44D\U0001F3FF", "\U0001F44D\U0001F44D"):
        assert parse_answer(phrase)["decision"] == "yes", repr(phrase)


def test_parse_answer_bare_declines():
    # only a bare decline parses no.
    for phrase in ("no", "nope", "nah", "n", "no thanks", "no thank you",
                   "stop", "cancel", "don't", "decline", "abort"):
        assert parse_answer(phrase)["decision"] == "no", phrase


def test_parse_answer_no_prefixed_edit_falls_to_other():
    # a "no ..."-prefixed reply that is EDIT GUIDANCE (not a bare decline) must NOT stop — it falls to
    # "other" so a draft preview REVISES instead of dead-ending the run.
    for phrase in ("no, mention the deadline instead", "no, make it more formal",
                   "not now", "no way that's too long"):
        assert parse_answer(phrase)["decision"] == "other", phrase


def test_parse_answer_no_change_idiom_flips_to_yes():
    # an EXACT no-change idiom (± a send tail) is APPROVAL, not a decline.
    for phrase in ("no need to change it, send", "no changes — send it",
                   "no changes - send it", "no edits, send it.", "no changes needed",
                   "nothing to change, send it"):
        assert parse_answer(phrase)["decision"] == "yes", phrase


def test_parse_answer_mixed_edit_plus_send_never_flips_to_yes():
    # a reply that asks for an EDIT and then says send must NOT approve the OLD draft — unapproved words
    # never go out. It falls to "other" so the preview revises (the revised draft gets its own preview).
    for phrase in ("no, make it shorter and send it", "no — fix the typo then send",
                   "no need to change the subject, just fix the body",
                   "no changes to the intro but tighten the ending, send it"):
        assert parse_answer(phrase)["decision"] == "other", phrase


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

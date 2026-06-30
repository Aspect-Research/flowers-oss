"""Part II — richer bounded watch conditions (count / number_near / changed), all LINEAR (no model-
authored regex, preserving the no-ReDoS invariant that keeps the single-threaded tick loop safe)."""

from __future__ import annotations

from flowers.engine.operator import Operator


def _c(text, match):
    return Operator._text_condition(text, match)


def test_contains_absent_unchanged():
    assert _c("tickets available now", {"contains": "tickets"}) == ["match"]
    assert _c("sold out", {"contains": "tickets"}) == []
    assert _c("in stock", {"absent": "sold out"}) == ["match"]
    assert _c("sold out", {"absent": "sold out"}) == []


def test_count_bounds():
    text = "PR opened. PR opened. PR opened."
    assert _c(text, {"count": {"of": "PR opened", "at_least": 3}}) == ["match"]
    assert _c(text, {"count": {"of": "PR opened", "at_least": 4}}) == []
    assert _c(text, {"count": {"of": "PR opened", "at_most": 3}}) == ["match"]
    assert _c(text, {"count": {"of": "PR opened", "at_most": 2}}) == []


def test_number_near():
    assert _c("Price: $49.99 today", {"number_near": {"anchor": "$", "at_most": 50}}) == ["match"]
    assert _c("Price: $59.99 today", {"number_near": {"anchor": "$", "at_most": 50}}) == []
    assert _c("Tickets: 5 left", {"number_near": {"anchor": "Tickets:", "at_least": 3}}) == ["match"]
    assert _c("Tickets: 2 left", {"number_near": {"anchor": "Tickets:", "at_least": 3}}) == []
    assert _c("Total 1,234 items", {"number_near": {"anchor": "Total", "equals": 1234}}) == ["match"]
    assert _c("no price here", {"number_near": {"anchor": "$", "at_most": 50}}) == []   # not found -> closed


def test_number_after_is_bounded_and_linear():
    # a huge text must not hang (linear find + a FIXED look-ahead window), and a number past the window
    # is not picked up.
    assert Operator._number_after("x" * 100000 + "$42", "$") == 42.0
    assert Operator._number_after("anchor" + " " * 1000 + "5", "anchor") is None
    assert Operator._number_after("anchor 7 then", "anchor") == 7.0
    assert Operator._number_after("plain text", "missing") is None


def test_fail_closed_on_empty_or_unknown():
    assert _c("anything", {}) == []
    assert _c("anything", {"unknown_key": 1}) == []
    assert _c("anything", {"pattern": ".*"}) == []   # a regex 'pattern' is NOT honored (ReDoS guard)


def test_malformed_numeric_bounds_fail_closed_never_raise():
    # model-authored junk bounds must NOT raise (a ValueError out of the single-threaded tick loop would
    # abort the whole due-batch — a cross-tenant DoS). They fail closed (condition not met).
    assert _c("price $40", {"number_near": {"anchor": "$", "at_most": "fifty"}}) == []
    assert _c("price $40", {"number_near": {"anchor": "$", "at_most": None}}) == []
    assert _c("a a a", {"count": {"of": "a", "at_least": "two"}}) == []
    assert _c("a a a", {"count": {"of": "a", "at_least": None}}) == []
    # a VALID bound still works after the coercion change
    assert _c("price $40", {"number_near": {"anchor": "$", "at_most": 50}}) == ["match"]
    assert _c("a a a", {"count": {"of": "a", "at_least": 3}}) == ["match"]


def test_and_semantics_all_conditions_hold():
    assert _c("in stock, only 2 left",
              {"contains": "in stock", "number_near": {"anchor": "only", "at_most": 5}}) == ["match"]
    assert _c("in stock, 9 left",     # anchor "only" absent -> number_near fails -> overall fail
              {"contains": "in stock", "number_near": {"anchor": "only", "at_most": 5}}) == []

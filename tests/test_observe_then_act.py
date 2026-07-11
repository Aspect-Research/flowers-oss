"""Observe-then-act (the Stagehand-style preview) + the resolved-action approval.

The model INSPECTs a page to get its actionable elements (by label + selector) before a side-effecting
submit, so it targets the exact control instead of guessing CSS — and the owner approves a plain-English
`describe` of the resolved action, not an opaque selector. Read-only inspect, deterministic submit.
"""

from __future__ import annotations

from _harness import build, make_brain, tc

from flowers.broker import Broker
from flowers.seams.browser import FakeBrowser
from flowers.types import Goal, RunStatus

# --- the inspect read returns candidate elements ------------------------------------------------

def test_fake_inspect_returns_candidates_for_the_current_page():
    br = FakeBrowser(elements={"opentable": [
        {"ref": "e0", "label": "Place reservation", "selector": "#book"},
        {"ref": "e1", "label": "Cancel", "selector": "#cancel"}]})
    br.act(action="navigate", params={"url": "https://opentable.com/r/otto"}, user_id="u1")
    res = br.act(action="inspect", params={}, user_id="u1")          # reads the CURRENT page
    assert res.ok and [e["label"] for e in res.elements] == ["Place reservation", "Cancel"]


def test_inspect_is_read_only_and_passes_elements_through_the_broker():
    br = FakeBrowser(elements={"": [{"ref": "e0", "label": "Submit booking", "selector": "#go"}]})
    b = Broker(browser=br, run_id="r")
    b.call_browser(action="navigate", params={"url": "https://x.com"}, user_id="u1")
    res = b.call_browser(action="inspect", params={}, user_id="u1")
    assert res.status == "ok" and res.effect.side_effecting is False  # a read, no approval
    assert res.data["elements"][0]["label"] == "Submit booking"


# --- the resolved action is what the owner approves ---------------------------------------------

def test_describe_leads_the_approval_prompt():
    b = Broker(browser=FakeBrowser(), run_id="r")
    res = b.call_browser(action="submit", user_id="u1",
                         params={"ref": "BK-1", "target": "opentable.com",
                                 "describe": "Book a table for 2 at 7pm at Otto"})
    assert res.status == "needs_approval"
    assert res.approval.prompt.startswith("Book a table for 2 at 7pm at Otto")   # the exact action, first
    assert "browser:submit" not in res.approval.prompt                          # no slug in the human prompt
    assert res.approval.effect_label == "browser:submit"                        # identity kept on the record


# --- end-to-end: inspect -> submit(describe) -> approve -> land ----------------------------------

def test_observe_then_act_loop_end_to_end():
    model = make_brain(steps=[{"text": "book the table"}], actions={"book the table": [
        tc("browser", action="inspect", params={}),
        tc("browser", action="submit",
           params={"ref": "BK-1", "target": "x.com", "describe": "Submit the booking on x.com"})]})
    br = FakeBrowser(elements={"": [{"ref": "e0", "label": "Book now", "selector": "#book"}]})
    h = build(model=model, browser=br)

    run = h["op"].start(Goal(text="book"))
    assert run.status is RunStatus.AWAITING_APPROVAL
    assert "Submit the booking on x.com" in run.pending_approval.prompt          # owner sees the real action
    run = h["cp"].answer(run_id=run.run_id, answer="yes")
    assert run.status is RunStatus.DONE
    assert br.observe(action="submit", params={"ref": "BK-1"}, user_id="local")  # the approved action landed

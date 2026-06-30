"""The operator spine, end to end over fakes — clarify, plan, approve, verify, await, monitor.

These drive the REAL Operator/ControlPlane with scripted seams. The headline guarantees:
  * a fabricated (non-landing) completion is REFUSED through the production path (never DONE);
  * an await step never completes on an unverified inbound; a monitor notifies on a verified match.
"""

from __future__ import annotations

import json

from flowers.controlplane import ControlPlane
from flowers.engine.operator import Operator
from flowers.seams.integrations import FakeIntegrations
from flowers.seams.interfaces import ModelResponse
from flowers.seams.model import FakeModel
from flowers.seams.search import FakeSearch
from flowers.seams.store import SqliteStore
from flowers.seams.timers import LocalTimers
from flowers.types import Goal, RunStatus, ToolCall


class _Channel:
    def __init__(self):
        self.events = []
    def emit(self, event):
        self.events.append(event)


def _brain(*, questions=None, steps=None, actions=None):
    """One FakeModel that serves the clarifier, planner, and executor by role + content."""
    questions = questions or []
    steps = steps or [{"text": "do it"}]
    actions = actions or {}

    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": questions}))
        if role == "planner":
            return ModelResponse(content=json.dumps({"steps": steps}))
        # executor: pick this step's scripted actions, finish when they're exhausted
        user = messages[1]["content"]
        acts = []
        for substr, a in actions.items():
            if substr in user:
                acts = a
                break
        n = sum(1 for m in messages if m.get("role") == "tool")
        if n < len(acts):
            return ModelResponse(tool_calls=[acts[n]], finish_reason="tool_calls")
        return ModelResponse(tool_calls=[ToolCall(name="finish", args={"summary": "done"})],
                             finish_reason="tool_calls")

    return FakeModel(on_complete=fn)


def _op(model, *, integrations=None, timers=None, channel=None, store=None):
    store = store or SqliteStore()
    return Operator(store=store, model=model, search=FakeSearch(),
                    integrations=integrations or FakeIntegrations(),
                    timers=timers or LocalTimers(), channel=channel), store


# ---------------------------------------------------------------- clarify

def test_clarify_parks_run():
    op, _ = _op(_brain(questions=["What's your budget?"]))
    run = op.start(Goal(text="organize a party"))
    assert run.status is RunStatus.CLARIFYING
    assert run.pending_approval.kind == "clarify"


def test_clarify_then_completes():
    model = _brain(questions=["Budget?"], steps=[{"text": "write the greeting"}],
                   actions={"write the greeting": [ToolCall(name="write_file",
                            args={"path": "hi.md", "content": "hello"})]})
    op, store = _op(model)
    cp = ControlPlane(store=store, operator=op)
    run = op.start(Goal(text="greet me"))
    assert run.status is RunStatus.CLARIFYING
    run2 = cp.answer(run_id=run.run_id, answer="$50")
    assert run2.status is RunStatus.DONE


# ---------------------------------------------------------------- happy generic

def test_generic_run_to_done_announces_and_reports():
    ch = _Channel()
    model = _brain(steps=[{"text": "write a brief"}],
                   actions={"write a brief": [ToolCall(name="write_file",
                            args={"path": "brief.md", "content": "hi"})]})
    op, _ = _op(model, channel=ch)
    run = op.start(Goal(text="write me a brief"))
    assert run.status is RunStatus.DONE
    kinds = [e["kind"] for e in ch.events]
    assert "plan_announce" in kinds and "done" in kinds


# ---------------------------------------------------------------- outreach + approval + verification

def _email_model():
    return _brain(steps=[{"text": "email the venue"}],
                  actions={"email the venue": [ToolCall(name="send_email",
                           args={"to": "bob@acme.com", "subject": "Venue inquiry"})]})


def test_side_effect_parks_for_approval_then_verifies():
    op, store = _op(_email_model())
    cp = ControlPlane(store=store, operator=op)
    run = op.start(Goal(text="email the venue about availability"))
    assert run.status is RunStatus.AWAITING_APPROVAL
    assert run.pending_approval.effect_label == "gmail:GMAIL_SEND_EMAIL"
    run2 = cp.answer(run_id=run.run_id, answer="yes")
    assert run2.status is RunStatus.DONE
    effs = store.get_effects(run.run_id)
    assert any(e.phase == "forwarded" and e.expected_present for e in effs)


def test_fabricated_completion_is_refused_through_production_path():
    # THE CI invariant at the spine level: a claimed send that did not land never reaches DONE.
    op, store = _op(_email_model(),
                    integrations=FakeIntegrations(drop_actions={("gmail", "GMAIL_SEND_EMAIL")}))
    cp = ControlPlane(store=store, operator=op)
    run = op.start(Goal(text="email the venue"))
    assert run.status is RunStatus.AWAITING_APPROVAL
    run2 = cp.answer(run_id=run.run_id, answer="yes")
    assert run2.status is RunStatus.ESCALATED          # NOT done
    assert "not reflected" in run2.pending_approval.prompt


def test_owner_declines_side_effect_escalates():
    op, store = _op(_email_model())
    cp = ControlPlane(store=store, operator=op)
    run = op.start(Goal(text="email the venue"))
    run2 = cp.answer(run_id=run.run_id, answer="no")
    assert run2.status is RunStatus.ESCALATED


# ---------------------------------------------------------------- await replies

def _await_model():
    return _brain(steps=[
        {"text": "watch for the venue reply", "kind": "await_replies",
         "params": {"window_seconds": 3600, "min_replies": 1, "match": {"from": "venue@hall.com"}}},
        {"text": "note the reply", "depends_on": [0]},
    ], actions={"note the reply": [ToolCall(name="write_file",
                args={"path": "note.md", "content": "the venue replied"})]})


def test_await_completes_on_verified_reply():
    integ = FakeIntegrations()
    op, store = _op(_await_model(), integrations=integ)
    cp = ControlPlane(store=store, operator=op)
    run = op.start(Goal(text="organize the venue"))
    assert run.status is RunStatus.WAITING
    integ.deliver_inbound("local", sender="venue@hall.com", subject="re: venue", body="we're available")
    run2 = cp.deliver(run_id=run.run_id)
    assert run2.status is RunStatus.DONE


def test_await_ignores_spam_then_replans_and_eventually_escalates():
    integ = FakeIntegrations()
    timers = LocalTimers()
    op, store = _op(_await_model(), integrations=integ, timers=timers)
    cp = ControlPlane(store=store, operator=op)
    # RELENTLESS but bounded by a WALL-CLOCK budget: keep sending batches until the time budget is spent,
    # then escalate honestly (the new deadline_ts terminator — not a fixed 3-batch cap).
    run = op.start(Goal(text="organize the venue", max_runtime_s=25_000_000))
    # a non-matching (spam) reply must NOT complete the await
    integ.deliver_inbound("local", sender="spam@x.com", subject="sale", body="buy now")
    assert cp.deliver(run_id=run.run_id).status is RunStatus.WAITING
    final = None
    for _ in range(10):
        timers.advance(10 ** 7)
        cp.tick()
        final = store.get_run(run.run_id)
        if final.status is RunStatus.ESCALATED:
            break
    assert final.status is RunStatus.ESCALATED
    assert final.replans >= 1   # it tried more batches before the time budget ran out (never fabricated)


# ---------------------------------------------------------------- monitor / heartbeat

def test_monitor_notifies_on_verified_match():
    ch = _Channel()
    integ = FakeIntegrations()
    model = _brain(steps=[{"text": "watch for the bank email", "kind": "monitor",
                           "params": {"interval_seconds": 3600, "match": {"from": "bank@chase.com"},
                                      "notify": "the bank email arrived"}}])
    op, store = _op(model, integrations=integ, channel=ch)
    cp = ControlPlane(store=store, operator=op)
    run = op.start(Goal(text="watch for the bank email and ping me"))
    assert run.status is RunStatus.WAITING
    integ.deliver_inbound("local", sender="bank@chase.com", subject="loan", body="approved")
    run2 = cp.deliver(run_id=run.run_id)
    assert run2.status is RunStatus.DONE
    assert any(e["kind"] == "notify" for e in ch.events)


# ---------------------------------------------------------------- Phase C: watch any page/condition

def test_text_condition_predicates():
    cond = Operator._text_condition
    assert cond("Tickets available now", {"contains": "available"}) == ["match"]
    assert cond("Sold out", {"contains": "available"}) == []
    assert cond("Back in stock", {"absent": "sold out"}) == ["match"]
    assert cond("Sold Out", {"absent": "sold out"}) == []
    assert cond("anything", {}) == []                  # fail closed: no condition never fires
    assert cond("anything", {"notify": "x"}) == []     # only non-condition keys -> fail closed
    assert cond("any", {"pattern": "x"}) == []         # 'pattern' is no longer supported (ReDoS) -> ignored


def _watch_op(model, *, search, channel, timers):
    store = SqliteStore()
    op = Operator(store=store, model=model, search=search, integrations=FakeIntegrations(),
                  timers=timers, channel=channel)
    return op, store


def test_monitor_url_probe_fires_when_page_condition_flips():
    # watch a PAGE over days: poll a URL until it's back in stock, then notify.
    ch, timers = _Channel(), LocalTimers()
    search = FakeSearch(fetches={"shop.example": "In stock — order now"})
    model = _brain(steps=[{"text": "watch the listing", "kind": "monitor",
                           "params": {"interval_seconds": 3600, "probe": "url",
                                      "url": "https://shop.example/item", "confirm_polls": 1,  # snipe instantly
                                      "match": {"absent": "Sold Out", "contains": "in stock"},
                                      "notify": "back in stock!", "max_polls": 5}}])
    op, store = _watch_op(model, search=search, channel=ch, timers=timers)
    cp = ControlPlane(store=store, operator=op)
    run = op.start(Goal(text="tell me when it's back in stock"))
    assert run.status is RunStatus.WAITING
    timers.advance(10 ** 7)
    cp.tick()
    assert store.get_run(run.run_id).status is RunStatus.DONE
    assert any(e["kind"] == "notify" for e in ch.events)


def test_monitor_confirm_polls_debounces_a_transient_match():
    # REVIEW FIX: a page watch needs `confirm_polls` CONSECUTIVE matches before firing, so a single
    # transient signal can't end the watch. With confirm_polls=2 it stays WAITING after one matching poll
    # and only fires (notify) on the second consecutive match.
    ch, timers = _Channel(), LocalTimers()
    search = FakeSearch(fetches={"shop.example": "In stock — order now"})
    model = _brain(steps=[{"text": "watch", "kind": "monitor",
                           "params": {"interval_seconds": 3600, "probe": "url", "url": "https://shop.example/i",
                                      "match": {"contains": "in stock"}, "notify": "back!",
                                      "confirm_polls": 2, "max_polls": 10}}])
    op, store = _watch_op(model, search=search, channel=ch, timers=timers)
    cp = ControlPlane(store=store, operator=op)
    op.start(Goal(text="watch"))
    timers.advance(10 ** 7)
    cp.tick()                                                    # 1st match -> not yet confirmed
    assert not any(e["kind"] == "notify" for e in ch.events)    # debounced: no notify yet
    timers.advance(10 ** 7)
    cp.tick()                                                    # 2nd consecutive match -> fire
    assert any(e["kind"] == "notify" for e in ch.events)


def test_monitor_url_probe_keeps_waiting_then_escalates_on_cap():
    # the page never meets the condition -> never fires; bounded by max_polls -> escalates (never fabricates).
    ch, timers = _Channel(), LocalTimers()
    search = FakeSearch(fetches={"shop.example": "Sold Out"})
    model = _brain(steps=[{"text": "watch the listing", "kind": "monitor",
                           "params": {"interval_seconds": 3600, "probe": "url",
                                      "url": "https://shop.example/item",
                                      "match": {"absent": "Sold Out"}, "notify": "back!", "max_polls": 3}}])
    op, store = _watch_op(model, search=search, channel=ch, timers=timers)
    cp = ControlPlane(store=store, operator=op)
    run = op.start(Goal(text="watch it"))
    assert run.status is RunStatus.WAITING
    final = None
    for _ in range(6):
        timers.advance(10 ** 7)
        cp.tick()
        final = store.get_run(run.run_id)
        if final.status is RunStatus.ESCALATED:
            break
    assert final.status is RunStatus.ESCALATED


def test_monitor_url_probe_does_not_fire_on_a_failed_probe():
    # REVIEW FIX (fail-OPEN): the documented case {"absent": "Sold Out"}. If the page FAILS to load
    # (down / rate-limited / login-wall), an `absent` condition must NOT trivially fire a false
    # 'back in stock!' — a failed probe is 'not yet', keep waiting (bounded -> escalate), NEVER notify.
    from flowers.seams.interfaces import FetchResponse
    ch, timers = _Channel(), LocalTimers()
    search = FakeSearch(fetches={"venue": FetchResponse(ok=False, url="https://venue/show", status=503,
                                                        error="down")})
    model = _brain(steps=[{"text": "watch", "kind": "monitor",
                           "params": {"interval_seconds": 3600, "probe": "url", "url": "https://venue/show",
                                      "match": {"absent": "Sold Out"}, "notify": "back!", "max_polls": 2}}])
    op, store = _watch_op(model, search=search, channel=ch, timers=timers)
    cp = ControlPlane(store=store, operator=op)
    run = op.start(Goal(text="watch"))
    assert run.status is RunStatus.WAITING
    final = None
    for _ in range(5):
        timers.advance(10 ** 7)
        cp.tick()
        final = store.get_run(run.run_id)
        if final.status is RunStatus.ESCALATED:
            break
    assert final.status is RunStatus.ESCALATED                       # bounded out, never falsely completed
    assert not any(e["kind"] == "notify" for e in ch.events)         # never a false 'back in stock!'

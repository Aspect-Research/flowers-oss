"""The ten §5 seed scenarios — conversation-replay eval, END-TO-END through the WEB layer ($0/offline).

Each scenario is a script of inbound owner texts driven through a Starlette ``TestClient`` over the
real ``flowers.channels.web`` app (see ``harness.UX``), asserted through the reusable ``invariants``
(so every scenario inherits the same usability floor) PLUS its own scenario-specific facts. Numbered
and titled to match ``docs/USABILITY_PLAN.md`` §5.

Determinism: the model is a scripted Fake brain, the integration world is ``FakeIntegrations`` (with
fault knobs), and the clock is the ``LocalTimers`` virtual clock — no sleeps for CORRECTNESS, no
network, no wall-clock dependence (the only ``sleep`` is the background-drive settle poll, identical
to the existing web suite).
"""

from __future__ import annotations

import json

import invariants
from _harness import make_brain, tc
from harness import UX

from flowers import runtime, trustgate
from flowers.seams.integrations import FakeIntegrations
from flowers.seams.interfaces import ModelResponse
from flowers.seams.model import FakeModel

# The canonical single-send goal + the executor's scripted compose. The action key "compose and send"
# matches the fast path's deterministic TEMPLATE step text ("compose and send the email to <addr>").
FAST_GOAL = "send an email to marc@acme.com saying the meeting moved to 3pm"
FAST_ACTIONS = {"compose and send": [tc("send_email", to="marc@acme.com", subject="Meeting update",
                                        body="The meeting moved to 3pm.")]}
_SEND_LABEL = "gmail:GMAIL_SEND_EMAIL"


def _forwarded_sends(ux, rid):
    return [e for e in ux.effects(rid) if e.label == _SEND_LABEL and e.phase == "forwarded"]


def _sent_surface(ux):
    return ux.integ.surface(runtime.local_user(), "sent")


# ===================================================================== 1. THE INCIDENT, VERBATIM

def test_s01_incident_verbatim_unverifiable_confirmed_done():
    """§5.1 — The incident, verbatim, through the WEB layer. An explicit send auto-commits (owner-grant),
    surfaces the draft as the single touch, then the Sent read-back is UNAVAILABLE (the incident's
    swallowed-to-None snapshot: 'couldn't confirm it went through'). The owner attests "It was sent!" and
    the run reaches DONE, owner-confirmed — never the pre-P0.2 "rephrase" dead-end, never a chore.

    (Read-back UNAVAILABLE models the honest 'unverifiable -> did it arrive?' path; a non-retryable
    BAD_INPUT is now the distinct ``verification_broken`` state, which self-heals on a timer and never
    becomes an owner chore — so the surface-unavailable fault is what reproduces the owner-asked
    conversation. Unit-level versions live in tests/test_escalation_intents.py; this one is over HTTP.)
    """
    actions = {"compose and send": [tc("send_email", to="marc@acme.com", subject="hi about the party",
                                       body="Hi Marc — wanted to say hi about the party!")]}
    with UX(model=make_brain(actions=actions),
            integrations=FakeIntegrations(no_readback={"gmail"})) as ux:
        rid = ux.goal("email marc@acme.com and say hi about the party")
        assert ux.status(rid) == "awaiting_approval"                  # the draft preview — the one touch
        assert ux.run(rid).pending_approval.kind == "preview"

        assert ux.answer(rid, "yes") == "escalated"                  # sent but unverifiable -> "did it arrive?"
        assert ux.run(rid).pending_approval.reason_code == "needs_owner_confirm"

        assert ux.answer(rid, "It was sent!") == "done"              # owner attests -> DONE
        ev = ux.events(rid)
        invariants.one_touch(ev)                                     # exactly the draft (the escalation is
        invariants.no_dead_ends(ev)                                  # a legit infra-failure ask, not a touch)
        invariants.no_user_chores(ev)                                # "did it arrive?" is the honest exception
        invariants.substantive_asks(ev)
        invariants.terminal_runs(ux.store, rid)
        assert not any("rephrase" in (e.get("text") or "") for e in ev)

        send = next(e for e in _forwarded_sends(ux, rid))
        assert send.detail.get("verification") == "owner-confirmed"  # the attestation, on the ledger
        assert send.expected_present is None                         # honest: never an independent read-back
        # honesty floor: owner-confirmed is a DISTINCT evidence class — strict verified_effects excludes it.
        verified = trustgate.verified_effects([e.as_gate_dict() for e in ux.effects(rid)])
        assert _SEND_LABEL not in verified


# ============================================================ 2. EXPLICIT SEND, HAPPY PATH (preview)

def test_s02_explicit_send_one_touch_verified():
    """§5.2 — Explicit send, preview=always (default): exactly ONE touch (the draft), sent + verified,
    at the fast path's <=2 model calls. No clarifier, no plan-announce — the draft IS the announcement."""
    with UX(model=make_brain(actions=FAST_ACTIONS)) as ux:
        rid = ux.goal(FAST_GOAL)
        assert ux.status(rid) == "awaiting_approval"
        ev = ux.events(rid)
        kinds = [e["kind"] for e in ev]
        assert "clarify" not in kinds and "plan_announce" not in kinds   # fast path skipped both

        assert ux.answer(rid, "yes") == "done"
        ev = ux.events(rid)
        assert invariants.one_touch(ev) == 1                         # the draft, and only the draft
        invariants.no_dead_ends(ev)
        invariants.no_user_chores(ev)
        invariants.substantive_asks(ev)
        invariants.terminal_runs(ux.store, rid)
        invariants.latency_budget(ux.store, rid, 2)                  # the fast path: compose + finish

        sends = _forwarded_sends(ux, rid)
        assert len(sends) == 1 and sends[0].expected_present is True  # independently verified (Sent ✓)


# ================================================================ 3. EXPLICIT SEND, preview=never

def test_s03_preview_never_zero_touch_verified():
    """§5.3 — Explicit send with FLOWERS_SEND_PREVIEW=never: ZERO touches — it sends straight through,
    verifies, and closes DONE, still at <=2 model calls."""
    with UX(model=make_brain(actions=FAST_ACTIONS), send_preview="never") as ux:
        rid = ux.goal(FAST_GOAL)
        assert ux.status(rid) == "done"
        ev = ux.events(rid)
        assert invariants.one_touch(ev, budget=0) == 0               # zero owner touches
        invariants.no_dead_ends(ev)
        invariants.no_user_chores(ev)
        invariants.substantive_asks(ev)
        invariants.terminal_runs(ux.store, rid)
        invariants.latency_budget(ux.store, rid, 2)

        sends = _forwarded_sends(ux, rid)
        assert len(sends) == 1 and sends[0].expected_present is True


# ===================================================================== 4. VAGUE SEND -> CLARIFY

def test_s04_vague_send_clarifies_then_one_draft_touch():
    """§5.4 — A vague send ("email marc about the thing" — no address, no content): the fast path
    declines, the clarifier asks its one content question, and the owner's reply SUPPLIES the recipient
    (their own words -> named-by-owner) -> owner-grant auto-commit -> ONE draft-preview touch -> sent.
    The clarify is counted SEPARATELY from the single approval touch."""
    brain = make_brain(
        questions=["who should I email, and what should I say?"],
        steps=[{"text": "send the note"}],
        actions={"send the": [tc("send_email", to="marc@acme.com", subject="the thing",
                                 body="The thing is all set.")]},
        mandate={"action_types": [_SEND_LABEL], "recipient_scope": [],
                 "magnitude_caps": {"max_sends": 1, "per_domain": 1, "per_recipient": 1}})
    with UX(model=brain) as ux:
        rid = ux.goal("email marc about the thing")
        assert ux.status(rid) == "clarifying"
        assert len(invariants.clarifies(ux.events(rid))) == 1        # asked (a required field was absent)

        # the owner's reply names the recipient + content -> named-by-owner -> auto-commit (no card)
        assert ux.answer(rid, "send it to marc@acme.com and tell him the thing is all set") == \
            "awaiting_approval"
        assert ux.run(rid).mandate_auto is True
        assert ux.run(rid).pending_approval.kind == "preview"        # the single touch is the draft

        assert ux.answer(rid, "yes") == "done"
        ev = ux.events(rid)
        invariants.one_touch(ev)                                     # ONE approval (the draft)
        assert len(invariants.clarifies(ev)) == 1                    # the content question, counted apart
        invariants.no_dead_ends(ev)
        invariants.no_user_chores(ev)
        invariants.substantive_asks(ev)
        invariants.terminal_runs(ux.store, rid)
        assert len(_forwarded_sends(ux, rid)) == 1


# ==================================================================== 5. OWNER DECLINES THE DRAFT

def test_s05_decline_draft_clean_stop_nothing_sent():
    """§5.5 — The owner declines the draft preview ("no"): the run STOPS cleanly, nothing is forwarded,
    nothing is sent, and no zombie is left behind (terminal)."""
    with UX(model=make_brain(actions=FAST_ACTIONS)) as ux:
        rid = ux.goal(FAST_GOAL)
        assert ux.status(rid) == "awaiting_approval"
        assert ux.answer(rid, "no") == "stopped"
        ev = ux.events(rid)
        invariants.one_touch(ev)                                     # the draft was the only touch
        invariants.no_dead_ends(ev)
        invariants.terminal_runs(ux.store, rid)
        assert ux.effects(rid) == []                                 # nothing forwarded
        assert _sent_surface(ux) == {}                               # nothing sent


# =================================================================== 6. ACK AFTER A DONE RUN -> CHAT

def _brain_send_and_route(route_resp: dict):
    """One brain that serves both a fast-path send (executor) AND the /api/route classifier (which the
    web layer calls with the _ROUTE_SYSTEM prompt). The route reply is scripted per case."""
    def fn(messages, tools, role):
        if "route ONE incoming text" in messages[0]["content"]:
            return ModelResponse(content=json.dumps(route_resp))
        if sum(1 for m in messages if m.get("role") == "tool") == 0:
            return ModelResponse(tool_calls=[tc("send_email", to="marc@acme.com", subject="Done",
                                                body="Done.")], finish_reason="tool_calls")
        return ModelResponse(tool_calls=[tc("finish", summary="sent")], finish_reason="tool_calls")
    return FakeModel(on_complete=fn)


def test_s06_ack_after_done_run_routes_to_chat_no_new_run():
    """§5.6 — "thanks" / "ok" / "👍" after a run finished route to CHAT via /api/route (the chat-client hook),
    never spawning a new task. Even when the classifier misfires (reply -> a task number that isn't
    awaiting), the deterministic ack-guard (P1.2) downgrades a bare ack to chat — never a run."""
    # a plausible classifier misfire: it tries to bind the ack to task #9, which isn't in the runs list.
    with UX(model=_brain_send_and_route({"intent": "reply", "n": 9}), send_preview="never") as ux:
        rid = ux.goal(FAST_GOAL)
        assert ux.status(rid) == "done"
        before = ux.run_ids()
        runs = [{"n": 1, "goal": FAST_GOAL, "status": "done", "awaiting": None}]
        for ack in ("thanks", "ok", "👍"):
            assert ux.route(ack, runs) == {"intent": "chat", "n": None}, f"ack {ack!r} did not route to chat"
        assert ux.run_ids() == before                               # no new run spawned by an ack


# ================================================================= 7. TWO RUNS + BARE "yes" (SAFETY)

def _route_brain(resp: dict):
    def fn(messages, tools, role):
        return ModelResponse(content=json.dumps(resp))
    return FakeModel(on_complete=fn)


_TWO_AWAITING = [
    {"n": 1, "goal": "send an email to mom", "status": "awaiting_approval", "awaiting": "approval"},
    {"n": 2, "goal": "clone the flowers repo", "status": "awaiting_approval", "awaiting": "approval"},
]


def test_s07_two_awaiting_bare_yes_never_auto_answers():
    """§5.7 — Two concurrent runs awaiting approval + a bare "yes" must NEVER auto-answer either. The
    "which one?" refusal belongs to the CHAT CLIENT (a bare yes with 2 approvals pending must ask which,
    never auto-approve). This asserts the flowers-side contract that guard relies on: /api/route returns
    a reply ONLY with an explicit, valid n from the classifier, and it NEVER INVENTS an n for a bare
    "yes" — so the client always sees either an explicit target (which it then disambiguates) or no
    target at all, never a fabricated one."""
    # (a) the classifier hands back NO n -> flowers must not fabricate one -> falls to the (gated) task
    #     default, never a silent reply to run #1 or #2.
    with UX(model=_route_brain({"intent": "reply", "n": None})) as ux:
        assert ux.route("yes", _TWO_AWAITING) == {"intent": "task", "n": None}
    # (b) the classifier names an explicit VALID n -> flowers passes exactly THAT n through (it does not
    #     invent, drop, or re-guess it); the chat client owns the 2-awaiting bare-yes "which one?" refusal.
    with UX(model=_route_brain({"intent": "reply", "n": 1})) as ux:
        assert ux.route("yes", _TWO_AWAITING) == {"intent": "reply", "n": 1}


# ===================================================================== 8. ESCALATION IGNORED -> REAP

def test_s08_ignored_escalation_reaped_at_ttl():
    """§5.8 — An escalation the owner never answers is closed by the zombie-run reaper (P1.1) at its TTL.
    Advance the VIRTUAL clock past 24h, pump tick() -> the run STOPS with a quiet note (no chore, no dead
    end, terminal)."""
    with UX(model=make_brain(actions=FAST_ACTIONS),
            integrations=FakeIntegrations(no_readback={"gmail"})) as ux:
        rid = ux.goal(FAST_GOAL)
        assert ux.answer(rid, "yes") == "escalated"
        assert ux.run(rid).pending_approval.reason_code == "needs_owner_confirm"

        ux.advance(24 * 3600 + 60)                                   # past the escalation TTL
        ux.tick()
        assert ux.status(rid) == "stopped"
        ev = ux.events(rid)
        assert any("closing this out" in (e.get("text") or "") for e in ev)   # the quiet reaper note
        invariants.no_dead_ends(ev)
        invariants.no_user_chores(ev)
        invariants.terminal_runs(ux.store, rid)


# ================================================================ 9. READ-BACK SLOW BUT WORKING (lag)

def test_s09_readback_lag_verified_without_owner():
    """§5.9 — The Sent read-back is SLOW but working (index lag): the verify loop rides out the lag and
    verifies the send WITHOUT any owner involvement — no escalation, no chore question. The only touch is
    the draft preview; verification is mechanical."""
    with UX(model=make_brain(actions=FAST_ACTIONS),
            integrations=FakeIntegrations(readback_lag={("gmail", "GMAIL_SEND_EMAIL"): 2}),
            verify_attempts=4, verify_delay=0.0) as ux:      # attempts > lag; delay 0 -> no real sleeping
        rid = ux.goal(FAST_GOAL)
        assert ux.status(rid) == "awaiting_approval"
        assert ux.answer(rid, "yes") == "done"                      # rode out the lag -> verified -> DONE
        ev = ux.events(rid)
        invariants.one_touch(ev)                                    # only the draft
        assert invariants.escalations(ev) == []                    # never escalated for verification
        invariants.no_user_chores(ev)                              # no "did it arrive?" — it self-verified
        invariants.no_dead_ends(ev)
        invariants.terminal_runs(ux.store, rid)
        sends = _forwarded_sends(ux, rid)
        assert len(sends) == 1 and sends[0].expected_present is True


# ==================================================================== 10. DENIED CONFIRMATION -> RESEND

def test_s10_denied_confirmation_resends_and_verifies():
    """§5.10 — The owner denies the confirmation ("nothing arrived"): the escalated send is corrected to
    failed + proven-absent (releasing the idempotency lock), the resend replans + executes, and — with the
    read-back healed before the resend — a REAL second send goes out and is independently verified. DONE."""
    integ = FakeIntegrations(no_readback={"gmail"})
    brain = make_brain(actions=FAST_ACTIONS,
                       questions=["what should the email say?"],   # would derail a fast-path run if it ran
                       steps=[{"text": "compose and send the email to marc@acme.com once more"}])
    with UX(model=brain, integrations=integ) as ux:
        rid = ux.goal(FAST_GOAL)
        assert ux.answer(rid, "yes") == "escalated"
        assert ux.run(rid).pending_approval.reason_code == "needs_owner_confirm"

        integ._no_readback.clear()                                 # the read-back surface recovers
        assert ux.answer(rid, "nothing arrived") == "done"        # denied -> resend -> verified -> DONE

        corr = [e for e in ux.effects(rid) if e.detail.get("correction") == "owner-reported-missing"]
        assert len(corr) == 1 and corr[0].phase == "failed" and corr[0].expected_present is False
        assert len(_forwarded_sends(ux, rid)) == 1                 # the real resend (original is failed now)
        assert not any(e.detail.get("idempotent_replay") for e in ux.effects(rid))  # not a dedup replay
        verified = trustgate.verified_effects([e.as_gate_dict() for e in ux.effects(rid)])
        assert _SEND_LABEL in verified                             # the resend independently verified

        ev = ux.events(rid)
        invariants.no_dead_ends(ev)
        invariants.terminal_runs(ux.store, rid)

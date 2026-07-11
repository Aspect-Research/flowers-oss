"""P0.1 — loud, distinguishable infra failures for a send whose self-verification read-back is BROKEN.

The incident (run_3c30c72c1b8e, 2026-07-08): Arcade made Gmail.ListEmailsByHeader.recipient list-typed,
so the STRING we sent 400'd every Sent read-back. The error was swallowed to a None snapshot, so a send
that really went out read as "unverifiable" and escalated a chore to the owner ("I sent it but couldn't
confirm — can you double-check? (or say 'retry')"), where a 'retry' would risk a DOUBLE send.

After P0.1 the broker tells three outcomes apart — verified / proven-missing / verification_broken — and
a verification_broken send COMPLETES the run (landed semantics), notes it once in plain voice, and arms a
single +60s re-check that only bothers the owner if the send turns out to be genuinely missing. These
tests drive the REAL engine offline via the Fake seam's fault-injection knobs ($0, no network).
"""

from __future__ import annotations

from _harness import build, make_brain, tc

from flowers.broker import Broker
from flowers.seams.integrations import FakeIntegrations
from flowers.types import Goal, RunStatus

_SEND = ("gmail", "GMAIL_SEND_EMAIL")


def _armed_kinds(timers) -> list[str]:
    """The kinds of timers still armed (not cancelled, not fired) — a direct read of the durable table."""
    rows = timers._conn.execute("SELECT kind FROM timers WHERE cancelled=0 AND fired=0").fetchall()
    return [r["kind"] for r in rows]


def _send_scenario(integ):
    """Build a one-step 'email bob@acme.com' run over ``integ`` and approve the send. Returns (h, rid)."""
    h = build(model=make_brain(
        steps=[{"text": "email bob@acme.com"}],
        actions={"email bob": [tc("send_email", to="bob@acme.com", subject="hi", body="hello there")]}),
        integrations=integ)
    run = h["op"].start(Goal(text="email bob@acme.com"))
    assert run.status is RunStatus.AWAITING_APPROVAL     # the send parks for approval (the one touch)
    run = h["cp"].answer(run_id=run.run_id, answer="yes")
    return h, run


# --------------------------------------------------------------------------- broker-level unit tests

def _broker(integ, **kw):
    return Broker(integrations=integ, run_id="r", **kw)


def test_broken_readback_records_verification_broken_distinct_from_unverifiable():
    # readback_errors -> a NON-retryable schema/BAD_INPUT error -> verification_broken: the effect carries
    # detail['readback_error'] with drift/expected None. A no_readback send is ALSO drift/expected None but
    # carries NO readback_error — the distinction P0.1 introduces (loud infra failure vs honest no-surface).
    broken = _broker(FakeIntegrations(readback_errors={_SEND})).call_integration(
        toolkit="gmail", action="GMAIL_SEND_EMAIL",
        params={"to": "bob@acme.com", "subject": "hi"}, user_id="u1", authorized=True)
    assert broken.effect.phase == "forwarded"
    assert broken.effect.drift_present is None and broken.effect.expected_present is None
    assert broken.effect.detail.get("readback_error")           # loud: the error is captured, not swallowed

    plain = _broker(FakeIntegrations(no_readback={"gmail"})).call_integration(
        toolkit="gmail", action="GMAIL_SEND_EMAIL",
        params={"to": "bob@acme.com", "subject": "hi"}, user_id="u1", authorized=True)
    assert plain.effect.drift_present is None and plain.effect.expected_present is None
    assert not plain.effect.detail.get("readback_error")        # a genuine no-read-back surface, not broken


def test_lagging_readback_still_verifies_within_attempts():
    # Provider index-lag: the Sent item is hidden for its first 2 polls, then appears. With enough verify
    # attempts the send still VERIFIES (no false proven-missing, no owner involvement).
    integ = FakeIntegrations(readback_lag={_SEND: 2})
    res = _broker(integ, verify_attempts=3).call_integration(
        toolkit="gmail", action="GMAIL_SEND_EMAIL",
        params={"to": "bob@acme.com", "subject": "hi"}, user_id="u1", authorized=True)
    assert res.effect.expected_present is True and not res.effect.detail.get("readback_error")


def test_retryable_readback_error_retries_every_attempt_without_early_stop():
    # STRUCTURAL classification (P0.1): a RETRYABLE read-back error must NOT early-stop the verify loop
    # the way a non-retryable schema/BAD_INPUT one does. The message carries "retry after 4220ms" — its
    # 422/400 digits must be IGNORED (there's no structural nonretryable signal). Drive _verify_readback
    # directly with a baseline present and a post read-back that errors retryably every poll: it must run
    # all `verify_attempts` and settle (None, None, None) — plain unverifiable, with NO readback_error.
    b = _broker(FakeIntegrations(), verify_attempts=3)
    calls = {"n": 0}
    def flaky_after():
        calls["n"] += 1
        return None, "kind=TOOL_RUNTIME_RETRY status=503: upstream busy, retry after 4220ms"
    drift, expected, err = b._verify_readback({"acct": {}}, flaky_after, {"id": "x"})
    assert calls["n"] == 3                                 # rode out every attempt — no early stop
    assert (drift, expected, err) == (None, None, None)    # plain unverifiable, NOT verification_broken

    # contrast: a STRUCTURED non-retryable error early-stops after the FIRST poll (retrying can't help).
    calls["n"] = 0
    def broken_after():
        calls["n"] += 1
        return None, "nonretryable=true kind=TOOL_RUNTIME_BAD_INPUT_VALUE status=400: bad input"
    drift, expected, err = b._verify_readback({"acct": {}}, broken_after, {"id": "x"})
    assert calls["n"] == 1                                 # early stop
    assert err and expected is None and drift is None      # verification_broken (the error is surfaced)


# --------------------------------------------------------------------------- operator-level scenarios

def test_verified_send_path_is_unchanged():
    # The happy path: a normal Fake read-back verifies the send -> DONE, expected_present True, no
    # verification_broken note, no re-check timer. (Guards that P0.1 didn't perturb the ordinary flow.)
    h, run = _send_scenario(FakeIntegrations())
    assert run.status is RunStatus.DONE
    sends = [e for e in h["store"].get_effects(run.run_id) if e.label == "gmail:GMAIL_SEND_EMAIL"
             and e.phase == "forwarded"]
    assert len(sends) == 1 and sends[0].expected_present is True
    assert "reverify" not in _armed_kinds(h["timers"])
    assert h["channel"].of_kind("escalated") == []


def test_verification_broken_completes_with_landed_report_and_timer_no_owner_question():
    # A schema-broken Sent read-back must NOT escalate a chore: the run completes (landed), the final
    # report notes the hiccup ONCE in plain voice, a single +60s re-check is armed, and the owner is
    # never asked to confirm anything.
    h, run = _send_scenario(FakeIntegrations(readback_errors={_SEND}))
    assert run.status is RunStatus.DONE                          # completed, not escalated
    assert h["channel"].of_kind("escalated") == []              # no owner chore-question
    send = next(e for e in h["store"].get_effects(run.run_id)
                if e.label == "gmail:GMAIL_SEND_EMAIL" and e.phase == "forwarded")
    assert send.expected_present is None and send.detail.get("readback_error")
    assert _armed_kinds(h["timers"]).count("reverify") == 1     # exactly ONE re-check armed
    report = h["channel"].of_kind("done")[-1]["text"]
    assert "re-verify" in report and "sent the email" in report  # the soft note, reply-style
    # honest weaker claim: the provider ACCEPTED it (delivery is exactly what's unconfirmed) — never the
    # overclaim that "it went out".
    assert "was accepted" in report and "it went out" not in report
    assert "gmail:GMAIL_SEND_EMAIL" not in report and "read-back" not in report
    # and it went out exactly once — never a spurious retry/duplicate under a broken read-back
    assert len(h["integ"].surface("local", "sent")) == 1


def test_reverify_finds_proven_missing_and_notifies_owner():
    # The send's read-back was broken AND the send never landed (dropped). The +60s re-check runs after
    # the read-back tool recovers, PROVES the message absent, and honestly notifies the owner.
    integ = FakeIntegrations(readback_errors={_SEND}, drop_actions={_SEND})
    h, run = _send_scenario(integ)
    rid = run.run_id
    assert run.status is RunStatus.DONE and "reverify" in _armed_kinds(h["timers"])
    integ.heal_readback_errors()          # the read-back tool recovers; the dropped send is still missing
    h["timers"].advance(_after := 61)
    h["cp"].tick()
    run = h["store"].get_run(rid)
    assert run.status is RunStatus.ESCALATED                    # the owner is notified — real failure
    msg = h["channel"].of_kind("escalated")[-1]["text"]
    assert "sent the email" in msg and "doesn't look like it actually went out" in msg
    # honest open question — invites direction, never promises a retry the handler can't safely run (a
    # byte-identical resend is idempotency-short-circuited; retry wiring is P0.2).
    assert "How do you want to handle it?" in msg and "another run at it" not in msg
    assert run.pending_approval is not None


def test_reverify_confirms_send_and_stays_done():
    # The read-back was broken at send time but the send DID land. The +60s re-check (after the tool
    # recovers) confirms the message -> the run stays DONE, the owner is never bothered.
    integ = FakeIntegrations(readback_errors={_SEND})           # broken check, but NOT dropped -> it lands
    h, run = _send_scenario(integ)
    rid = run.run_id
    assert run.status is RunStatus.DONE
    integ.heal_readback_errors()
    h["timers"].advance(61)
    h["cp"].tick()
    run = h["store"].get_run(rid)
    assert run.status is RunStatus.DONE                         # confirmed -> stays done
    assert h["channel"].of_kind("escalated") == []             # no owner message


def test_reverify_still_broken_stays_done_without_bothering_owner():
    # The read-back tool NEVER recovers. The +60s re-check still can't confirm -> just record it (log);
    # the owner is not handed a chore. The run stays DONE (the send did go out).
    integ = FakeIntegrations(readback_errors={_SEND})           # persistent, never healed; the send landed
    h, run = _send_scenario(integ)
    rid = run.run_id
    assert run.status is RunStatus.DONE
    h["timers"].advance(61)
    h["cp"].tick()
    run = h["store"].get_run(rid)
    assert run.status is RunStatus.DONE
    assert h["channel"].of_kind("escalated") == []


def test_proven_missing_send_still_escalates_immediately():
    # A send that the (WORKING) read-back proves never landed is REAL failure -> escalate at send time,
    # exactly as before P0.1 (verification_broken must not soften a genuine proven-missing).
    h, run = _send_scenario(FakeIntegrations(drop_actions={_SEND}))
    assert run.status is RunStatus.ESCALATED
    assert h["channel"].of_kind("escalated")                    # the owner is asked (evidence of failure)


def test_retryable_readback_error_stays_plain_unverifiable_not_broken():
    # A read-back that errors RETRYABLY (transient blip: the baseline read is fine, then every verify
    # poll errors with "retry after 4220ms") is NOT verification_broken. The loop rides out its attempts
    # and, when they error, the send settles PLAIN unverifiable — escalating the owner exactly as before
    # P0.1, with NO readback_error recorded and NO +60s re-check armed (the broken path must NOT trigger).
    h, run = _send_scenario(FakeIntegrations(readback_retryable_errors={_SEND}))
    assert run.status is RunStatus.ESCALATED                     # plain unverifiable -> owner asked
    send = next(e for e in h["store"].get_effects(run.run_id)
                if e.label == "gmail:GMAIL_SEND_EMAIL" and e.phase == "forwarded")
    assert send.expected_present is None and not send.detail.get("readback_error")   # NOT broken
    assert "reverify" not in _armed_kinds(h["timers"])          # the broken path did NOT trigger
    msg = h["channel"].of_kind("escalated")[-1]["text"]
    assert "couldn't confirm" in msg and "did it arrive" in msg  # the plain-unverifiable copy (P1.4: honest question, no chore)


def test_reverify_does_not_escalate_a_stopped_run():
    # Defensive guard (P0.1): if the owner has CLOSED the run (STOPPED) by the time the +60s re-check
    # fires and proves the send missing, we must NOT re-open it with a fresh escalation — the owner
    # already walked away. Leave it STOPPED (logged at ERROR); DONE/ESCALATED still escalate (above).
    integ = FakeIntegrations(readback_errors={_SEND}, drop_actions={_SEND})
    h, run = _send_scenario(integ)
    rid = run.run_id
    assert run.status is RunStatus.DONE and "reverify" in _armed_kinds(h["timers"])
    stopped = h["store"].get_run(rid)
    stopped.status = RunStatus.STOPPED             # owner closed the run before the re-check fires
    h["store"].save_run(stopped)
    integ.heal_readback_errors()                   # tool recovers; the dropped send is still missing
    before = len(h["channel"].of_kind("escalated"))
    h["timers"].advance(61)
    h["cp"].tick()
    run = h["store"].get_run(rid)
    assert run.status is RunStatus.STOPPED                       # stays closed — not re-opened
    assert len(h["channel"].of_kind("escalated")) == before     # no new escalation event

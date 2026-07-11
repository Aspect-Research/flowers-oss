"""The broker — the single egress that turns a tool call into a gate-ready EffectRecord.

The headline tests prove the production path end-to-end at the broker level: a verified send is
accepted by the gate; a fabricated (non-landing) send is REFUSED; an unverifiable one is routed to
the owner. No hand-authored gate inputs — the EffectRecord comes from the broker's real read-back.
"""

from __future__ import annotations

from flowers import trustgate as g
from flowers.broker import Broker
from flowers.seams.integrations import FakeIntegrations
from flowers.seams.interfaces import ModelResponse


class _StubModel:
    """Minimal ModelClient for metering tests."""
    def __init__(self, cost=0.01):
        self.cost = cost

    def available(self):
        return True

    def complete(self, messages, *, tools=None, role="executor", response_format=None, max_tokens=None):
        return ModelResponse(content="ok", cost_usd=self.cost)


def _gate_for(effect):
    """Run a single effect through the real gate, claiming done — the production-path adjudication."""
    unver, unverifiable = g.classify_effects([effect.as_gate_dict()], claimed_done=True)
    return g.gate_verdict(claimed_done=True, ok=True, stale_files=[], gate_breaking=[],
                          unverified_external=unver, unverifiable_external=unverifiable)


def _broker(integrations, **kw):
    return Broker(integrations=integrations, run_id="run_1", **kw)


def test_side_effect_needs_approval_when_unauthorized():
    b = _broker(FakeIntegrations())
    res = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                             params={"to": "a@b.com", "subject": "s"}, user_id="u1", authorized=False)
    assert res.status == "needs_approval"
    assert res.approval.kind == "side_effect" and res.approval.tier == "ask"
    assert res.effect.phase == "deferred" and res.effect.side_effecting is True


def test_send_approval_prompt_shows_the_literal_body():
    # draft-then-send preview: the owner sees exactly what goes out under their name before approving.
    b = _broker(FakeIntegrations())
    res = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                             params={"to": "bob@acme.com", "subject": "Hi",
                                     "body": "Dear Bob, here is my tailored pitch."}, user_id="u1")
    assert res.status == "needs_approval"
    # conversational: a friendly ask naming the recipient, then the literal body the owner will send.
    assert "Dear Bob, here is my tailored pitch." in res.approval.prompt
    assert "bob@acme.com" in res.approval.prompt
    assert res.approval.prompt.lower().startswith("want me to send this email")
    assert "gmail:GMAIL_SEND_EMAIL" not in res.approval.prompt   # no slug/JSON in the human prompt


def test_never_tier_needs_approval_kind_never():
    b = _broker(FakeIntegrations())
    res = b.call_integration(toolkit="gmail", action="GMAIL_DELETE_MESSAGE",
                             params={}, user_id="u1", authorized=False)
    assert res.status == "needs_approval" and res.approval.kind == "never"


def test_verified_send_is_accepted_by_gate():
    b = _broker(FakeIntegrations())
    res = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                             params={"to": "bob@acme.com", "subject": "Venue inquiry"},
                             user_id="u1", authorized=True)
    assert res.status == "ok" and res.effect.phase == "forwarded"
    assert res.effect.expected_present is True and res.effect.drift_present is True
    accept, reason = _gate_for(res.effect)
    assert accept is True


def test_fabricated_send_is_refused_by_gate():
    # THE CI invariant at the broker level: a claimed send that did not land is refused through the
    # real read-back path — no hand-authored gate input.
    b = _broker(FakeIntegrations(drop_actions={("gmail", "GMAIL_SEND_EMAIL")}))
    res = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                             params={"to": "bob@acme.com", "subject": "Venue inquiry"},
                             user_id="u1", authorized=True)
    assert res.status == "ok" and res.effect.phase == "forwarded"
    assert res.effect.expected_present is False
    accept, reason = _gate_for(res.effect)
    assert accept is False and "not reflected" in reason


def test_unverifiable_send_routes_to_owner():
    # a side-effecting send whose toolkit has NO independent read-back surface -> not added-item
    # verifiable -> never silently accepted, always owner-confirmed. (Modelled offline by no_readback;
    # in the live v1 Gmail+Calendar surface every kept write IS read-back-verifiable, but the gate's
    # unverifiable path must still hold for any future no-readback write.)
    b = _broker(FakeIntegrations(no_readback={"gmail"}))
    res = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                             params={"to": "bob@acme.com", "subject": "Venue inquiry"},
                             user_id="u1", authorized=True)
    assert res.effect.drift_present is None and res.effect.expected_present is None
    accept, reason = _gate_for(res.effect)
    assert accept is False and "needs your confirmation" in reason


def test_auto_readonly_executes_without_approval():
    b = _broker(FakeIntegrations())
    res = b.call_integration(toolkit="gmail", action="GMAIL_FETCH_EMAILS",
                             params={}, user_id="u1", authorized=False)
    assert res.status == "ok" and res.effect.side_effecting is False


def test_model_metering():
    seen = {}
    def on_usage(*, kind, cost_usd, detail):
        seen.setdefault(kind, 0.0)
        seen[kind] += cost_usd
    b = Broker(model=_StubModel(cost=0.02), integrations=FakeIntegrations(), on_usage=on_usage)
    b.complete([{"role": "user", "content": "hi"}], role="planner")
    assert abs(b.spent_usd - 0.02) < 1e-9
    assert abs(seen.get("model", 0.0) - 0.02) < 1e-9


def test_transient_readback_miss_is_retried_not_falsely_unverifiable():
    # A one-off read-back hiccup (snapshot returns None) on the FIRST post-send check must be RETRIED,
    # not concluded terminal — so a live send whose Sent label hasn't indexed yet still verifies on a
    # later attempt instead of falsely escalating "couldn't confirm it landed". (Before: a None `after`
    # bailed the verify loop immediately, so more attempts never helped the transient-error case.)
    class _FlakyReadback(FakeIntegrations):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.calls = 0

        def snapshot(self, **kw):
            self.calls += 1
            if self.calls == 2:      # call 1 = pre-send baseline (ok); call 2 = first after-snapshot (miss)
                return None
            return super().snapshot(**kw)

    integ = _FlakyReadback()
    b = _broker(integ, verify_attempts=3, verify_delay=0.0)
    params = {"to": "bob@acme.com", "subject": "hi", "body": "x"}
    gk = b.grant_key_for("gmail", "GMAIL_SEND_EMAIL", params)
    res = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL", params=params,
                             user_id="u1", grants={gk})
    assert res.ok and res.effect.phase == "forwarded"
    assert res.effect.expected_present is True     # retried PAST the transient miss -> verified
    assert integ.calls >= 3                         # baseline + at least two after-attempts


def test_persistent_readback_failure_stays_unverifiable():
    # If EVERY post-send read-back errors, it settles honestly on unverifiable (None) — never fabricated.
    class _DeadReadback(FakeIntegrations):
        def snapshot(self, **kw):
            return None

    b = _broker(_DeadReadback(), verify_attempts=3, verify_delay=0.0)
    params = {"to": "bob@acme.com", "subject": "hi", "body": "x"}
    gk = b.grant_key_for("gmail", "GMAIL_SEND_EMAIL", params)
    res = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL", params=params,
                             user_id="u1", grants={gk})
    assert res.ok and res.effect.expected_present is None      # no baseline/read-back -> unverifiable

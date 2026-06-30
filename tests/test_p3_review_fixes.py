"""Regressions for the P3 adversarial-review findings (p3-trustpath-review). Each test names the fix
so a future change that reopens a trust hole fails loudly.
"""

from __future__ import annotations

from _harness import build, make_brain, tc

from flowers import policy
from flowers import trustgate as g
from flowers.broker import Broker
from flowers.seams.browser import FakeBrowser, is_side_effecting_action
from flowers.seams.integrations import FakeIntegrations, _parse_emails
from flowers.types import Goal, RunStatus


def _gate(effect):
    unver, unverifiable = g.classify_effects([effect.as_gate_dict()], claimed_done=True)
    return g.gate_verdict(claimed_done=True, ok=True, stale_files=[], gate_breaking=[],
                          unverified_external=unver, unverifiable_external=unverifiable)


# --- Fix A: a grant binds to the EXACT params; fp-less actions don't collapse to a reusable bare label ---

def test_a_integration_grant_binds_to_exact_recipient():
    b = Broker(integrations=FakeIntegrations(), run_id="r")
    parked = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                                params={"to": "bob@x.com", "subject": "Hi", "body": "y"}, user_id="u1")
    grant = parked.grant_key
    other = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                               params={"to": "eve@x.com", "subject": "Hi", "body": "y"},
                               user_id="u1", grants={grant})
    assert other.status == "needs_approval"          # a different recipient is NOT authorized
    same = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                              params={"to": "bob@x.com", "subject": "Hi", "body": "y"},
                              user_id="u1", grants={grant})
    assert same.status == "ok"


def test_a_fingerprintless_browser_actions_do_not_share_a_grant():
    b = Broker(browser=FakeBrowser(), run_id="r")
    g1 = b.call_browser(action="submit", params={"target": "venueA.com"}, user_id="u1").grant_key
    other = b.call_browser(action="submit", params={"target": "venueB.com"}, user_id="u1", grants={g1})
    assert other.status == "needs_approval"          # ref-less submits to different targets don't share a grant


# --- Fix B: an owner auto override waives APPROVAL but NOT the independent verification ---

def test_b_browser_auto_override_still_verifies_and_refuses_nonlanding():
    b = Broker(browser=FakeBrowser(drop_actions=("submit",)), overrides={"browser": "auto"}, run_id="r")
    res = b.call_browser(action="submit", params={"ref": "BK-1"}, user_id="u1")   # no explicit auth
    assert res.status == "ok" and res.effect.side_effecting is True               # NOT demoted to read-only
    accept, _ = _gate(res.effect)
    assert accept is False                                                        # non-landing still refused


def test_b_integration_auto_override_still_verifies_a_landing_send():
    b = Broker(integrations=FakeIntegrations(), overrides={"gmail": "auto"}, run_id="r")
    res = b.call_integration(toolkit="gmail", action="GMAIL_SEND_EMAIL",
                             params={"to": "bob@x.com", "subject": "Hi"}, user_id="u1")
    assert res.status == "ok" and res.effect.side_effecting is True and res.effect.expected_present is True


# --- Fix C: the money floor catches name variants (no override can loosen them to auto) ---

def test_c_money_floor_catches_name_variants():
    for act in ["payment", "make-payment", "make_payment", "paynow", "check-out", "PAYMENT"]:
        assert policy.is_money_action("browser", act) is True
        assert policy.is_refused("browser", act) is True
        # money is now REFUSE (architectural non-capability), un-overridable (was NEVER/ask-owner)
        assert policy.classify("browser", act, overrides={"browser": "auto"}) == policy.REFUSE
    assert policy.classify("browser", "navigate", overrides={"browser": "auto"}) == policy.AUTO  # control


# --- Fix D: a provably-absent (drift False) no-fingerprint browser action is HARD-refused ---

def test_d_nonlanding_no_fingerprint_browser_is_hard_refused():
    res = Broker(browser=FakeBrowser(drop_actions=("submit",)), run_id="r").call_browser(
        action="submit", params={"target": "x.com"}, user_id="u1", authorized=True)   # no ref -> fp None
    assert res.effect.expected_present is None and res.effect.drift_present is False
    unver, unverifiable = g.classify_effects([res.effect.as_gate_dict()], claimed_done=True)
    assert "browser:submit" in unver and "browser:submit" not in unverifiable   # hard refuse, not ask-owner


# --- Fix F: a read-back parser crash degrades to unverifiable (record kept), never silently accepted ---

def test_f_parse_emails_skips_malformed_items():
    # (a bare string / None item must not crash); `from` is the BARE email + `from_raw` keeps the header.
    assert _parse_emails({"emails": ["a bare string", None,
                                     {"id": "1", "to": "bob@x.com", "from_": "Al <al@x.com>",
                                      "body": "hi there"}]}) \
        == {"1": {"id": "1", "subject": "", "to": "bob@x.com", "to_raw": "bob@x.com",
                  "from": "al@x.com", "from_raw": "Al <al@x.com>",
                  "body": "hi there", "snippet": ""}}   # id exposed (trash/label fingerprint); body/snippet retained


def test_f_snapshot_crash_degrades_to_unverifiable_not_lost():
    class _Boom(FakeIntegrations):
        def snapshot(self, **kw):
            raise RuntimeError("provider returned a shape we choke on")

    res = Broker(integrations=_Boom(), run_id="r").call_integration(
        toolkit="gmail", action="GMAIL_SEND_EMAIL",
        params={"to": "bob@x.com", "subject": "Hi"}, user_id="u1", authorized=True)
    assert res.status == "ok" and res.effect is not None        # record NOT dropped
    assert res.effect.expected_present is None                  # unverifiable, not a fabricated 'landed'
    accept, _ = _gate(res.effect)
    assert accept is False                                      # the gate routes to the owner, never accepts


# --- Fix H: a lowercase exact override key is honored (not silently dropped) ---

def test_h_lowercase_exact_override_is_honored():
    assert policy.classify("browser", "submit", overrides={"browser:submit": "never"}) == policy.NEVER
    assert policy.classify("gmail", "GMAIL_SEND_EMAIL",
                           overrides={"gmail:gmail_send_email": "never"}) == policy.NEVER


# --- Fix I: a lost resume state (process restart) never authorizes on a bare label ---

def _booking_op():
    model = make_brain(steps=[{"text": "book the table"}],
                       actions={"book the table": [
                           tc("browser", action="submit", params={"ref": "BK-1", "target": "x.com"})]})
    br = FakeBrowser()
    return build(model=model, browser=br), br


def test_i_durable_continuation_survives_a_simulated_restart():
    # grants + resume-state are PERSISTED (save_continuation), so clearing the in-memory caches
    # (a 'process restart') does NOT lose the parked action — resume() rehydrates from the Store and
    # resume-at-action runs the exact approved action once.
    h, br = _booking_op()
    run = h["op"].start(Goal(text="book"))
    assert run.status is RunStatus.AWAITING_APPROVAL
    h["op"]._pending_grant.clear()                                         # simulate a 'process restart':
    h["op"]._resume_state.clear()                                         # wipe the in-memory hot caches
    h["op"]._grants.clear()
    run = h["cp"].answer(run_id=run.run_id, answer="yes")
    assert run.status is RunStatus.DONE                                    # rehydrated -> resumed exactly
    assert br.observe(action="submit", params={"ref": "BK-1"}, user_id="local")   # the approved action landed


def test_i_truly_lost_continuation_does_not_silently_authorize():
    # Fail-safe: if the durable continuation is GENUINELY gone (not just the caches), resume must NOT
    # authorize on a bare label — it re-parks for a fresh approval and nothing is submitted.
    h, br = _booking_op()
    run = h["op"].start(Goal(text="book"))
    assert run.status is RunStatus.AWAITING_APPROVAL
    h["op"]._pending_grant.clear()
    h["op"]._resume_state.clear()
    h["op"]._grants.clear()
    h["store"].save_continuation(run.run_id, {})                          # the continuation is truly lost
    run = h["cp"].answer(run_id=run.run_id, answer="yes")
    assert run.status is RunStatus.AWAITING_APPROVAL                      # re-parks, never bare-authorizes
    assert br.observe(action="submit", params={"ref": "BK-1"}, user_id="local") == {}


# --- Fix J: the seam's side-effecting set is the policy's (order/apply land + verify) ---

def test_j_policy_side_effecting_verbs_land_and_verify():
    assert is_side_effecting_action("order") and not is_side_effecting_action("navigate")
    res = Broker(browser=FakeBrowser(), run_id="r").call_browser(
        action="order", params={"ref": "ORD-1"}, user_id="u1", authorized=True)
    assert res.effect.side_effecting is True and res.effect.expected_present is True
    accept, _ = _gate(res.effect)
    assert accept is True

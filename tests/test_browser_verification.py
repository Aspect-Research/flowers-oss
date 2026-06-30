"""The observer-verification PRODUCER — the broker's browser path feeds the gate's cua branch.

The gate already SHIPS the browser/cua contract (effect_kind='cua', observer!=actor, screenshots never
verify). These tests prove the broker now PRODUCES records that exercise every branch, through the real
FakeBrowser:
  * an independently-observed submit            -> verified (accepted)
  * a submit that does NOT land                 -> hard refused (the fabricated-completion case)
  * a surface with no independent observation   -> unverifiable (ask the owner)
  * an observation by the actor itself (==actor) -> rejected as self-report, never counted as landed
Plus tiering (submit=ask, pay=never), read-only driving (no approval), and parking for approval.
"""

from __future__ import annotations

from _harness import build, make_brain, tc

from flowers import trustgate as g
from flowers.broker import Broker
from flowers.seams.browser import FakeBrowser
from flowers.types import Goal, RunStatus


def _gate(effect):
    unver, unverifiable = g.classify_effects([effect.as_gate_dict()], claimed_done=True)
    return g.gate_verdict(claimed_done=True, ok=True, stale_files=[], gate_breaking=[],
                          unverified_external=unver, unverifiable_external=unverifiable)


def _broker(**kw):
    return Broker(browser=FakeBrowser(**kw), run_id="r")


def test_independently_observed_submit_is_verified():
    res = _broker().call_browser(action="submit", params={"ref": "BK-1", "target": "venue.com"},
                                 user_id="u1", authorized=True)
    eff = res.effect
    assert eff.effect_kind == "cua" and eff.expected_present is True
    assert eff.observer and eff.actor and eff.observer != eff.actor   # INDEPENDENT provenance
    accept, _ = _gate(eff)
    assert accept is True
    assert g.verified_effects([eff.as_gate_dict()]) == ["browser:submit"]


def test_nonlanding_submit_is_hard_refused():
    res = _broker(drop_actions=("submit",)).call_browser(
        action="submit", params={"ref": "BK-1"}, user_id="u1", authorized=True)
    assert res.effect.expected_present is False        # observed ABSENT -> hard refuse, not ask-owner
    accept, _ = _gate(res.effect)
    assert accept is False


def test_no_independent_observation_is_unverifiable():
    res = _broker(no_observation=True).call_browser(
        action="submit", params={"ref": "BK-1"}, user_id="u1", authorized=True)
    assert res.effect.expected_present is None
    accept, _ = _gate(res.effect)
    assert accept is False                              # routed to the owner, never silently accepted


def test_self_sourced_observation_is_rejected_even_when_it_landed():
    # observer == actor: the agent "observing itself" can never verify, even though the effect landed.
    res = _broker(self_sourced=True).call_browser(
        action="submit", params={"ref": "BK-1"}, user_id="u1", authorized=True)
    eff = res.effect
    assert eff.expected_present is True and eff.observer == eff.actor
    accept, _ = _gate(eff)
    assert accept is False
    assert g.verified_effects([eff.as_gate_dict()]) == []   # never counted as landed


def test_read_only_browser_action_runs_without_approval():
    res = _broker(pages={"venue.com": "pick a time"}).call_browser(
        action="navigate", params={"url": "https://venue.com/book"}, user_id="u1")
    assert res.status == "ok" and "pick a time" in res.data["text"]
    assert res.effect.side_effecting is False


def test_side_effecting_browser_parks_for_approval():
    res = _broker().call_browser(action="submit", params={"ref": "BK-1"}, user_id="u1")  # unauthorized
    assert res.status == "needs_approval"
    assert res.pending == {"browser": True, "action": "submit", "params": {"ref": "BK-1"}}
    assert res.effect.phase == "deferred" and res.effect.effect_kind == "cua"


def test_money_browser_action_is_refused_not_approvable():
    # money is a categorical non-capability now: refused outright, NEVER offered for approval.
    res = _broker().call_browser(action="pay", params={"ref": "P-1"}, user_id="u1")
    assert res.status == "refused" and res.approval is None and res.grant_key == ""


def test_exact_params_bound_grant_authorizes_only_the_matching_action():
    # the grant the broker issues for ONE exact action must NOT authorize a different one — neither a
    # different ref NOR (the review's cross-destination case) the same ref to a different target.
    b = _broker()
    grant = b.call_browser(action="submit", params={"ref": "BK-1", "target": "venueA.com"},
                           user_id="u1").grant_key                     # the grant issued at the park
    diff_ref = b.call_browser(action="submit", params={"ref": "BK-2", "target": "venueA.com"},
                              user_id="u1", grants={grant})
    diff_target = b.call_browser(action="submit", params={"ref": "BK-1", "target": "attacker.com"},
                                 user_id="u1", grants={grant})
    assert diff_ref.status == "needs_approval" and diff_target.status == "needs_approval"
    same = b.call_browser(action="submit", params={"ref": "BK-1", "target": "venueA.com"},
                          user_id="u1", grants={grant})
    assert same.status == "ok" and same.effect.expected_present is True


# --- full operator path: a browser side-effect parks, then resumes-at-action on approval ---
def test_browser_submit_parks_then_resumes_at_action_and_verifies():
    model = make_brain(steps=[{"text": "book the rooftop table"}],
                       actions={"book the rooftop": [
                           tc("browser", action="submit",
                              params={"ref": "BK-9", "target": "rooftop.com"})]})
    h = build(model=model, browser=FakeBrowser())
    run = h["op"].start(Goal(text="book a rooftop table"))
    assert run.status is RunStatus.AWAITING_APPROVAL
    assert "browser:submit" in (run.pending_approval.effect_label or "")
    run = h["cp"].answer(run_id=run.run_id, answer="yes")              # approve -> resume the exact action
    assert run.status is RunStatus.DONE
    effs = h["store"].get_effects(run.run_id)
    assert any(e.effect_kind == "cua" and e.expected_present is True and e.observer != e.actor
               for e in effs)                                          # verified via an independent observer


def test_browser_submit_declined_does_not_act():
    model = make_brain(steps=[{"text": "book the rooftop table"}],
                       actions={"book the rooftop": [
                           tc("browser", action="submit", params={"ref": "BK-9"})]})
    br = FakeBrowser()
    h = build(model=model, browser=br)
    run = h["op"].start(Goal(text="book a rooftop table"))
    assert run.status is RunStatus.AWAITING_APPROVAL
    run = h["cp"].answer(run_id=run.run_id, answer="no")              # decline -> nothing is submitted
    assert run.status is RunStatus.ESCALATED
    assert br.observe(action="submit", params={"ref": "BK-9"}, user_id="local") == {}

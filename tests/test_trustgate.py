"""The moat's correctness suite — the deterministic gate, exhaustively.

If these pass, a fabricated/unverified completion is mechanically refused and a verified one accepted,
with no LLM in the path. This is the single most important test file in the repo.
"""

from __future__ import annotations

from flowers import trustgate as g

# ----------------------------------------------------------------- gate_verdict matrix

def _accept(**kw):
    base = dict(claimed_done=True, ok=True, stale_files=[], gate_breaking=[])
    base.update(kw)
    return g.gate_verdict(**base)


def test_clean_completion_accepts():
    accept, reason = _accept()
    assert accept is True
    assert "supported by the record" in reason


def test_not_claimed_refuses():
    accept, reason = g.gate_verdict(claimed_done=False, ok=True, stale_files=[], gate_breaking=[])
    assert accept is False and "did not claim completion" in reason


def test_errored_refuses():
    accept, reason = g.gate_verdict(claimed_done=True, ok=False, stale_files=[], gate_breaking=[])
    assert accept is False and "errored" in reason


def test_stale_refuses():
    accept, reason = _accept(stale_files=["notes.md"])
    assert accept is False and "stale reads" in reason and "notes.md" in reason


def test_gate_breaking_refuses():
    accept, reason = _accept(gate_breaking=["unsupported-completion"])
    assert accept is False and "reliability detector" in reason


def test_objective_unmet_refuses():
    accept, reason = _accept(objective_unmet=["has-brief"])
    assert accept is False and "objective check" in reason


def test_unverified_external_refuses():
    accept, reason = _accept(unverified_external=["gmail:GMAIL_SEND_EMAIL"])
    assert accept is False and "not reflected by the external state" in reason


def test_unverifiable_external_routes_to_owner():
    accept, reason = _accept(unverifiable_external=["browser:book_table"])
    assert accept is False and "needs your confirmation" in reason


def test_refusal_precedence_stale_before_external():
    # Stale is checked before external — a run that is both stale and unverified reports stale.
    accept, reason = _accept(stale_files=["a.md"], unverified_external=["gmail:SEND"])
    assert accept is False and "stale reads" in reason


# ----------------------------------------------------------------- classify_effects

def _eff(**kw):
    base = dict(toolkit="gmail", action="GMAIL_SEND_EMAIL", side_effecting=True, phase="forwarded")
    base.update(kw)
    return base


def test_no_claim_means_nothing_to_verify():
    assert g.classify_effects([_eff(expected_present=False)], claimed_done=False) == ([], [])


def test_composio_expected_true_is_verified():
    unver, unverifiable = g.classify_effects([_eff(expected_present=True)], claimed_done=True)
    assert unver == [] and unverifiable == []


def test_composio_expected_false_is_unverified():
    unver, _ = g.classify_effects([_eff(expected_present=True and False, drift_present=False)],
                                  claimed_done=True)
    # expected_present False -> hard refuse
    unver2, _ = g.classify_effects([_eff(expected_present=False)], claimed_done=True)
    assert unver2 == ["gmail:GMAIL_SEND_EMAIL"]


def test_composio_fingerprintless_drift_is_unverifiable_not_verified():
    # HARDENED (audit fix): a composio side-effect with NO expected fingerprint (expected_present None)
    # and bare drift can no longer be auto-verified — a concurrent unrelated writer also shows drift, so
    # accepting it would be a false positive. It now routes to the OWNER (unverifiable), never verified.
    unver, unverifiable = g.classify_effects([_eff(expected_present=None, drift_present=True)],
                                             claimed_done=True)
    assert unverifiable == ["gmail:GMAIL_SEND_EMAIL"] and unver == []
    # and it is NOT counted as a landed effect (verified_effects is the effect_landed source of truth)
    assert g.verified_effects([_eff(expected_present=None, drift_present=True)]) == []
    # the PRECISE fingerprint path still verifies (expected_present True)
    assert g.verified_effects([_eff(expected_present=True, drift_present=True)]) == ["gmail:GMAIL_SEND_EMAIL"]


def test_composio_no_readback_is_unverifiable():
    unver, unverifiable = g.classify_effects([_eff(expected_present=None, drift_present=None)],
                                             claimed_done=True)
    assert unverifiable == ["gmail:GMAIL_SEND_EMAIL"] and unver == []


def test_composio_no_effect_is_unverified():
    unver, unverifiable = g.classify_effects([_eff(expected_present=None, drift_present=False)],
                                             claimed_done=True)
    assert unver == ["gmail:GMAIL_SEND_EMAIL"] and unverifiable == []


def test_deferred_terminal_but_claimed_done_is_unverified():
    unver, _ = g.classify_effects([_eff(phase="deferred")], claimed_done=True)
    assert unver == ["gmail:GMAIL_SEND_EMAIL"]


def test_terminal_record_wins():
    # deferred then forwarded+verified for the same action_id -> verified.
    recs = [
        _eff(action_id="a1", phase="deferred"),
        _eff(action_id="a1", phase="forwarded", expected_present=True),
    ]
    unver, unverifiable = g.classify_effects(recs, claimed_done=True)
    assert unver == [] and unverifiable == []


def test_read_only_action_is_skipped():
    rec = _eff(toolkit="gmail", action="GMAIL_FETCH_EMAILS", side_effecting=False)
    assert g.classify_effects([rec], claimed_done=True) == ([], [])


# ----- browser / CUA provenance rules

def _browser(**kw):
    base = dict(toolkit="browser", action="book_table", effect_kind="cua",
                side_effecting=True, phase="forwarded")
    base.update(kw)
    return base


def test_browser_screenshot_can_never_verify():
    _, unverifiable = g.classify_effects([_browser(verification="screenshot",
                                                   expected_present=True, observer="obs", actor="agent")],
                                         claimed_done=True)
    assert unverifiable == ["browser:book_table"]


def test_browser_observer_equals_actor_is_self_report():
    _, unverifiable = g.classify_effects([_browser(expected_present=True, observer="agent", actor="agent")],
                                         claimed_done=True)
    assert unverifiable == ["browser:book_table"]


def test_browser_independent_observer_expected_true_verifies():
    unver, unverifiable = g.classify_effects(
        [_browser(expected_present=True, observer="independent_obs", actor="agent")],
        claimed_done=True)
    assert unver == [] and unverifiable == []


def test_browser_bare_drift_is_not_enough():
    # provenance-required: drift without an expected fingerprint -> unverifiable (ask owner).
    _, unverifiable = g.classify_effects(
        [_browser(expected_present=None, drift_present=True, observer="independent_obs", actor="agent")],
        claimed_done=True)
    assert unverifiable == ["browser:book_table"]


def test_browser_expected_false_is_hard_refuse():
    unver, _ = g.classify_effects(
        [_browser(expected_present=False, observer="independent_obs", actor="agent")],
        claimed_done=True)
    assert unver == ["browser:book_table"]


def test_browser_missing_side_effecting_fails_closed():
    rec = _browser(expected_present=True, observer="independent_obs", actor="agent")
    rec.pop("side_effecting")
    unver, unverifiable = g.classify_effects([rec], claimed_done=True)
    assert unver == [] and unverifiable == []  # treated as side-effecting and verified


def test_snapshot_and_snapshot_drift(tmp_path):
    f = tmp_path / "src.txt"
    f.write_text("one", encoding="utf-8")
    base = g.snapshot_dir(str(tmp_path))
    assert g.snapshot_drift(base, g.snapshot_dir(str(tmp_path))) == []  # nothing changed
    f.write_text("two", encoding="utf-8")
    assert g.snapshot_drift(base, g.snapshot_dir(str(tmp_path))) == ["src.txt"]  # changed underneath
    # a NEW file is the agent's own artifact, not drift
    (tmp_path / "artifact.md").write_text("hi", encoding="utf-8")
    assert "artifact.md" not in g.snapshot_drift(base, g.snapshot_dir(str(tmp_path)))


# ----------------------------------------------------------------- post-hoc confirmation

def test_identical_redo_and_final_retry():
    events = [
        {"kind": "write", "path": "a", "ok": True, "hash": "h1"},
        {"kind": "write", "path": "a", "ok": True, "hash": "h1"},  # identical redo
        {"kind": "run", "path": "cmd", "ok": False},
        {"kind": "run", "path": "cmd", "ok": False},               # final still failed
    ]
    assert g.has_identical_redo(events, {"a"}) is True
    assert g.final_retry_failed(events, {"cmd"}) is True
    breaking = g.confirm_gate_breaking(
        ["unsupported-completion", "forgot-own-edit", "failed-retry"],
        events, {"a"}, {"cmd"})
    assert breaking == ["unsupported-completion", "forgot-own-edit", "failed-retry"]


def test_final_retry_resolved_is_not_breaking():
    events = [{"kind": "run", "path": "cmd", "ok": False}, {"kind": "run", "path": "cmd", "ok": True}]
    assert g.final_retry_failed(events, {"cmd"}) is False


# ----------------------------------------------------------------- objective checks

def test_source_membership_catches_unfetched_cite():
    crit = [{"id": "cites", "objective_check": {"kind": "source_membership",
                                                "params": {"deliverable": "brief.md"}}}]
    bundle = {"texts": {"brief.md": "see https://example.com/a and https://example.com/b"},
              "fetched_urls": ["https://example.com/a"]}
    unmet = g.evaluate_objective_checks(crit, bundle)
    assert unmet == ["cites"]
    detail = g.describe_objective_failures(crit, bundle)
    assert "example.com/b" in detail


def test_source_membership_canonical_equivalence_passes():
    crit = [{"id": "cites", "objective_check": {"kind": "source_membership", "params": {}}}]
    bundle = {"texts": {"x": "https://www.Example.com/a/#frag"},
              "fetched_urls": ["https://example.com/a"]}
    assert g.evaluate_objective_checks(crit, bundle) == []  # www/case/slash/fragment canonicalized


def test_file_checks():
    crit = [
        {"id": "exists", "objective_check": {"kind": "file_exists", "params": {"path": "out.md"}}},
        {"id": "count", "objective_check": {"kind": "file_count", "params": {"suffix": ".md", "min": 2}}},
        {"id": "rx", "objective_check": {"kind": "regex_present",
                                         "params": {"path": "out.md", "pattern": "Hello"}}},
    ]
    bundle = {"files": ["out.md", "notes.md"], "texts": {"out.md": "Hello world"}}
    assert g.evaluate_objective_checks(crit, bundle) == []


def test_effect_landed_check():
    crit = [{"id": "sent", "objective_check": {"kind": "effect_landed",
                                               "params": {"label": "gmail:GMAIL_SEND_EMAIL"}}}]
    assert g.evaluate_objective_checks(crit, {"verified_effects": ["gmail:GMAIL_SEND_EMAIL"]}) == []
    assert g.evaluate_objective_checks(crit, {"verified_effects": []}) == ["sent"]


def test_unknown_objective_kind_is_unmet():
    crit = [{"id": "weird", "objective_check": {"kind": "no_such_kind", "params": {}}}]
    assert g.evaluate_objective_checks(crit, {}) == ["weird"]


def test_canonical_url_and_content_hash():
    assert g.canonical_url("https://WWW.Example.com:443/p/#frag") == "https://example.com/p"
    assert g.content_hash("x") == g.content_hash("x")
    assert g.content_hash(None) == "<none>"


def test_failed_attempt_forgiven_only_when_the_SAME_action_verifiably_landed():
    # Found live: a provider-failed create followed by a VERIFIED create of IDENTICAL params was
    # refused as a fabricated completion. A retry that verifiably landed answers the fabrication
    # concern — but ONLY for that exact action, matched by grant_key identity (not the bare label).
    recs = [_eff(action_id="a1", phase="failed", grant_key="cal:CREATE|deadbeef"),
            _eff(action_id="a2", phase="forwarded", drift_present=True, expected_present=True,
                 grant_key="cal:CREATE|deadbeef")]
    unver, unverifiable = g.classify_effects(recs, claimed_done=True)
    assert unver == [] and unverifiable == []


def test_verified_send_does_NOT_forgive_a_failed_send_to_a_DIFFERENT_target():
    # The label-aliasing false-accept the adversarial review caught: a verified send to bob must NEVER
    # mask a FAILED send to alice just because both are gmail:GMAIL_SEND_EMAIL. Different params ->
    # different grant_key -> the failed send is still a hard refuse. This is the core trust guarantee.
    recs = [_eff(action_id="a1", phase="forwarded", drift_present=True, expected_present=True,
                 grant_key="gmail:SEND|to-bob"),
            _eff(action_id="a2", phase="failed", grant_key="gmail:SEND|to-alice")]
    unver, _ = g.classify_effects(recs, claimed_done=True)
    assert unver == ["gmail:GMAIL_SEND_EMAIL"]


def test_failed_attempt_with_no_landed_retry_still_refused():
    unver, _ = g.classify_effects([_eff(action_id="a1", phase="failed", grant_key="g1")],
                                  claimed_done=True)
    assert unver == ["gmail:GMAIL_SEND_EMAIL"]


def test_refused_money_action_is_never_forgiven_by_a_landed_sibling():
    # A categorical money/illegal refusal must never be softened — not even if an unrelated action of
    # the same toolkit:action label verifiably landed.
    recs = [_eff(action_id="a1", phase="refused", grant_key="pay:CHARGE|x"),
            _eff(action_id="a2", phase="forwarded", drift_present=True, expected_present=True,
                 grant_key="pay:CHARGE|y")]
    unver, _ = g.classify_effects(recs, claimed_done=True)
    assert "gmail:GMAIL_SEND_EMAIL" in unver


def test_proven_absent_is_not_forgiven_by_a_failed_sibling():
    # A read-back that POSITIVELY proved a forwarded effect absent stays a hard refuse even when
    # another record of the same identity landed.
    recs = [_eff(action_id="a1", phase="forwarded", expected_present=False, grant_key="g"),
            _eff(action_id="a2", phase="forwarded", drift_present=True, expected_present=True,
                 grant_key="g")]
    unver, _ = g.classify_effects(recs, claimed_done=True)
    assert unver == ["gmail:GMAIL_SEND_EMAIL"]

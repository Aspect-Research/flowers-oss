"""The reusable usability invariants (P2 / §5) — the regression FLOOR.

Each helper encodes ONE product rule from the plan and raises ``AssertionError`` when a scenario
violates it. Scenarios import these and assert through them (plus their scenario-specific facts), so
every future scenario inherits the same floor for free: add a scenario, get the guarantees.

An "event" is a durable owner-facing log entry: ``{"kind": ..., "text": ..., ...}`` (drained via
``UX.events``). The owner-answerable kinds are ``approval`` (a mandate card, a per-action ask, or a
P0.3b draft preview), ``clarify`` (a content question), and ``escalated`` (a parked review question).
Everything else (``progress`` / ``notify`` / ``plan_announce`` / ``done``) is informational.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- event census helpers

def approvals(events) -> list[dict]:
    """Approval-kind events the owner had to answer (mandate card / per-action ask / draft preview)."""
    return [e for e in events if e.get("kind") == "approval"]


def clarifies(events) -> list[dict]:
    """Content questions the clarifier asked (counted SEPARATELY from approvals, per §5)."""
    return [e for e in events if e.get("kind") == "clarify"]


def escalations(events) -> list[dict]:
    return [e for e in events if e.get("kind") == "escalated"]


def _texts(events) -> list[str]:
    return [(e.get("text") or "") for e in events]


# --------------------------------------------------------------------------------------- one_touch

def one_touch(events, budget: int = 1) -> int:
    """RULE (§5): a fully-specified imperative costs the owner AT MOST ``budget`` approval-kind touches.

    For an explicit "email X saying Y" that is exactly ONE — and it should be the substantive draft
    preview, not a permission card (see :func:`substantive_asks`). Clarifier CONTENT questions are
    counted separately (a required field genuinely absent from the goal is allowed to prompt one) and
    are the scenario's own concern, not this budget's. Returns the approval count for further asserts.
    """
    got = approvals(events)
    assert len(got) <= budget, (
        f"one_touch: {len(got)} approval touches > budget {budget}: {[e.get('text') for e in got]}")
    return len(got)


# ------------------------------------------------------------------------------------ no_dead_ends

# The incident's signature dead end: an owner reply that the run "couldn't turn into a next step",
# answered with a plea to rephrase — a reply that neither advances nor closes the conversation.
_DEAD_END_MARKERS = (
    "rephrase what you'd like",
    "rephrase",
    "couldn't turn that into a next step",
)
# A meaningful system event either ADVANCES the work or CLOSES it — the run is never mute.
_MEANINGFUL = frozenset({"done", "stopped", "notify", "progress", "approval", "escalated",
                         "plan_announce", "clarify"})


def no_dead_ends(events) -> None:
    """RULE (§5): the system NEVER dead-ends — no rephrase-plea shape, and no reply that neither
    advances nor closes. Every owner turn is met with a meaningful system event.

    Enforced two ways: (a) no event text matches the rephrase-plea markers (the exact incident bug —
    ``operator.py`` used to answer "I couldn't turn that into a next step — can you rephrase…"); and
    (b) the run emitted at least one advancing/closing event (it is never silent). A run left parked
    forever is a run-level dead end and is caught by :func:`terminal_runs`.
    """
    for e in events:
        low = (e.get("text") or "").lower()
        for marker in _DEAD_END_MARKERS:
            assert marker not in low, f"no_dead_ends: rephrase-plea / dead-end reply present: {e!r}"
    assert any(e.get("kind") in _MEANINGFUL for e in events), (
        "no_dead_ends: the run emitted no advancing or closing event (silent run)")


# ----------------------------------------------------------------------------------- no_user_chores

# Owner-facing REQUEST shapes that hand the owner a verification chore the system could do itself
# ("can you double-check it went through?", "…or say 'retry'"). This is the exact UX P0.1 removes.
_CHORE_MARKERS = (
    "double-check", "double check",
    "can you check", "could you check", "please check",
    "can you confirm", "could you confirm", "please confirm",
    "say 'retry'", "say retry", "or retry",
)
# DOCUMENTED EXCEPTION (§4 / §5): the honest "…couldn't confirm on my end that it went through — did
# it arrive?" is PERMITTED — it is only ever asked AFTER the verification infrastructure genuinely
# could not confirm (read-back unavailable/broken), and it never offers a "retry" (which could
# double-send). It is a genuine question, not a chore, so it is deliberately NOT in _CHORE_MARKERS
# above ("did it arrive?" matches none of them). A chore is the system offloading work it can do;
# "did it arrive?" asks for evidence only the human now has.


def no_user_chores(events) -> None:
    """RULE (§5 / §2.4): never hand the owner a question whose answer the system could obtain itself.

    Flags the chore REQUEST shapes (``can you check/confirm … went through``, ``or say 'retry'``).
    The honest post-failure ``did it arrive?`` is the documented exception (see the note above) and is
    intentionally permitted.
    """
    for e in events:
        low = (e.get("text") or "").lower()
        for marker in _CHORE_MARKERS:
            assert marker not in low, f"no_user_chores: owner-facing chore present: {e!r}"


# ---------------------------------------------------------------------------------- substantive_asks

# The incident's WRONG question: a permission ask after an explicit imperative ("Mind if I send the
# email … without checking in on every step?") that never showed the owner what was going out. P0.3
# replaces it with the draft itself.
_META_PERMISSION_MARKERS = (
    "mind if i send",
    "without checking in",
    "checking in on every step",
)


def substantive_asks(events) -> None:
    """RULE (§5 / §2.3): an approval for a DELIVERING action carries the payload (the draft body /
    recipient), never only meta-permission language.

    Every approval event is checked for the banned meta-permission shape; a send-shaped approval must
    additionally carry substance (a recipient address / a non-trivial draft), so the one touch the
    owner gets is the thing itself — the draft — not "is it ok if I do a thing?".
    """
    for e in approvals(events):
        text = e.get("text") or ""
        low = text.lower()
        for marker in _META_PERMISSION_MARKERS:
            assert marker not in low, f"substantive_asks: meta-permission ask (P0.3 forbids): {e!r}"
        if "send" in low:   # a delivering approval must SHOW what it will send
            assert "@" in text or len(text.strip()) > 40, (
                f"substantive_asks: send approval lacks a payload (recipient/draft): {e!r}")


# ------------------------------------------------------------------------------------ terminal_runs

def terminal_runs(store, run_ids) -> None:
    """RULE (§5): at scenario end EVERY run is closed (done/stopped) — nothing left ESCALATED/WAITING.

    A run parked forever on the owner is a zombie (the reaper, P1.1, exists precisely so this holds).
    ``run_ids`` is a single id or an iterable of them.
    """
    if isinstance(run_ids, str):
        run_ids = [run_ids]
    for rid in run_ids:
        run = store.get_run(rid)
        assert run is not None, f"terminal_runs: run {rid} not found"
        assert run.status.value in ("done", "stopped"), (
            f"terminal_runs: run {rid} left non-terminal: {run.status.value}")


# ------------------------------------------------------------------------------------ latency_budget

def latency_budget(store, run_id, max_model_calls: int) -> int:
    """RULE (§5 / §4.3): a run spends AT MOST ``max_model_calls`` model calls (the ``usage`` ledger's
    model-kind rows). Guards the fast path's ~5 -> <=2 win against silent regressions. Returns the count.
    """
    with store._locked() as c:
        rows = c.execute("SELECT kind FROM usage WHERE run_id = ?", (run_id,)).fetchall()
    n = sum(1 for r in rows if r["kind"] == "model")
    assert n <= max_model_calls, f"latency_budget: {n} model calls > budget {max_model_calls}"
    return n

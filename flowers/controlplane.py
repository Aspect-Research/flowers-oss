"""The control plane — create a run for an inbound goal, and route owner answers / due timers.

Stateless except through the Store. It is the seam between a channel and the Operator: a channel hands it
a goal, and it creates + starts the run. Owner answers (to clarifying questions / approvals / escalations)
and due timers (awaited replies / monitor polls) are routed back into the right run's ``resume``.
"""

from __future__ import annotations

import logging

from flowers.types import Goal, RunState

_log = logging.getLogger("flowers.controlplane")


class ControlPlane:
    def __init__(self, *, store, operator):
        self.store = store
        self.operator = operator

    def intake(self, *, goal_text: str, budget_usd: float = 2.0) -> RunState:
        """Create a run for the goal and drive it synchronously (returns the settled run)."""
        return self.operator.start(Goal(text=goal_text, budget_usd=budget_usd))

    def begin(self, *, goal_text: str, budget_usd: float = 2.0) -> tuple[RunState, Goal]:
        """Create + persist the run and return ``(run, goal)`` IMMEDIATELY, without driving — so a channel
        can hand back the run-id at once and drive in the background (``drive``), streaming events live."""
        goal = Goal(text=goal_text, budget_usd=budget_usd)
        return self.operator.begin(goal), goal

    def drive(self, run: RunState, goal: Goal) -> RunState:
        """Drive a ``begin``-created run to its next settle point. Blocking (run this off the event loop)."""
        return self.operator.run_pending(run, goal)

    def answer(self, *, run_id: str, answer: str) -> RunState:
        """Record an owner answer to the run's pending question/approval, then resume."""
        run = self.store.get_run(run_id)
        if run is not None and run.pending_approval is not None:
            self.store.resolve_approval(run.pending_approval.id, answer)
        return self.operator.resume(run_id, answer=answer)

    def deliver(self, *, run_id: str) -> RunState:
        """A watched/awaited external reply arrived: resume the parked run to CHECK (not a deadline)."""
        return self.operator.resume(run_id, event="reply")

    def fail_run(self, run_id: str, reason: str) -> None:
        """Record an unexpected drive failure as an honest ESCALATED outcome — a crashed background
        drive must never leave a run silently stuck RUNNING (spinner-forever on the dashboard)."""
        self.operator.fail(run_id, reason)

    def stalled_run_ids(self) -> list[str]:
        """The run-ids of crash orphans (RUNNING/PLANNING) — a fast SQL read, no re-drive. Snapshot these
        at startup BEFORE serving so the "a RUNNING run is a crash orphan" invariant holds (captured before
        any new request can drive a run); hand them to :meth:`recover_run_ids` in the background so the slow
        re-drive never blocks the server from serving."""
        return [run.run_id for run in self.store.running_runs()]

    def recover_run_ids(self, run_ids: list[str]) -> list[RunState]:
        """Re-drive the given crash-orphan runs (a synchronous drive schedules no timer, so a run interrupted
        mid-step has nothing to wake it). Per-run isolation (like tick): one poisoned run never aborts the
        sweep. Safe to re-run: the idempotency guard prevents duplicate sends."""
        recovered: list[RunState] = []
        for run_id in run_ids:
            try:
                recovered.append(self.operator.recover(run_id))
            except Exception:
                # A deterministic re-drive failure must not abort the sweep; log it so the orphaned run is
                # visible (it stays RUNNING and a later restart re-attempts it) rather than silently lost.
                _log.exception("recover: re-drive failed for run %s", run_id)
                continue
        return recovered

    def recover_stalled(self) -> list[RunState]:
        """Snapshot crash orphans and re-drive them, synchronously, in one call. Call ONLY at startup,
        BEFORE any new request drives a run. (The lifespan splits this — snapshot before serving, re-drive
        in the background — but the combined form is what tests and simple callers use.)"""
        return self.recover_run_ids(self.stalled_run_ids())

    def tick(self, *, at: float | None = None) -> list[RunState]:
        """Poll due timers and resume their runs (a timer firing is a DEADLINE) — the serve-loop cycle."""
        resumed: list[RunState] = []
        seen: set[str] = set()
        for timer in self.operator.timers.due(at=at):
            if timer.run_id in seen:
                continue
            seen.add(timer.run_id)
            try:
                resumed.append(self.operator.resume(timer.run_id, event="timer"))
            except Exception:
                # Per-run isolation: a single malformed/poisoned run must NEVER abort the whole due-batch —
                # the other runs' timers were already claimed (fired) by due(), so an uncaught exception
                # here would silently drop them. Quarantine the bad run (it stays parked) and keep going,
                # but log it so the failure is visible rather than silently swallowed.
                _log.exception("tick: resume failed for run %s", timer.run_id)
                continue
        return resumed

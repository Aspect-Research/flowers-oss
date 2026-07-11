"""The control plane — create a run for an inbound goal, and route owner answers / due timers.

Stateless except through the Store. It is the seam between a channel and the Operator: a channel hands it
a goal, and it creates + starts the run. Owner answers (to clarifying questions / approvals / escalations)
and due timers (awaited replies / monitor polls) are routed back into the right run's ``resume``.
"""

from __future__ import annotations

import json
import logging

from flowers import runtime
from flowers.types import Goal, RunState

_log = logging.getLogger("flowers.controlplane")

# The router splits an inbound owner message into one of three lanes. 'task' is the safe default on any
# error: a run is always gated and always surfaces, so a mis-routed chat becomes a (harmless) run, never
# an un-actioned request.
_ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["task", "reply", "chat"]},
        "n": {"type": ["integer", "null"]},
    },
    "required": ["intent"],
}
_ROUTE_SYSTEM = """You route ONE incoming text message for a personal assistant that texts with its owner
and may have several TASKS running in the background. Pick exactly one lane:

- "task": the owner is asking the assistant to DO something — anything with a side effect (send/email/
  buy/schedule/create/delete), anything needing tools, live/fresh data, or multiple steps. When unsure
  between task and chat, prefer "task" — but ONLY for an ACTIONABLE request; this tiebreak never applies
  to a bare acknowledgment (see "chat").
- "reply": the message answers or redirects one of the ACTIVE TASKS listed below (an approval yes/no, an
  answer to its question, or new guidance for it). Set "n" to that task's number. When exactly ONE task
  is awaiting an answer and a short ack plausibly answers it ("ok"/"yes"/"sure" to its pending question),
  that IS a reply — prefer "reply" over "chat" in that case.
- "chat": ordinary conversation — greetings, thanks, banter, or a question the assistant can just answer,
  INCLUDING questions about what it's working on / how a task is going (you are given the active tasks).
  A message that is ONLY an acknowledgment, thanks, or a reaction emoji with NO actionable content
  ("thanks", "ok", "cool", "got it", "nice", "👍") is ALWAYS "chat" — NEVER a task — unless it plausibly
  answers a single awaiting task (then "reply", per above). Never spawn a task from a bare ack.

Return {"intent": ..., "n": <task number or null>}."""

# A pure acknowledgment / thanks / reaction emoji with NO actionable content — the kind that must never
# spawn a run. Deliberately conservative and aligned with channels.base.parse_answer's yes-vocabulary
# where it overlaps ("ok"/"thanks"): a real request ("email mom", "send it") carries a verb + object that
# is NOT in this set, so it can never be misread as an ack. Used ONLY to steer the two fail-toward-doing
# fallbacks (a reply naming no real task, and the classifier-error default) away from a spurious task.
_ACK_WORDS = frozenset({
    "thanks", "thank you", "thank u", "thanks so much", "thx", "ty", "tysm", "ok", "okay", "k", "kk",
    "cool", "got it", "gotit", "gotcha", "nice", "great", "awesome", "sweet", "perfect", "sounds good",
    "good", "np", "no worries", "cheers", "word", "bet", "roger", "aight", "alright", "love it", "amazing",
})


def _is_ack(text: str) -> bool:
    """True iff a message is a pure acknowledgment / thanks / reaction emoji with no actionable content.
    Conservative by construction: a single short phrase drawn only from ``_ACK_WORDS``, OR a message with
    no alphanumeric content at all (a bare emoji / reaction). Anything with a real verb or object has
    alphanumerics beyond the ack set and is NOT an ack — so a genuine request is never downgraded."""
    t = (text or "").strip().lower().rstrip("!.").strip()
    if not t:
        return False
    if not any(ch.isalnum() for ch in t):   # a bare emoji / reaction / punctuation — no actionable content
        return True
    return t in _ACK_WORDS

_CHAT_SYSTEM = """You are the owner's personal assistant, chatting over a messaging app. Keep it human and brief.
You are NOT executing a task right now — this is conversation. You can see the owner's active background
tasks (below) and should answer naturally about them if asked ("still drafting that email, almost there").
If the owner asks you to actually DO something with an effect, say you'll kick it off — a separate task
will handle it; do not pretend you already did it."""


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

    @staticmethod
    def _runs_blob(runs: list[dict]) -> str:
        if not runs:
            return "(no active tasks)"
        lines = []
        for r in runs:
            wait = f", waiting on you for: {r['awaiting']}" if r.get("awaiting") else ""
            lines.append(f"#{r.get('n')}: {str(r.get('goal', '')).strip()[:120]} "
                         f"[{r.get('status', '?')}{wait}]")
        return "\n".join(lines)

    def classify(self, *, text: str, runs: list[dict]) -> dict:
        """Route an inbound owner message against the open runs → {"intent": task|reply|chat, "n": int?}.
        Never raises: any failure defaults to a new task (gated + always surfaced)."""
        default = {"intent": "task", "n": None}
        if not str(text or "").strip():
            return default
        try:
            blob = f"ACTIVE TASKS:\n{self._runs_blob(runs)}\n\nMESSAGE:\n{text}"
            resp = self.operator.model.complete(
                [{"role": "system", "content": _ROUTE_SYSTEM}, {"role": "user", "content": blob}],
                role="executor",
                response_format={"type": "json_schema",
                                 "json_schema": {"name": "route", "schema": _ROUTE_SCHEMA}})
            data = json.loads(resp.content)
            intent = data.get("intent")
            if intent not in ("task", "reply", "chat"):
                return default
            n = data.get("n")
            # 'reply' is only meaningful if it names a real active task. When it doesn't, don't blindly
            # spawn a task: an ACK-shaped reply with a bad/missing ordinal is a stray acknowledgment
            # ("thanks"/"👍"), not a request — route it to chat so it never mints a spurious run. A
            # NON-ack invalid reply is still an actionable message with nowhere to land, so it keeps the
            # fail-toward-doing default (task).
            valid_ns = {r.get("n") for r in runs}
            if intent == "reply" and n not in valid_ns:
                return {"intent": "chat", "n": None} if _is_ack(text) else default
            return {"intent": intent, "n": n if isinstance(n, int) else None}
        except Exception:
            _log.exception("classify failed; defaulting to task")
            # Hard default is "task" (fail-toward-doing: a mis-routed request becomes a gated run, never a
            # dropped ask) — EXCEPT a deterministically ack-shaped text, which must never spawn a run even
            # when the classifier is down ("thanks"/"👍" -> chat).
            return {"intent": "chat", "n": None} if _is_ack(text) else default

    def chat(self, *, text: str, history: list[dict], runs: list[dict]) -> str:
        """One conversational turn — NOT a run: cheap, ungated, persona-styled, with active-run context so
        status questions answer naturally. ``history`` is [{"role": "owner"|"assistant", "text": ...}]."""
        style = runtime.reply_style()
        system = _CHAT_SYSTEM + (f"\n\nSTYLE: {style}" if style else "")
        system += f"\n\nOWNER'S ACTIVE TASKS RIGHT NOW:\n{self._runs_blob(runs)}"
        messages = [{"role": "system", "content": system}]
        for turn in (history or [])[-10:]:
            role = "assistant" if turn.get("role") == "assistant" else "user"
            messages.append({"role": role, "content": str(turn.get("text", ""))})
        messages.append({"role": "user", "content": str(text)})
        try:
            resp = self.operator.model.complete(messages, role="executor")
            return (resp.content or "").strip() or "…"
        except Exception:
            _log.exception("chat failed")
            return "sorry, my brain glitched — try again?"

    def tick(self, *, at: float | None = None) -> list[RunState]:
        """Poll due timers and resume their runs — the serve-loop cycle. Most timer kinds fire as a
        DEADLINE (event="timer"); an ``await_check`` is an interim reply CHECK (event="check") — it
        probes the inbox mid-window without triggering the window's no-replies deadline path."""
        resumed: list[RunState] = []
        # One resume per run per tick — but when a run's interim check AND its window deadline are
        # both due (due() marks BOTH fired), the deadline must win: a check-first dedup would consume
        # the deadline silently and the window would never close.
        chosen: dict[str, object] = {}
        reverifies: list[object] = []
        reaps: list[object] = []
        for timer in self.operator.timers.due(at=at):
            # A `reverify` timer (a verification_broken send's +60s re-check) and a `reap` timer (a zombie
            # escalation's TTL, P1.1) are both ORTHOGONAL to the run's drive lifecycle — one observes an
            # effect, the other closes an ignored escalation — so each is dispatched independently of the
            # one-resume-per-run dedup (which is about not double-DRIVING a run). Kind-discriminated so the
            # reaper never swallows a reverify timer and vice versa.
            if timer.kind == "reverify":
                reverifies.append(timer)
                continue
            if timer.kind == "reap":
                reaps.append(timer)
                continue
            cur = chosen.get(timer.run_id)
            if cur is None or (getattr(cur, "kind", "") == "await_check" and timer.kind != "await_check"):
                chosen[timer.run_id] = timer
        for timer in reverifies:
            try:
                r = self.operator.reverify(timer.run_id, timer.payload)
                if r is not None:
                    resumed.append(r)
            except Exception:
                _log.exception("tick: reverify failed for run %s", timer.run_id)
        for timer in reaps:
            try:
                r = self.operator.reap(timer.run_id, timer.payload)
                if r is not None:
                    resumed.append(r)
            except Exception:
                _log.exception("tick: reap failed for run %s", timer.run_id)
        for timer in chosen.values():
            try:
                event = "check" if timer.kind == "await_check" else "timer"
                resumed.append(self.operator.resume(timer.run_id, event=event))
            except Exception:
                # Per-run isolation: a single malformed/poisoned run must NEVER abort the whole due-batch —
                # the other runs' timers were already claimed (fired) by due(), so an uncaught exception
                # here would silently drop them. Quarantine the bad run (it stays parked) and keep going,
                # but log it so the failure is visible rather than silently swallowed.
                _log.exception("tick: resume failed for run %s", timer.run_id)
                continue
        return resumed

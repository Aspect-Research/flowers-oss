"""The operator — the run lifecycle that ties the spine together.

  intake -> CLARIFY (park if questions) -> PLAN -> ANNOUNCE -> DRIVE the DAG -> GATE each step
         -> accept / redirect(bounded) / park-for-approval / await(+next-batch) / monitor / escalate -> DONE.

Crash-safe by construction: run state + frozen plan live in the Store and timers in DurableTimers, so
``resume`` reconstructs everything from disk. The deterministic gate adjudicates every claimed
completion — nothing reaches DONE on an unverified/fabricated effect (never fabricate, never silently
quit). On approval, the parked executor loop is RESUMED at the exact approved action (deterministic),
not re-run.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from collections import defaultdict
from dataclasses import dataclass

from flowers import effects, memory, policy, replies, runtime, trustgate
from flowers import mandate as mandate_lib
from flowers.broker import Broker
from flowers.channels.base import parse_answer
from flowers.engine.announcer import announce_plan
from flowers.engine.clarifier import Clarifier
from flowers.engine.executor import Executor
from flowers.engine.planner import Planner
from flowers.engine.scheduler import SemanticBudget
from flowers.engine.verifier import Verifier
from flowers.seams.integrations import CAPABILITY_CATALOG
from flowers.seams.sandbox import LocalSubprocessSandbox
from flowers.seams.telemetry import NoOpTracer
from flowers.types import (
    ApprovalRequest,
    Goal,
    Plan,
    PlanStep,
    RunState,
    RunStatus,
    StepKind,
    StepResult,
    StepStatus,
    now_ts,
)

_log = logging.getLogger("flowers.operator")

_LADDER_HARD_CAP = 12      # absolute backstop per step on relentless retries; budget + deadline_ts are the
#                            REAL terminators. Each climb escalates the feedback so retries get CREATIVE.
_LADDER_HINTS = (
    "Fix the specific problem and try again.",
    "Try a DIFFERENT approach or tool for the same goal — not the same path that just failed.",
    "Try an ALTERNATE CHANNEL or a SECOND route/contact for the same effect: a different integration, the "
    "browser last-mile instead of an API, or a different person who can help.",
    "Automated paths are exhausted: if a specific HUMAN could get this done, email them to ask (a real, "
    "verified send); otherwise produce an honest hand-off describing exactly what remains and why.",
)
_HARD_RUNG = 2             # at/above this rung the executor escalates to the STRONGER model ("executor_hard")
#                            — horsepower kicks in once the cheap approach has failed + been redirected twice.
_MAX_REPLANS = 8           # backstop on whole-plan RE-ARCHITECTURES per run (lever 2). budget + deadline_ts
#                            stay the REAL terminators (relentless); this only caps no-progress churn.
_MAX_MONITOR_POLLS_HARD = 5000  # HARD ceiling on a watch's polls (infinite-loop backstop); the REAL bounds
#                                 are the watched match, a plan-set max_polls, and the run's deadline_ts.
_MAX_RECURRING_OCCURRENCES_HARD = 10000  # HARD backstop on a recurring step (a notify is free, so neither
#                                 budget nor an absent deadline bounds it) — it can't re-arm forever.
_MIN_INTERVAL_S = 60.0     # floor on a monitor interval so a tiny/negative value can't thrash tick()
_CONNECT_POLL_S = 15.0     # how often a parked-on-connect run polls for the OAuth grant to land
_AWAIT_CHECK_S = 180.0     # default interim reply-check cadence while an await window is open
_MAX_CONNECT_POLLS = 240   # backstop on connect polls (~1h @ 15s); the run's deadline_ts also bounds it
_REVERIFY_DELAY_S = 60.0   # a verification_broken send's ONE durable re-check fires +60s later (P0.1c):
#                            re-run the read-back once the broken tool may have recovered, then notify
#                            the owner ONLY if it proves the send actually missing.

_PROVIDER_LABELS = {"gmail": "Gmail", "googlecalendar": "Google Calendar"}


def _provider_label(toolkit: str) -> str:
    """A human name for a toolkit, for the connect message ('Gmail', 'Google Calendar')."""
    return _PROVIDER_LABELS.get((toolkit or "").lower(), (toolkit or "your account").replace("_", " ").title())


def _num(v, default):
    """Coerce a (possibly model-authored) value to float, fail-closed to ``default`` on junk. Plan params
    come from untrusted model JSON; a non-numeric string must NEVER raise out of the single-threaded tick
    loop (that would abort the whole due-batch — a cross-run DoS, the same shape the no-regex rule bars)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(v, default):
    """Coerce to int, fail-closed to ``default`` on junk (tolerates '3'/'3.0'). See :func:`_num`."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _default_sandbox(run_id: str):
    """A per-run workdir keyed by run_id — STABLE across process restarts (the same run reattaches to
    the same files), and not-owned so close() never deletes it."""
    path = os.path.join(tempfile.gettempdir(), f"flowers-sbx-{run_id}")
    os.makedirs(path, exist_ok=True)
    return LocalSubprocessSandbox(workdir=path)


@dataclass
class _GateOutcome:
    accept: bool
    reason: str
    needs_owner: bool   # the gate routed an effect to the owner (unverifiable) -> escalate, don't redirect
    owner_prompt: str = ""   # a friendly owner-facing message for the needs_owner case (no slug/machine phrasing)


def _friendly_effect_clause(label: str) -> str:
    """A plain-English past-tense clause for a toolkit:ACTION effect label — reads after 'I ':
    'sent the email', 'pushed the code'. For owner-facing messages (never the raw slug)."""
    tk, _, act = (label or "").partition(":")
    a = act.upper()
    tk = tk.lower()
    if tk == "gmail" and "SEND" in a:
        return "sent the email"
    if tk in ("googlecalendar", "calendar") and ("CREATE" in a or "ADD" in a):
        return "added the calendar event"
    if tk == "browser":
        return "submitted that on the site"
    return f"ran {act.replace('_', ' ').strip().lower() or 'that'}"


def _unverifiable_owner_msg(unverifiable: list[str]) -> str:
    """The needs-your-confirmation escalation, worded as a light, honest question rather than a chore
    (P1.4): 'I sent the email, but couldn't confirm on my end that it actually went through — did it
    arrive?' No slug, no 'read-back' jargon, no 'double-check' errand. The 'did it arrive?' shape lines up
    with the P0.2 reply vocabulary: 'it arrived'/'yes' confirm, and a bare 'no'/'nothing arrived' denies
    (the denied->resend path — a bare 'no' answers THIS question as "it didn't", so on needs_owner_confirm
    it maps to denied, not stop; see ``_deterministic_intent``). Deliberately offers NO 'retry' — the action
    already FORWARDED (landed_effects), so a retry after a landed send risks a DOUBLE-send; the owner's own
    confirmation is the evidence we're missing here."""
    clauses: list[str] = []
    for lbl in unverifiable:
        c = _friendly_effect_clause(lbl)
        if c not in clauses:
            clauses.append(c)
    if not clauses:
        did, it = "did that", "it"
    elif len(clauses) == 1:
        did, it = clauses[0], "it"
    else:
        did = ", ".join(clauses[:-1]) + f" and {clauses[-1]}"
        it = "they"
    return (f"I {did}, but couldn't confirm on my end that {it} actually went through — "
            f"did {it} arrive?")


def _is_verification_broken(eff) -> bool:
    """True iff an effect is the ``verification_broken`` state the broker records: a FORWARDED
    side-effect whose own read-back tool errored non-retryably (``detail['readback_error']``), so it is
    neither verified nor proven-missing — the SEND is fine, the CHECK is broken. It is NOT an owner
    chore: the operator treats it as landed for step completion and re-checks it on a timer."""
    return bool(getattr(eff, "side_effecting", False) and eff.phase == "forwarded"
                and eff.expected_present is None and (eff.detail or {}).get("readback_error"))


def _reverify_missing_msg(labels: list[str]) -> str:
    """The honest owner notice when a +60s re-check PROVES a send that we'd reported as delivered was
    actually not there — worded like a person owning the correction, no slug, no 'read-back' jargon.
    Deliberately promises NO retry: the escalation handler can't safely resend (a byte-identical resend
    is idempotency-short-circuited; retry wiring is P0.2), so it opens the question instead of a mechanism
    it can't honour."""
    clauses: list[str] = []
    for lbl in labels:
        c = _friendly_effect_clause(lbl)
        if c not in clauses:
            clauses.append(c)
    did = clauses[0] if len(clauses) == 1 else (", ".join(clauses[:-1]) + f" and {clauses[-1]}"
                                                if clauses else "did that")
    return (f"Quick correction — I'd said I {did}, but on a second check it doesn't look like it "
            f"actually went out. How do you want to handle it?")


# ---- escalation-reply intents (P0.2) -------------------------------------------------------------
# An escalation is a PARKED conversation; the owner's reply carries one of these intents. It is
# classified DETERMINISTICALLY first (``_deterministic_intent``), and only a reply no shortcut fits
# costs ONE cheap model call (``Operator._classify_escalation_reply``) that HARD-DEFAULTS to
# "guidance" — the safe, replan-only intent that can never authorize anything — on any error/invalid
# output. ``confirmed``/``denied`` only carry send-attestation meaning on the two send-confirmation
# escalations (needs_owner_confirm / reverify_proven_missing); on any other escalation they fall
# through to guidance, preserving the pre-P0.2 replan behavior.
_INTENT_SCHEMA = {
    "type": "object",
    "properties": {"intent": {"type": "string",
                              "enum": ["confirmed", "denied", "retry", "stop", "guidance"]}},
    "required": ["intent"],
}
_INTENT_SYSTEM = """You classify ONE reply an owner texted back to a personal assistant that had paused a
task and asked the owner a question. The assistant either (a) sent/did something it could NOT confirm on
its own and asked the owner to double-check it actually went through, or (b) is asking how to proceed.
Pick the single best label for the owner's reply:

- "confirmed": the owner says it DID go through / arrived / worked ("it was sent", "got it", "yep, arrived").
- "denied": the owner says it did NOT arrive / never showed up ("nothing arrived", "didn't get it").
- "retry": the owner wants it attempted again / re-sent.
- "stop": the owner wants to drop it / leave it / never mind.
- "guidance": anything else — new instructions, a redirect, or an ambiguous reply.

Return {"intent": ...}."""

# Model-free shortcuts for the common replies (matched case-insensitively on the trimmed reply, trailing
# '.'/'!' stripped). Explicit confirm/deny/retry tokens are checked BEFORE the bare-decline stop rule so
# "no email here" reads as denied, not stop. A BARE "yes" is confirmation only on the "did it go through?"
# question (needs_owner_confirm); the reverify "how do you want to handle it?" makes a bare yes genuinely
# ambiguous, so it falls to the model / guidance default. Symmetrically, a BARE "no" is a DENIAL ("it
# didn't arrive") only on that same needs_owner_confirm question; on any other escalation (the open-ended
# reverify question, or a non-send failure) a bare "no" keeps its pre-P0.2 meaning: stop.
_STOP_WORDS = frozenset({"stop", "leave it", "nevermind", "never mind", "drop it", "forget it"})
_RETRY_WORDS = frozenset({"retry", "try again", "resend", "re-send", "send it again", "send again"})
_CONFIRM_WORDS = frozenset({"it was sent", "sent", "it sent", "got it", "it worked", "worked", "all good",
                            "yes it went through", "it went through", "went through", "received",
                            "it arrived", "✓", "✔", "👍"})
_DENY_WORDS = frozenset({"nothing arrived", "didn't get it", "didnt get it", "no email here",
                         "never arrived", "didn't arrive", "didnt arrive", "nothing here",
                         "never got it", "didn't come through", "didnt come through"})
_YES_WORDS = frozenset({"yes", "yep", "yeah", "yup", "ya"})
# The bare "no"-family (mirrors channels.base._NO_FAMILY). Meaning is question-dependent — see the
# needs_owner_confirm branch in ``_deterministic_intent``.
_NO_WORDS = frozenset({"no", "nope", "nah", "n"})


def _deterministic_intent(ans: str, reason_code: str) -> str | None:
    """A model-free intent for the common escalation replies, or None to fall to the cheap model call."""
    t = (ans or "").strip().lower().rstrip(".!")
    if not t:
        return None
    if t in _RETRY_WORDS:
        return "retry"
    if t in _CONFIRM_WORDS:
        return "confirmed"
    if t in _DENY_WORDS:
        return "denied"
    # On the "did it arrive?" send-confirmation escalation (needs_owner_confirm), a BARE "no"-family reply
    # is the owner ANSWERING that question — it did NOT arrive — which is DENIED (the P0.2 denied->resend
    # path), NOT a request to stop. Checked BEFORE the bare-decline stop rule so this "no" routes to the
    # resend, not a stop. On EVERY other escalation (the reverify "how do you want to handle it?" open
    # question, or a non-send failure) a bare "no" falls through and keeps its pre-P0.2 meaning: stop.
    # Explicit stop tokens ("stop"/"leave it"/...) always stop, even here (they are not in _NO_WORDS).
    if reason_code == "needs_owner_confirm" and t in _NO_WORDS:
        return "denied"
    # A BARE decline stops the run (the pre-P0.2 heuristic, preserved verbatim: 'no'/'stop'/'cancel' etc.
    # or any short <=3-word decline-prefixed reply). A longer decline-prefixed sentence is GUIDANCE, not a
    # stop (a redirect that merely starts with "No" must steer the run, not kill it).
    if t in _STOP_WORDS or (parse_answer(ans)["decision"] == "no" and len(ans.split()) <= 3):
        return "stop"
    if t in _YES_WORDS:
        return "confirmed" if reason_code == "needs_owner_confirm" else None
    return None


class Operator:
    def __init__(self, *, store, model, search, integrations, timers, browser=None,
                 sandbox_factory=None, tracer=None, channel=None, overrides=None,
                 budget: SemanticBudget | None = None,
                 clarify_enabled: bool = True, announce_enabled: bool = True,
                 mandate_enabled: bool = True, verifier_enabled: bool = True,
                 verify_attempts: int = 1, verify_delay: float = 0.0,
                 send_preview: str = "always", escalation_ttl_h: float = 24.0,
                 fast_path_enabled: bool = True):
        self.store = store
        self.model = model
        self.search = search
        self.integrations = integrations
        self.browser = browser           # no-API last-mile; None until a browser backend is wired
        self.timers = timers
        self.tracer = tracer or NoOpTracer()
        self.channel = channel
        self.overrides = overrides or {}
        self.budget = budget or SemanticBudget()
        self.planner = Planner(model)
        self.clarifier = Clarifier(model, enabled=clarify_enabled)
        self.verifier = Verifier(model, enabled=verifier_enabled)
        self.executor = Executor(budget=self.budget)
        self.announce_enabled = announce_enabled
        # The Mandate: when on (default), a planner-proposed autonomy scope is shown to the owner as a
        # single editable card (AWAITING_GO) and, once approved, widens authorization for in-scope
        # reversible actions. Default-empty (no proposed mandate, or declined) -> today's ask-everything.
        self.mandate_enabled = mandate_enabled
        # Single-action fast path (P1.3): when on (default), a self-contained 'email <one named address>
        # saying <content>' skips the clarifier + planner LLM calls for a deterministic compose->send
        # template plan (~5 -> <=2 model calls). Plumbed from FLOWERS_FAST_PATH in app.py. Requires
        # mandate_enabled (the send auto-commits via owner_grant); any goal the detector doesn't match
        # runs the UNCHANGED full pipeline.
        self.fast_path_enabled = fast_path_enabled
        # Draft preview (P0.3b): "always" (default) surfaces an auto-committed delivering send as a draft
        # the owner confirms (the single touch); "never" sends it directly (zero touches). Plumbed from
        # FLOWERS_SEND_PREVIEW in app.py, like the other FLOWERS_* knobs. Only affects OWNER-GRANT
        # (auto-committed) mandates — a card-approved mandate always sends silently (owner saw the plan).
        self.send_preview = send_preview or "always"
        # Zombie-run reaper TTL (P1.1): how long an ESCALATED run may sit unanswered before it is closed
        # quietly. Plumbed from FLOWERS_ESCALATION_TTL_H in app.py; junk/non-positive falls back to 24h
        # (never a zero/negative TTL that would reap the instant a run escalates).
        _ttl = _num(escalation_ttl_h, 24.0)
        self.escalation_ttl_h = _ttl if _ttl > 0 else 24.0
        self.verify_attempts = verify_attempts   # read-back retries (live: tolerate provider lag)
        self.verify_delay = verify_delay
        self._sandbox_factory = sandbox_factory or _default_sandbox
        self._sandboxes: dict = {}
        self._grants: dict = {}          # run_id -> set of authorized grant_keys (persists for the run)
        self._pending_grant: dict = {}   # run_id -> grant_key awaiting the owner's approval
        self._resume_state: dict = {}    # run_id -> the parked executor resume state (resume-at-action)
        self._connect: dict = {}         # run_id -> {toolkit,url,polls} for a parked-on-connect run
        self._fetched: dict = {}         # run_id -> set of URLs actually fetched (source_membership check)
        self._discovered: dict = {}      # run_id -> set of recipients admitted to scope by fetch-provenance
        self._guidance_pleaded: set = set()  # run_ids that already got the "rephrase?" plea once — never
        #                                twice in a row (P0.2): a second unparseable guidance closes DONE.
        # Per-run drive mutex: the served app can enter a run from TWO threads at once — the tick
        # poller (a due timer -> resume) and a request worker (an owner answer -> resume, or a fresh
        # drive). Without exclusion they double-drive the same run: two concurrent step loops, a
        # duplicate outreach batch, a racing send. An RLock (re-entrant within one thread, so the
        # public entry point -> internal _drive nesting is fine) serializes drives per run_id.
        self._run_locks: dict = defaultdict(threading.RLock)
        self._run_locks_guard = threading.Lock()

    # ---- durable continuation (grants + parked resume-state survive a process restart) ----
    def _persist_continuation(self, run_id: str) -> None:
        """Checkpoint the run's authorized grants + parked resume-state to the Store so a FRESH process
        can resume-at-action EXACTLY (not re-derive under a bare grant — the P3-review cross-restart
        gap). The in-memory dicts are the hot cache; the Store is the source of truth across a restart."""
        self.store.save_continuation(run_id, {
            "grants": sorted(self._grants.get(run_id, set())),
            "pending_grant": self._pending_grant.get(run_id),
            "resume_state": self._resume_state.get(run_id),
            # the parked-on-connect state (which account to poll + how many polls) survives a restart, so a
            # fresh process keeps polling Arcade for the grant and resumes-at-action when it lands.
            "connect": self._connect.get(run_id),
            # the fetched-URL set is part of the run's hot cache too — persist it so a citing step that
            # resumes in a FRESH process after a park still sees the URLs an earlier step fetched (else
            # source_membership would falsely refuse a legitimate deliverable across a restart).
            "fetched": sorted(self._fetched.get(run_id, set())),
            # provenance-admitted recipients survive a park/restart too (else a discovered contact would
            # drop off the effective scope and the next-batch send to it would wrongly re-park).
            "discovered": sorted(self._discovered.get(run_id, set())),
        })

    def _load_continuation(self, run_id: str) -> None:
        """Rehydrate the in-memory cache from the Store — no-op if already cached (a live process keeps
        its hot dicts; only a restarted process reads back from disk)."""
        if (run_id in self._grants or run_id in self._pending_grant or run_id in self._resume_state
                or run_id in self._connect):
            return
        data = self.store.get_continuation(run_id)
        if not data:
            return
        if data.get("grants"):
            self._grants[run_id] = set(data["grants"])
        if data.get("pending_grant"):
            self._pending_grant[run_id] = data["pending_grant"]
        if data.get("resume_state") is not None:
            self._resume_state[run_id] = data["resume_state"]
        if data.get("connect") is not None:
            self._connect[run_id] = data["connect"]
        if data.get("fetched"):
            self._fetched[run_id] = set(data["fetched"])
        if data.get("discovered"):
            self._discovered[run_id] = set(data["discovered"])

    @contextlib.contextmanager
    def _locked_run(self, run_id: str):
        """Hold the per-run drive mutex. The tiny guarded lookup mints one RLock per run_id lazily."""
        with self._run_locks_guard:
            lock = self._run_locks[run_id]
        with lock:
            yield

    # ================================================================ entry points
    def begin(self, goal: Goal) -> RunState:
        """Create + persist the run as PENDING and return it IMMEDIATELY, without driving. Lets a channel
        hand the run-id to the owner right away and drive in the background (via :meth:`run_pending`), so
        plan/progress events stream LIVE instead of arriving in a batch after a blocking synchronous run."""
        run = RunState(run_id=goal.run_id, goal_text=goal.text,
                       budget_usd=goal.budget_usd, status=RunStatus.PENDING)
        # Wall-clock relentlessness budget: convert the goal's max_runtime_s to an absolute deadline on the
        # INJECTABLE timer clock (deterministic in tests; real wall-clock in prod). The give-up sites keep
        # trying until budget OR this deadline is exhausted, instead of quitting after 2-3 attempts.
        if goal.max_runtime_s is not None:   # distinguish 0 ("stop ~now") from None ("no time bound")
            run.deadline_ts = self.timers.now() + max(0.0, _num(goal.max_runtime_s, 0.0))
        self.store.create_run(run)
        return run

    def run_pending(self, run: RunState, goal: Goal) -> RunState:
        """Drive a just-``begin``-created run: screen -> clarify -> plan -> execute. Safe to run in a
        background thread; every event it emits streams to the channel as it happens."""
        with self._locked_run(run.run_id):
            return self._run_pending(run, goal)

    def _run_pending(self, run: RunState, goal: Goal) -> RunState:
        # Illegal/disallowed-intent pre-screen: a goal asking for something illegal is hard-refused at
        # INTAKE — before any planning or model call — deterministically (no LLM in the refuse path). This
        # catches an illicit GOAL whose individual steps might each look benign (is_refused is action-scoped
        # and would miss it). Money is a separate non-capability the planner never reaches for.
        if policy.is_disallowed_text(run.goal_text):
            self._escalate(run, "refused: I can't help with this — it asks for something illegal, "
                                "which flowers will not do.")
            return run
        # Single-action fast path (P1.3): a self-contained "email <one named address> saying <content>"
        # skips the clarifier AND planner LLM calls — a deterministic template plan + the owner-grant
        # auto-mandate drive straight to the executor's compose, and the draft preview IS the plan
        # announcement (§4.3). The detector is fail-closed (multiple recipients / no content / a second
        # task / any pre-existing constraint -> None), so ANY doubt falls through to the unchanged
        # pipeline below. Runs AFTER the disallowed pre-screen (the refuse floor holds, fast or slow).
        if self.fast_path_enabled and self.mandate_enabled:
            fs = mandate_lib.fast_path_goal(goal)
            if fs is not None:
                settled = self._fast_plan_and_drive(run, goal, fs)
                if settled is not None:
                    return settled
        questions = self.clarifier.clarify(goal, broker=self._broker(run),
                                           memory=self.store.get_memory())
        if questions:
            apr = ApprovalRequest(run_id=run.run_id, kind="clarify",
                                  prompt="Before I start, a couple of questions:\n- " + "\n- ".join(questions),
                                  options=[])
            self._park(run, RunStatus.CLARIFYING, apr)
            return run
        return self._plan_and_drive(run, goal)

    def start(self, goal: Goal) -> RunState:
        """Create AND drive synchronously (returns the settled run). The programmatic/test entry point;
        channels that want live streaming use :meth:`begin` + :meth:`run_pending` in the background."""
        return self.run_pending(self.begin(goal), goal)

    def resume(self, run_id: str, *, answer: str | None = None, event: str | None = None) -> RunState:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"unknown run {run_id}")
        # A message to a run that is actively DRIVING is handled WITHOUT the per-run drive lock: the
        # drive thread holds that lock for the whole step, and the ack must be immediate, not blocked
        # behind a minutes-long model call. add_note + _emit are each independently thread-safe (the
        # store serializes its own writes), and a note is context-only — it can never mutate run state.
        if (run.status in (RunStatus.RUNNING, RunStatus.PLANNING, RunStatus.PENDING)
                and answer and answer.strip()):
            self.store.add_note(run.run_id, answer.strip())
            self._emit(run, "notify", "noted — I'm mid-task; I'll fold that in at my next step.")
            return run
        # A PARKED run has no active drive, so this lock is uncontended by a drive — its job is to
        # serialize two resumers of the SAME parked run (the tick poller firing a timer AND the owner
        # answering at the same instant), which would otherwise double-drive it.
        with self._locked_run(run_id):
            return self._resume(run_id, answer=answer, event=event)

    def _resume(self, run_id: str, *, answer: str | None = None, event: str | None = None) -> RunState:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"unknown run {run_id}")
        goal = self._goal_of(run)
        self._load_continuation(run_id)   # rehydrate grants/resume-state if this is a fresh process

        if run.status is RunStatus.CLARIFYING:
            ans = answer if answer is not None else self._answer_for(run)
            if ans is None:
                return run                              # still waiting
            # Clarify path: the goal_text was disallowed-screened at start(), but illicit intent can
            # arrive via the ANSWER to a clarifying question (a benign goal -> a question -> an illicit
            # reply reaches the planner with no deterministic floor). Re-run the same no-LLM pre-screen on
            # the answer here, before any planning/model call.
            if policy.is_disallowed_text(ans):
                self._escalate(run, "refused: I can't help with this — it asks for something illegal, "
                                    "which flowers will not do.")
                return run
            goal.constraints["clarification"] = ans
            run.pending_approval = None
            return self._plan_and_drive(run, goal)

        if run.status is RunStatus.AWAITING_APPROVAL:
            appr = run.pending_approval
            if appr is None:
                return run
            is_undo = appr.kind == "undo"   # a mandate undo-window soft-confirm (auto-releases on its timer)
            is_preview = appr.kind == "preview"   # a P0.3b draft preview — distinct reply-semantics below
            stored = self._answer_for(run)
            if is_undo and event == "timer" and answer is None and stored is None:
                decision = "yes"            # the undo window elapsed with no veto -> release the send
            else:
                decision = parse_answer((answer if answer is not None else stored) or "")["decision"]
            if is_undo:
                self.timers.cancel_for_run(run.run_id)   # the window is resolved either way
            # Draft-preview reply-semantics (P0.3b): yes -> send (falls through to the grant+resume path);
            # a bare no -> stop the run cleanly; ANY OTHER reply -> treat as edit guidance (revise the draft
            # and re-preview). Only "yes" authorizes the exact previewed draft; edits never send the old one.
            if is_preview and decision != "yes":
                raw = (answer if answer is not None else stored) or ""
                return self._stop_preview(run) if decision == "no" else self._revise_preview(run, goal, raw)
            if decision == "yes":
                # ONE answer parser (channels.base.parse_answer) so the owner's "yes"/"do it"/"send it"
                # vocabulary is identical on every surface (web + SMS) and can't silently diverge.
                # Authorize ONLY with the exact fingerprint-bound grant key the broker issued (never the
                # bare effect_label). Grants + resume-state are now DURABLE (save_continuation), so even
                # a fresh process resumes-at-action exactly; a genuinely-lost state re-parks for approval.
                gk = self._pending_grant.pop(run.run_id, None)
                if gk:
                    self._grants.setdefault(run.run_id, set()).add(gk)
                rs = self._resume_state.pop(run.run_id, None)
                self._persist_continuation(run.run_id)
                if not is_undo:   # an auto-released undo is not a deliberate per-action approval -> don't learn
                    self._record_trust(run, appr)
                run.pending_approval = None
                if rs is not None:
                    return self._resume_step(run, goal, rs)   # resume-at-action: run the approved action exactly
                self._unpark_step(run)
                return self._drive(run, goal)
            self._escalate(run, f"owner declined: {appr.effect_label or appr.prompt}",
                           reason_code="owner_declined")
            return run

        if run.status is RunStatus.AWAITING_GO:
            # The owner answered the mandate card. YES -> commit exactly the proposed scope (which rode on
            # the persisted plan, so it survives a restart). Anything else -> decline to ask-everything
            # (NOT an escalate — declining the mandate just means "keep approving each action").
            ans = answer if answer is not None else self._answer_for(run)
            if ans is None:
                return run                                    # still waiting on the owner
            plan = self.store.get_plan(run.run_id)
            proposed = (plan.mandate or {}) if plan is not None else {}
            run.mandate = proposed if parse_answer(ans)["decision"] == "yes" else {}
            run.mandate_counts = mandate_lib.new_counts()
            run.pending_approval = None
            run.status = RunStatus.RUNNING
            self.store.save_run(run)
            return self._drive(run, goal)

        if run.status is RunStatus.AWAITING_CONNECT:
            return self._resume_connect(run, goal, event=event)

        if run.status is RunStatus.WAITING:
            return self._resume_waiting(run, goal, event=event)

        if run.status is RunStatus.ESCALATED:
            return self._resume_escalated(run, goal, answer)

        if (run.status in (RunStatus.RUNNING, RunStatus.PLANNING, RunStatus.PENDING)
                and answer and answer.strip()):
            # A message while the run is mid-drive: NEVER dropped. Queue it durably for the next
            # decision point (next step's feedback / the next replan) and acknowledge at once — the
            # dashboard shows a spinner that only clears when an event arrives. Context only: a note
            # cannot mint a grant or bypass an approval. This thread must NOT call _drive (the drive
            # thread is still running this run — a second drive would race it).
            self.store.add_note(run.run_id, answer.strip())
            self._emit(run, "notify", "noted — I'm mid-task; I'll fold that in at my next step.")
            return run

        return run

    def fail(self, run_id: str, reason: str) -> None:
        """Public crash surface: escalate a run that an unexpected exception left in-flight, so the
        failure is an honest parked outcome (answerable, visible) rather than a silent stuck-RUNNING.
        No-op if the run is already terminal or parked — the exception may have raced a legitimate
        settle (e.g. the drive parked the run for approval and THEN the thread died)."""
        with self._locked_run(run_id):
            run = self.store.get_run(run_id)
            if run is None or run.status not in (RunStatus.RUNNING, RunStatus.PLANNING,
                                                 RunStatus.PENDING):
                return
            self._escalate(run, reason, reason_code="internal_error")

    def recover(self, run_id: str) -> RunState:
        """Crash recovery: a run left in a synchronous in-flight state (RUNNING or PLANNING) by a process
        that died has NO parked timer to wake it (drive/plan are synchronous), so nothing would ever re-enter
        it. Re-drive it from its persisted plan — any step still RUNNING (interrupted, never completed) is
        reset to PENDING and re-driven. SAFE because the run-scoped idempotency guard guarantees a side-effect
        already VERIFIED-landed is NOT re-sent on the re-drive (the broker seeds its forwarded-gk set from the
        effect ledger). A run that crashed DURING planning has no plan yet (no effects happened) -> escalate
        honestly. A run in any other state is returned untouched (parked/waiting runs have their own resume)."""
        with self._locked_run(run_id):
            return self._recover(run_id)

    def _recover(self, run_id: str) -> RunState:
        run = self.store.get_run(run_id)
        if run is None:
            raise KeyError(f"unknown run {run_id}")
        if run.status not in (RunStatus.RUNNING, RunStatus.PLANNING):
            return run
        self._load_continuation(run_id)        # rehydrate grants/resume-state in this fresh process
        goal = self._goal_of(run)
        plan = self.store.get_plan(run.run_id)
        if plan is None:
            # PLANNING-crash (or a lost plan): nothing was driven yet, so there is no work to resume —
            # surface it honestly rather than leave it a silent permanent orphan.
            self._escalate(run, "recovered after an interruption during planning — please resend the request")
            return run
        reset = False
        for s in plan.steps:
            if s.status is StepStatus.RUNNING:
                s.status = StepStatus.PENDING   # interrupted, not completed -> re-drive (idempotency-safe)
                reset = True
        if reset:
            self.store.save_plan(run.run_id, plan)
        # Orient the owner: the durable event log means a reconnected dashboard replays the pre-crash
        # timeline — this marks where the old process ended and the recovery re-drive picks up.
        self._emit(run, "progress", "recovering after a restart — resuming where I left off")
        return self._drive(run, goal)

    # ================================================================ planning / driving
    def _fast_plan_and_drive(self, run: RunState, goal: Goal, fs) -> RunState | None:
        """Drive the P1.3 single-action fast path: a deterministic TEMPLATE plan (no planner LLM call) of
        ONE compose-and-send step, with the owner-grant mandate committed EXACTLY as :meth:`_plan_and_drive`
        would commit a planner-proposed one (same ``owner_grant`` derivation, same ``mandate_auto`` flag, so
        the P0.3 preview + P0.2 escalation machinery downstream is byte-identical). No ``plan_announce`` —
        the draft preview IS the announcement (§4.3): it already renders standalone (recipient + the full
        draft), so suppressing the separate announce loses nothing. Returns None when ``owner_grant``
        declines the template proposal (fail-closed — the caller falls through to the unchanged full
        pipeline; the fast path never authorizes on its own say-so). Everything after this method is the
        EXISTING machinery: the executor's normal compose+send, the broker preview park, the read-back
        gate, and — on any wobble (a done-claim without a send, a gate refusal, a replan) — the normal
        redirect/replan path, planner included (the <=2-call budget is the happy path only)."""
        proposed = {
            "action_types": [fs.action_label],
            "recipient_scope": [fs.recipient],
            "magnitude_caps": {"max_sends": 1, "per_domain": 1, "per_recipient": 1},
            "irreversibility_ceiling": "ASK",
            "done_definition": f"the email to {fs.recipient} is sent and verified",
            "undo_seconds": 0,
        }
        granted = mandate_lib.owner_grant(proposed, goal)
        if granted is None:
            return None                       # anything off -> the unchanged full pipeline (never force it)
        run.status = RunStatus.PLANNING
        self.store.save_run(run)
        # The template step mirrors a planner-authored send step field-for-field: a generic step whose
        # ``produces`` label became the ``effect_landed`` done-criterion (planner._parse_steps emits exactly
        # this shape), so the gate REQUIRES the send actually landed — an executor that claims done without
        # sending is refused and the normal redirect/replan machinery takes over.
        step = PlanStep(index=0, text=f"compose and send the email to {fs.recipient}",
                        kind=StepKind.GENERIC,
                        done_criteria=[{"id": "effect_landed",
                                        "objective_check": {"kind": "effect_landed",
                                                            "params": {"label": fs.action_label}}}])
        plan = Plan(steps=[step], goal_text=goal.text, mandate=proposed)
        self.store.save_plan(run.run_id, plan)   # the proposal rides the plan, like a planner-proposed one
        run.mandate = granted
        run.mandate_auto = True   # drives the draft-preview single touch (P0.3b), exactly as owner-grant
        run.fast_path = True      # skips the finish-time verifier (see _handle_step_result) + marks the run
        run.mandate_counts = mandate_lib.new_counts()
        run.status = RunStatus.RUNNING
        self.store.save_run(run)
        return self._drive(run, goal)

    def _plan_and_drive(self, run: RunState, goal: Goal) -> RunState:
        run.status = RunStatus.PLANNING
        self.store.save_run(run)
        plan = self.planner.plan(goal, broker=self._broker(run), catalog=CAPABILITY_CATALOG,
                                 memory=self.store.get_memory())
        self.store.save_plan(run.run_id, plan)
        proposed = plan.mandate or {}     # the planner's proposed autonomy scope (rides on the plan)
        if self.announce_enabled:
            self._emit(run, "plan_announce",
                       announce_plan(plan, mandate=proposed))
        # The single editable mandate card: if the planner proposed an autonomy scope, ask the owner to
        # grant it ONCE (AWAITING_GO) before driving. On approval the run.mandate is committed and in-scope
        # actions auto-authorize; declining (or no mandate) keeps today's per-action approval.
        if self.mandate_enabled and proposed:
            # OWNER-GRANT (P0.3a): if the goal ITSELF already authorizes exactly this scope — an explicit
            # imperative naming the send + recipients the owner named (in the goal text OR their own
            # clarifier reply, both carried on ``goal``; a clarifier-supplied recipient counts as
            # named-by-owner via goal_named_recipients over goal.constraints) — commit a TIGHT mandate
            # (exactly the named recipients, one send each) and skip the card. Anything broader -> card.
            granted = mandate_lib.owner_grant(proposed, goal)
            if granted is not None:
                run.mandate = granted
                run.mandate_auto = True   # drives the draft-preview single touch (P0.3b)
                run.mandate_counts = mandate_lib.new_counts()
                run.status = RunStatus.RUNNING
                self.store.save_run(run)
                return self._drive(run, goal)
            apr = ApprovalRequest(run_id=run.run_id, kind="mandate",
                                  prompt=mandate_lib.render_card(proposed), options=["yes", "no"])
            self._park(run, RunStatus.AWAITING_GO, apr)
            # mandate=True marks this as the autonomy card (not a per-action approval) so a channel can
            # render it with its OWN yes/no instruction — an explicit flag, not a fragile text match.
            self._emit(run, "approval", apr.prompt, mandate=True)
            return run
        run.status = RunStatus.RUNNING
        self.store.save_run(run)
        return self._drive(run, goal)

    def _drive(self, run: RunState, goal: Goal) -> RunState:
        plan = self.store.get_plan(run.run_id)
        while True:
            ready = plan.ready_indices()
            if not ready:
                if plan.is_complete():
                    return self._finalize(run, plan)
                self.store.save_run(run)            # a step is parked/waiting; stop here
                return run
            step = plan.steps[ready[0]]
            if step.kind is StepKind.AWAIT_REPLIES:
                self._park_wait(run, plan, step, kind="await_replies")
                return run
            if step.kind is StepKind.MONITOR:
                self._park_wait(run, plan, step, kind="monitor")
                return run
            if step.kind is StepKind.RECURRING:
                self._park_wait(run, plan, step, kind="recurring")
                return run
            outcome = self._run_generic_step(run, goal, plan, step)
            if outcome in ("parked", "escalated"):
                return run
            plan = self.store.get_plan(run.run_id)   # advanced -> next ready step

    def _run_generic_step(self, run, goal, plan, step) -> str:
        step.status = StepStatus.RUNNING
        self.store.save_plan(run.run_id, plan)
        sandbox = self._sandbox(run.run_id)
        if "_box_baseline" not in step.params:
            # box-observation baseline: the read-set BEFORE the step, via the sandbox's OWN snapshot()
            # (works for a local fs AND a remote E2B microVM — never reads the wrong host filesystem).
            step.params["_box_baseline"] = sandbox.snapshot()
            self.store.save_plan(run.run_id, plan)   # persist so a park/resume keeps the baseline
        broker = self._broker(run)
        grants = self._grants.get(run.run_id, set())
        feedback = step.params.get("_feedback", "")
        # Fold in any owner messages that arrived while the run was mid-drive (queued by resume()'s
        # RUNNING branch). Prompt CONTEXT only: a note can steer the work, but it can never mint a
        # grant or bypass an approval — authorization still flows only through the parked-approval path.
        notes = self.store.take_notes(run.run_id)
        if notes:
            feedback = (feedback + "\n\nWHILE YOU WERE WORKING, the owner said (fold this in): "
                        + "; ".join(notes)).strip()
        # Model escalation (lever 1): on hard ladder rungs use the STRONGER executor model — more horsepower
        # exactly when the cheap approach has already failed. Derived from the PERSISTED rung, so it survives
        # a restart for free (no new field). Verification is untouched: the stronger model still routes every
        # action through the same broker + read-back gate.
        rung = int(step.params.get("_ladder", 0))
        role = "executor_hard" if rung >= _HARD_RUNG else "executor"
        prior = [(s.text, s.result.text) for s in plan.steps
                 if s.status is StepStatus.DONE and s.result is not None]
        result = self.executor.run(step, plan=plan, goal=goal, broker=broker, sandbox=sandbox,
                                   grants=grants, user_id=runtime.local_user(), feedback=feedback, prior=prior,
                                   available_tools=self._available_tools(),
                                   memory=self.store.get_memory(), role=role)
        self._persist_mandate_counts(run, broker)
        return self._handle_step_result(run, goal, plan, step, result, allow_redirect=True)

    def _resume_step(self, run, goal, resume_state) -> RunState:
        """Owner approved: execute the EXACT parked action (deterministic) and continue its loop."""
        plan = self.store.get_plan(run.run_id)
        running = [s for s in plan.steps if s.status is StepStatus.RUNNING]
        step = running[0] if running else None
        if step is None:
            run.status = RunStatus.RUNNING
            return self._drive(run, goal)
        broker = self._broker(run)
        sandbox = self._sandbox(run.run_id)
        grants = self._grants.get(run.run_id, set())
        result = self.executor.resume(resume_state, broker=broker, sandbox=sandbox,
                                      grants=grants, user_id=runtime.local_user())
        self._persist_mandate_counts(run, broker)
        # An approved-then-performed action that fails verification is surfaced honestly (no re-run/divergence).
        outcome = self._handle_step_result(run, goal, plan, step, result, allow_redirect=False)
        if outcome in ("parked", "escalated"):
            return run
        return self._drive(run, goal)

    # ---- draft-preview resume (P0.3b) ----------------------------------------------------------------
    def _stop_preview(self, run) -> RunState:
        """Owner declined the draft preview (a bare 'no') -> STOP the run cleanly. The send was parked
        BEFORE the forward, so nothing went out and there is nothing to correct — a clean stop, never an
        escalation/owner-declined dead end."""
        self._pending_grant.pop(run.run_id, None)
        self._resume_state.pop(run.run_id, None)
        self._persist_continuation(run.run_id)
        run.pending_approval = None
        run.status = RunStatus.STOPPED
        run.updated_at = now_ts()
        self._release_run_resources(run)
        self.store.save_run(run)
        self.timers.cancel_for_run(run.run_id)
        self._emit(run, "notify", "okay — I won't send it.")
        return run

    def _revise_preview(self, run, goal, edit: str) -> RunState:
        """Owner replied to the draft preview with EDITS (not yes/no) -> revise + re-preview. Drop the
        pending grant/resume for the OLD draft (never authorize it), feed the owner's changes back into the
        RUNNING send step as feedback, and re-drive: the executor regenerates the REVISED send, which parks
        a FRESH preview of the new draft. The revised send still flows through the broker + read-back gate,
        and the recipient allow-list is untouched — an edit revises CONTENT, never widens scope."""
        prior = (self._resume_state.get(run.run_id) or {}).get("pending", {}).get("params", {}) or {}
        self._pending_grant.pop(run.run_id, None)
        self._resume_state.pop(run.run_id, None)
        self._persist_continuation(run.run_id)
        plan = self.store.get_plan(run.run_id)
        running = [s for s in plan.steps if s.status is StepStatus.RUNNING] if plan else []
        run.pending_approval = None
        run.status = RunStatus.RUNNING
        run.updated_at = now_ts()
        if not running:
            self.store.save_run(run)
            return self._drive(run, goal)             # lost the step -> just carry on driving
        step = running[0]
        prior_subject = str(prior.get("subject") or "").strip()
        prior_body = str(prior.get("body") or prior.get("text") or prior.get("message") or "").strip()
        draft = (f"Subject: {prior_subject}\n{prior_body}" if prior_subject else prior_body) \
            or "(the draft you prepared)"
        step.params["_feedback"] = (
            "The owner reviewed the draft you were about to send and asked for CHANGES before it goes out — "
            "do NOT send the old version.\n"
            f"YOUR PREVIOUS DRAFT:\n{draft}\n\n"
            f"THE OWNER'S REQUESTED CHANGES: {edit}\n"
            "Revise the message accordingly and send the REVISED version to the same recipient.")
        step.params.pop("_box_baseline", None)         # a fresh box baseline for the re-run
        step.status = StepStatus.PENDING
        self.store.save_plan(run.run_id, plan)
        self.store.save_run(run)   # persist RUNNING + cleared approval before re-driving (crash-safe)
        self._emit(run, "progress", "revising the draft with your changes")
        return self._drive(run, goal)

    def _resume_escalated(self, run, goal, answer: str | None) -> RunState:
        """An escalation is a PARKED conversation, not a dead end: the owner's reply is classified into
        an INTENT (P0.2) — confirmed / denied / retry / stop / guidance — and handled accordingly. The
        pending 'review' approval is the anchor the answer resolves, and it carries the ``reason_code``
        that says WHY the run escalated. Continuing never widens authorization — every new action still
        flows through the broker + read-back gate, and the budget/deadline terminators still hold."""
        ans = answer if answer is not None else self._answer_for(run)
        if ans is None or not ans.strip():
            return run                                   # still waiting on the owner
        reason_code = getattr(run.pending_approval, "reason_code", "") or ""
        subject_keys = list(getattr(run.pending_approval, "subject_keys", ()) or ())
        intent = self._classify_escalation_reply(ans, reason_code,
                                                 getattr(run.pending_approval, "prompt", "") or "")
        # confirmed/denied/retry only carry send meaning on a send-confirmation escalation (an unverifiable
        # send, or a reverify-proven-missing correction). On any OTHER escalation type (owner_declined /
        # connect / budget / generic step failure) they are meaningless, so they fall through to guidance —
        # the classic replan — exactly as before P0.2. "retry" is gated the same as "denied": on a non-send
        # escalation the replan naturally reads it as "try the failed step again", NOT as a send re-issue.
        send_confirm = reason_code in ("needs_owner_confirm", "reverify_proven_missing")
        if intent == "stop":
            return self._stop_escalated(run)
        if intent == "confirmed" and send_confirm:
            return self._confirm_escalated(run, goal, subject_keys)
        if send_confirm and intent in ("retry", "denied"):
            return self._resend_escalated(run, goal, ans, subject_keys, denied=(intent == "denied"))
        return self._guidance_escalated(run, goal, ans)

    def _classify_escalation_reply(self, ans: str, reason_code: str, question: str) -> str:
        """Map the owner's escalation reply to an intent. Deterministic shortcuts first (model-free);
        otherwise ONE cheap model call (the ControlPlane.classify pattern — enum JSON schema, executor
        role) that HARD-DEFAULTS to "guidance" on any error or non-conforming output. With Fake models the
        call yields no valid JSON, so tests exercise the shortcuts and this default without a live model."""
        intent = _deterministic_intent(ans, reason_code)
        if intent is not None:
            return intent
        try:
            resp = self.model.complete(
                [{"role": "system", "content": _INTENT_SYSTEM},
                 {"role": "user", "content": f"THE ASSISTANT ASKED:\n{question}\n\n"
                                             f"THE OWNER REPLIED:\n{ans}"}],
                role="executor",
                response_format={"type": "json_schema",
                                 "json_schema": {"name": "intent", "schema": _INTENT_SCHEMA}})
            data = json.loads(resp.content)
            if data.get("intent") in ("confirmed", "denied", "retry", "stop", "guidance"):
                return data["intent"]
        except Exception:
            _log.exception("escalation intent classify failed; defaulting to guidance")
        return "guidance"

    def _pending_send_effects(self, run, subject_keys) -> list:
        """The EXACT forwarded, not-yet-confirmed (``expected_present is None``) send effects an owner
        attestation should correct — scoped to what THIS escalation was about, never a sibling effect.

        When ``subject_keys`` is non-empty (the escalation stamped the ``action_id``s it asked about),
        select precisely those records — so an owner "nothing arrived" on one send's escalation can never
        flip a verification_broken send that is on its own +60s reverify track. When it is empty (a run
        persisted before ``subject_keys`` existed), fall back to the pre-P0.2 predicate MINUS records with
        ``detail['readback_error']``: a plain-unverifiable send is fair game, but a verification_broken one
        (reported separately, re-checked on a timer) must never be swept in by the fallback."""
        keys = set(subject_keys or ())
        if keys:
            return [e for e in self.store.get_effects(run.run_id) if e.action_id in keys]
        return [e for e in self.store.get_effects(run.run_id)
                if e.side_effecting and e.phase == "forwarded" and e.expected_present is None
                and e.detail.get("grant_key") and not e.detail.get("readback_error")]

    def _stop_escalated(self, run) -> RunState:
        """Owner closed the escalation — STOPPED (the pre-P0.2 bare-'no' path, unchanged)."""
        run.status = RunStatus.STOPPED
        run.pending_approval = None
        run.updated_at = now_ts()
        self.store.save_run(run)
        self.timers.cancel_for_run(run.run_id)
        self._emit(run, "notify", "okay — leaving it here.")
        return run

    def _confirm_escalated(self, run, goal, subject_keys) -> RunState:
        """The owner ATTESTED the escalated send went through — the strongest evidence we have when our own
        read-back couldn't confirm it. Record that attestation on each subject send effect as a DISTINCT,
        explicitly-owner-sourced ledger evidence class (``detail['verification'] = 'owner-confirmed'``,
        set ONLY here, from a real owner reply) — NOT the strict top-level ``verification`` guard field and
        NOT ``expected_present``, so ``verified_effects`` stays strict and the final report never claims an
        independent read-back it doesn't have. The trust gate is untouched (``_gate_step`` treats an
        owner-confirmed send as landed for step-completion, exactly like verification_broken).

        The escalated send's step FAILED the gate; the attestation resolves it, so mark that step DONE.
        If un-done plan steps REMAIN, don't hard-close (that would skip the rest of the plan and its
        objective checks) — ack and RESUME the normal drive loop, letting step 2..N run and the run reach
        DONE via ``_finalize`` like any other resume. Only when nothing remains do we close DONE here with
        the friendly one-liner."""
        for eff in self._pending_send_effects(run, subject_keys):
            eff.detail["verification"] = "owner-confirmed"
            self.store.update_effect(run.run_id, eff)
        plan = self.store.get_plan(run.run_id)
        if plan:
            for s in plan.steps:                          # the attested send's step succeeded after all
                if s.status is StepStatus.FAILED:
                    s.status = StepStatus.DONE
            self.store.save_plan(run.run_id, plan)
        remaining = bool(plan) and any(
            s.status not in (StepStatus.DONE, StepStatus.SKIPPED) for s in plan.steps)
        run.pending_approval = None
        run.updated_at = now_ts()
        if remaining:
            run.status = RunStatus.RUNNING
            self.store.save_run(run)
            self._emit(run, "notify", "good to know — that went through. carrying on.")
            return self._drive(run, goal)
        run.status = RunStatus.DONE
        self._release_run_resources(run)
        self.store.save_run(run)
        self.timers.cancel_for_run(run.run_id)   # drop any armed reverify — the owner has settled it
        self._emit(run, "done", "great — all set then.")
        return run

    def _resend_escalated(self, run, goal, ans: str, subject_keys, *, denied: bool) -> RunState:
        """The owner reports the escalated send never arrived (``denied``) or explicitly asked to try
        again (``retry``). Record the honest correction on each subject send effect — its net outcome is
        FAILURE: the API forwarded it, but the owner (the strongest available evidence) attests it never
        landed. We flip BOTH ``expected_present=False`` and ``phase='failed'``, and it is the PHASE flip
        that does the load-bearing work:
          * ``expected_present=False`` alone already releases the grant-key from the operator's idempotency
            re-seed — that re-seed keeps ``forwarded`` records whose ``expected_present is not False``
            (:meth:`_broker`), so proving the effect missing drops it whether or not the phase changes.
          * But a record left ``phase='forwarded'`` with ``expected_present=False`` is a PERMANENT hard
            refuse in ``trustgate.classify_effects`` (a forwarded-but-proven-absent send) with NO
            supersession path: ``landed_gks`` is seeded ONLY from ``forwarded`` + ``expected True`` records
            and ``retryable_attempts`` ONLY from ``failed``/``attempted`` ones — so a forwarded+False
            record can never be forgiven by a later verified resend, and the run could never clear the gate.
            Flipping the phase to ``failed`` routes the record into ``retryable_attempts`` instead, where
            the verified resend of the same identity (grant_key) supersedes it and the run can reach DONE.
        (This is the reviewed correction to the P0.1 finding: the phase flip is about the gate's
        supersession rule, not about the idempotency re-seed.) Then replan a retry path and drive it."""
        for eff in self._pending_send_effects(run, subject_keys):
            eff.phase = "failed"           # honest: forwarded, but owner-attested never-landed -> failed
            eff.expected_present = False   # the strongest evidence (owner attestation) proves it absent
            eff.detail["correction"] = "owner-reported-missing" if denied else "owner-requested-retry"
            self.store.update_effect(run.run_id, eff)
        reason = ("the owner reports the message never arrived" if denied
                  else "the owner asked to try the send again")
        return self._replan_from_escalation(
            run, goal, reason=reason,
            new_info=f"OWNER GUIDANCE: {ans}\nThe earlier send did NOT arrive — RE-SEND it (a genuine, "
                     "fresh send of the same message).")

    def _guidance_escalated(self, run, goal, ans: str) -> RunState:
        """The classic path: fold the owner's guidance into a replan of the remaining work (it can never
        authorize anything). A no-new-steps replan is handled by :meth:`_replan_from_escalation`."""
        return self._replan_from_escalation(run, goal, reason="the owner replied to the escalation",
                                            new_info=f"OWNER GUIDANCE: {ans}")

    def _replan_from_escalation(self, run, goal, *, reason: str, new_info: str) -> RunState:
        """Shared resume: refresh spend, enforce the budget/deadline terminator, then replan the remaining
        work and drive it. A replan that yields NO new steps is not a dead end (P0.2) — see
        :meth:`_close_or_plea`."""
        run.spent_usd = self.store.run_spend(run.run_id)   # refresh: spend may postdate the escalation
        if not self._has_headroom(run):
            self._emit(run, "notify",
                       f"I can't continue — the budget (${run.budget_usd:.2f}) or time limit is spent. "
                       "Start a new request and I'll pick up from what's done.")
            return run
        run.pending_approval = None
        run.status = RunStatus.RUNNING
        run.updated_at = now_ts()
        self.store.save_run(run)
        plan = self.store.get_plan(run.run_id)
        done_steps = [s for s in plan.steps if s.status is StepStatus.DONE] if plan else []
        newplan = self.planner.replan(
            goal, done_steps, reason=reason, new_info=new_info,
            broker=self._broker(run), catalog=CAPABILITY_CATALOG,
            memory=self.store.get_memory())
        if len(newplan.steps) <= len(done_steps):
            return self._close_or_plea(run, plan)
        self.store.save_plan(run.run_id, newplan)
        self._emit(run, "progress", "picking the run back up with your guidance")
        return self._drive(run, goal)

    def _close_or_plea(self, run, plan) -> RunState:
        """A replan produced no new work. DEFAULT (P0.2): close the run DONE with a friendly "nothing more
        needed" — a no-op replan is the CORRECT outcome for a reply that needed no further action (the
        incident dead-end treated this as failure). The rephrase plea survives ONLY as the exception: a
        FIRST unparseable guidance on a run that GENUINELY still has pending (not-done) work, and never
        twice in a row (tracked per-run in ``_guidance_pleaded``)."""
        has_pending = bool(plan) and any(s.status is not StepStatus.DONE for s in plan.steps)
        if has_pending and run.run_id not in self._guidance_pleaded:
            self._escalate(run, "I couldn't turn that into a next step — can you rephrase what "
                                "you'd like me to do?", reason_code="needs_rephrase")
            # Set the flag AFTER _escalate: _escalate parks the run and clears its per-run caches
            # (_release_run_resources), so setting it earlier would be wiped. Now it survives to the next
            # reply — a second unparseable guidance closes DONE instead of pleading again.
            self._guidance_pleaded.add(run.run_id)
            return run
        run.pending_approval = None
        run.status = RunStatus.DONE
        run.updated_at = now_ts()
        self._release_run_resources(run)
        self.store.save_run(run)
        self.timers.cancel_for_run(run.run_id)
        self._emit(run, "done", "nothing more needed — all set.")
        return run

    def _park_connect(self, run, na) -> str:
        """Park a run that needs the user to CONNECT an account: AWAITING_CONNECT, emit a tappable connect
        link (the consent URL), and schedule the auth-completion poll timer (driven by the durable tick)."""
        run.pending_approval = None
        run.status = RunStatus.AWAITING_CONNECT
        run.updated_at = now_ts()
        self.store.save_run(run)
        provider = _provider_label(na.get("toolkit", ""))
        self._emit(run, "connect", f"connect your {provider}", url=na.get("url", ""), provider=provider)
        self.timers.schedule(run_id=run.run_id, wake_at=self.timers.now() + _CONNECT_POLL_S,
                             kind="connect", payload={})
        return "parked"

    def _resume_connect(self, run, goal, *, event: str | None = None) -> RunState:
        """A parked-on-connect run woke (the poll timer fired): check whether the OAuth grant has landed. If
        CONNECTED -> resume-at-action EXACTLY (deterministic, once). If not yet -> re-arm the poll, bounded by
        a hard backstop AND the run's deadline, escalating honestly if it never connects (never a silent quit,
        never a silent authorize). The completion check is an INDEPENDENT authorize() probe — no LLM."""
        meta = self._connect.get(run.run_id) or {}
        toolkit = meta.get("toolkit", "")
        authorize = getattr(self.integrations, "authorize", None)
        _res = authorize(toolkit, runtime.local_user()) if callable(authorize) else ("error", "", "")
        status = _res[0]
        if status == "completed":
            self.timers.cancel_for_run(run.run_id)
            self._connect.pop(run.run_id, None)
            rs = self._resume_state.pop(run.run_id, None)
            self._persist_continuation(run.run_id)
            run.pending_approval = None
            if rs is not None:
                return self._resume_step(run, goal, rs)   # run the EXACT parked action, now that it's connected
            run.status = RunStatus.RUNNING
            self.store.save_run(run)
            return self._drive(run, goal)
        polls = _int(meta.get("polls"), 0) + 1
        if polls >= _MAX_CONNECT_POLLS or not self._has_headroom(run):
            self.timers.cancel_for_run(run.run_id)
            self._connect.pop(run.run_id, None)
            self._resume_state.pop(run.run_id, None)
            self._persist_continuation(run.run_id)
            self._escalate(run, f"couldn't connect your {_provider_label(toolkit)} in time — tap the link "
                                "I sent and I'll pick right back up")
            return run
        # Providers EXPIRE pending consent flows and mint fresh ones (found live: a stale connect
        # link dead-ends at the provider with no grant and a confusing error). Each poll returns the
        # CURRENT consent URL — when it changes, re-emit the connect card so the owner's tappable
        # link is always the live flow, not a dead replay.
        fresh_url = str(_res[1]) if len(_res) > 1 and _res[1] else ""
        if fresh_url and fresh_url != meta.get("url"):
            meta["url"] = fresh_url
            provider = _provider_label(toolkit)
            self._emit(run, "connect", f"connect your {provider} (fresh link — the old one expired)",
                       url=fresh_url, provider=provider)
        meta["polls"] = polls
        self._connect[run.run_id] = meta
        self._persist_continuation(run.run_id)
        self.timers.schedule(run_id=run.run_id, wake_at=self.timers.now() + _CONNECT_POLL_S,
                             kind="connect", payload={})
        self.store.save_run(run)
        return run

    def _has_headroom(self, run) -> bool:
        """True iff the run may keep trying — both the dollar budget AND the wall-clock deadline (if any)
        have headroom. This is the relentless terminator: a give-up site climbs the ladder only while this
        holds, then escalates honestly. (Budget alone is insufficient — integration/browser loops cost ~$0
        — so deadline_ts is load-bearing, not orthogonal.)"""
        if run.spent_usd > run.budget_usd:
            return False
        return not (run.deadline_ts and self.timers.now() >= run.deadline_ts)

    def _handle_step_result(self, run, goal, plan, step, result, *, allow_redirect: bool) -> str:
        run.spent_usd = self.store.run_spend(run.run_id)
        if run.spent_usd > run.budget_usd:
            self._escalate(run, f"budget of ${run.budget_usd:.2f} reached (spent ${run.spent_usd:.2f})",
                           reason_code="budget_exhausted")
            return "escalated"
        if run.deadline_ts and self.timers.now() >= run.deadline_ts:
            self._escalate(run, "time budget reached — surfacing where I got to",
                           reason_code="deadline_exhausted")
            return "escalated"

        # Persist the REAL effects (forwarded/failed/refused) FIRST — BEFORE any needs_approval park — so a
        # mandate-/grant-authorized send that already forwarded EARLIER in this same executor loop is
        # recorded for the gate even when a LATER call in the loop parks for approval. Otherwise the
        # forwarded effect would be dropped on the early return and escape the read-back gate (a mandate-
        # authorized non-landing send could then slip through). A deferred parking effect is excluded by
        # the phase filter; a refused (money) effect IS recorded so the gate/audit sees the non-completion.
        for eff in result.effects:
            eff.detail["step_index"] = step.index   # bind each effect to its PRODUCING step (effect_landed)
            if eff.phase in ("forwarded", "failed", "refused"):
                self.store.append_effect(run.run_id, eff)
            if eff.phase == "refused":
                self._money_tripwire(run, eff)

        # Accumulate the URLs this step actually FETCHED through the proxy (across the whole run), so the
        # gate's source_membership check can refuse a deliverable that cites a source the run never read.
        fetched = {e.get("url") for e in (result.events or [])
                   if e.get("kind") == "fetch" and e.get("ok") and e.get("url")}
        if fetched:
            self._fetched.setdefault(run.run_id, set()).update(fetched)

        # Provenance-discovered recipients: admit an email FOUND on a fetched page whose host its own domain
        # matches (e.g. chef@bistro.com on bistro.com) to the mandate's effective scope — never a recipient
        # that appears only in free model text, never one injected onto an unrelated page. Only when a
        # mandate is active (nothing to widen otherwise); persisted in the continuation, dropped at run end.
        if run.mandate:
            admitted = mandate_lib.admitted_from_fetch(result.events)
            if admitted:
                self._discovered.setdefault(run.run_id, set()).update(admitted)
                self._persist_continuation(run.run_id)

        # Persist anything the agent chose to REMEMBER about this user (cross-session, self-curated).
        # Recorded regardless of whether the step's gate passes — a learning survives a failed step.
        notes = [e.get("note") for e in (result.events or [])
                 if e.get("kind") == "remember" and e.get("note")]
        if notes:
            self.store.save_memory(memory.append_notes(self.store.get_memory(), notes))

        if result.signals.get("needs_auth"):
            # The step needs the USER to connect an account (OAuth). Park on CONNECT: stash the exact parked
            # action's resume-state (so it runs deterministically once granted), text a tappable connect
            # link, and start the auth-completion poll. Resume-at-action happens in _resume_connect.
            na = result.signals["needs_auth"]
            rs = result.signals.get("resume")
            if rs is not None:
                self._resume_state[run.run_id] = rs
            self._connect[run.run_id] = {"toolkit": na.get("toolkit", ""), "url": na.get("url", ""),
                                         "polls": 0}
            self._persist_continuation(run.run_id)   # durable: keep polling + resume-at-action across a restart
            return self._park_connect(run, na)

        if result.signals.get("needs_approval"):
            apr = result.signals["needs_approval"]
            gk = result.signals.get("grant_key")
            if gk:
                self._pending_grant[run.run_id] = gk
            rs = result.signals.get("resume")
            if rs is not None:
                self._resume_state[run.run_id] = rs
            self._persist_continuation(run.run_id)   # durable: survive a restart between park and approve
            self.store.save_approval(apr)
            self._park(run, RunStatus.AWAITING_APPROVAL, apr)
            auto = int(result.signals.get("auto_release_seconds") or 0)
            if auto > 0 and apr.kind == "undo":
                # undo-window soft-confirm: schedule the auto-release timer + send a vetoable "queued"
                # notice (not a blocking approval). The owner can reply STOP within the window; otherwise
                # the timer fires -> resume(event="timer") auto-releases the EXACT parked send.
                self.timers.schedule(run_id=run.run_id, wake_at=self.timers.now() + auto,
                                     kind="undo_release", payload={})
                self._emit(run, "notify", apr.prompt)
            else:
                self._emit(run, "approval", apr.prompt, effect_label=apr.effect_label, tier=apr.tier)
            return "parked"

        if (result.signals.get("tool_failed") or result.signals.get("exhausted")
                or result.signals.get("blocked")):
            code = "tool_failed"
            if result.signals.get("tool_failed") == "model":
                # The model itself failed (transport error, unavailable adapter) — the one failure the
                # ladder can't climb past (every retry needs the model). Surface the underlying error
                # text so the owner sees WHAT broke, not just that a step "failed repeatedly".
                code, reason = "model_error", (result.text or "the model call failed")
            elif result.signals.get("tool_failed"):
                reason = f"tool '{result.signals.get('tool_failed')}' failed repeatedly"
            elif result.signals.get("blocked"):
                reason = result.text or "the step could not be completed"
            else:
                reason = "step exhausted its budget"
            step.status = StepStatus.FAILED
            step.result = result
            self.store.save_plan(run.run_id, plan)
            self._escalate(run, f"step {step.index + 1} could not complete: {reason}", reason_code=code)
            return "escalated"

        gate = self._gate_step(run, step, result, self._sandbox(run.run_id))
        # Independent constraint verification: when completing THIS step would finish the run, a skeptical
        # critic (NOT the executor) checks the deliverable actually meets the owner's hard constraints. An
        # unsatisfied verdict becomes a redirectable refusal, so relentlessness keeps searching (or escalates
        # honestly) rather than reporting an unsatisfactory answer as done. Fail-open (never blocks on error).
        # FAST-PATH runs (P1.3) skip it: the detector only ever matches a goal with NO constraints (any
        # constraint -> the full pipeline), and the send itself was just verified MECHANICALLY by the gate's
        # read-back — there is nothing fuzzy left for a model critic to judge, and the skip is what holds
        # the happy path at <=2 model calls. The deterministic gate above ran in full either way.
        if gate.accept and self._finishes_run(plan, step) and not run.fast_path:
            deliverable = self._run_deliverable(plan, step, result)   # the run's actual answer, like _finalize
            ok, why = self.verifier.verify(goal, deliverable, broker=self._broker(run))
            if not ok:
                gate = _GateOutcome(accept=False, reason=why, needs_owner=False)
        if gate.accept:
            step.status = StepStatus.DONE
            step.result = result
            step.params.pop("_feedback", None)
            step.params.pop("_ladder", None)
            step.params.pop("_box_baseline", None)
            self.store.save_plan(run.run_id, plan)
            # A verification_broken send this step just forwarded completes the step (landed), but we
            # never LEAVE it unconfirmed: arm ONE durable +60s re-check per broken send that re-runs the
            # read-back once the broken tool may have recovered (see _reverify). The soft "I'll re-check"
            # note rides on the final report (_finalize). Log ERROR so the operator sees the broken probe.
            self._arm_reverify(run, [e for e in result.effects if _is_verification_broken(e)])
            self._emit(run, "progress", f"step {step.index + 1} done: {step.text}")
            return "advanced"

        # Gate refused. Owner-confirm class (or a performed-action resume) -> escalate; else bounded redirect.
        if gate.needs_owner or not allow_redirect:
            step.status = StepStatus.FAILED
            step.result = result
            self.store.save_plan(run.run_id, plan)
            # The unverifiable case gets a friendly, slug-free owner message; other refusals keep the
            # precise "step N: <reason>" (they're redirect/diagnostic-facing, not the owner-confirm path).
            # Stamp the escalation with the action_ids of the PLAIN-unverifiable sends it is about
            # (forwarded + expected None + NO readback_error) — NOT any verification_broken send, which is
            # reported separately and re-checked on its own +60s timer — so an owner reply flips only these.
            subject = None
            if gate.owner_prompt:
                subject = tuple(e.action_id for e in self.store.get_effects(run.run_id)
                                if e.side_effecting and e.phase == "forwarded"
                                and e.expected_present is None and (e.detail or {}).get("grant_key")
                                and not (e.detail or {}).get("readback_error"))
            self._escalate(run, gate.owner_prompt or f"step {step.index + 1}: {gate.reason}",
                           reason_code="needs_owner_confirm" if gate.owner_prompt else "",
                           subject_keys=subject)
            return "escalated"
        return self._climb_ladder(run, goal, plan, step, gate.reason)

    def _climb_ladder(self, run, goal, plan, step, reason) -> str:
        """The relentless give-up: instead of quitting at a fixed attempt count, re-run the step with
        ESCALATING feedback (fix -> different approach -> alternate channel/contact -> human hand-off) while
        budget AND the wall-clock deadline have headroom, up to a hard backstop. Only at exhaustion does it
        escalate honestly. Synchronous (no async park) so the tick loop stays ordered; the model executes the
        creative strategies the hints suggest, and every send it makes still flows through the mandate gate
        + the read-back verification (relentlessness widens effort, never the trust guarantees)."""
        rung = int(step.params.get("_ladder", 0))
        if not self._has_headroom(run) or rung >= _LADDER_HARD_CAP:
            # The per-step ladder is spent. While budget+deadline still allow, RE-ARCHITECT the remaining
            # plan (lever 2) — a DIFFERENT route, completed work preserved — before giving up. Relentlessness
            # at the PLAN level, not just the step level. A pure budget/deadline exhaustion (no headroom)
            # skips straight to the honest escalate (a replan would only burn more).
            if self._has_headroom(run):
                outcome = self._replan_remaining(run, goal, plan, step, reason)
                if outcome is not None:
                    return outcome
            step.status = StepStatus.FAILED
            self.store.save_plan(run.run_id, plan)
            self._escalate(run, f"step {step.index + 1} could not be completed after {rung + 1} attempts: {reason}")
            return "escalated"
        step.params["_ladder"] = rung + 1
        step.params["_feedback"] = f"{reason}\n\n{_LADDER_HINTS[min(rung, len(_LADDER_HINTS) - 1)]}"
        step.status = StepStatus.PENDING   # re-attempt on the next loop pass (with escalated feedback)
        self.store.save_plan(run.run_id, plan)
        return self._run_generic_step(run, goal, plan, step)

    def _replan_remaining(self, run, goal, plan, step, reason) -> str | None:
        """Plan-level relentlessness (lever 2): when a step's per-step ladder is spent, RE-ARCHITECT the
        REMAINING DAG via the planner (a DIFFERENT route that does not repeat the failed step), preserving
        all COMPLETED work, then let _drive pick up the new plan. Bounded by a run-level replan cap AND
        budget/deadline (the REAL terminators). Returns "advanced" if it saved a new plan to drive, or None
        to tell the caller to escalate honestly (cap reached, no headroom, or the replan produced no new
        work). Trust-safe: replan only re-authors WHICH steps remain — every new action still flows through
        the broker + read-back gate, and COMPLETED (DONE) steps + their verified effects are never re-driven
        (the failed step is excluded from the preserved set, so it is re-architected, not skipped as done).
        Returns "advanced" (not a nested _drive) so the deep per-step ladder recursion UNWINDS first and the
        existing outer _drive loop reloads the new plan — keeping stack depth flat across many replans."""
        if not (self._has_headroom(run) and run.dag_replans < _MAX_REPLANS):
            return None
        run.dag_replans += 1
        self.store.save_run(run)   # count the attempt BEFORE replanning so a crash mid-replan can't loop
        done_steps = [s for s in plan.steps if s.status is StepStatus.DONE]   # FAILED step intentionally excluded
        new_info = ("That route is exhausted. Re-architect the REMAINING work with a DIFFERENT approach, "
                    "tool, or contact that does NOT repeat the failed step; keep all completed work.")
        notes = self.store.take_notes(run.run_id)   # mid-drive owner guidance steers the re-architecture
        if notes:
            new_info = "THE OWNER SAID (mid-run): " + "; ".join(notes) + "\n" + new_info
        newplan = self.planner.replan(
            goal, done_steps,
            reason=f'step {step.index + 1} "{step.text}" could not be completed: {reason}',
            new_info=new_info,
            broker=self._broker(run), catalog=CAPABILITY_CATALOG,
            memory=self.store.get_memory())
        if len(newplan.steps) <= len(done_steps):
            return None   # no-progress: the model added no new steps -> don't loop; escalate honestly
        self.store.save_plan(run.run_id, newplan)
        self._emit(run, "progress",
                   f"that approach stalled — re-architecting the remaining plan (replan {run.dag_replans})")
        return "advanced"   # _drive reloads the new plan and keeps driving it

    @staticmethod
    def _finishes_run(plan, step) -> bool:
        """True iff completing ``step`` would finish the plan (every OTHER step is already DONE) — the point
        at which the run's deliverable is settled, so the independent verifier runs there."""
        return all(s.status is StepStatus.DONE for s in plan.steps if s.index != step.index)

    @staticmethod
    def _run_deliverable(plan, step, result) -> str:
        """The run's owner-facing answer at finish time — the LAST non-empty step result in plan order,
        using ``step``'s fresh ``result`` (not yet persisted). Mirrors ``_finalize``'s selection, so the
        verifier judges the SAME text the owner will see (a step that finishes with an empty summary can't
        smuggle an earlier constrained answer past the check)."""
        deliverable = ""
        for s in plan.steps:
            t = ((result.text if s.index == step.index else (s.result.text if s.result else "")) or "").strip()
            if t:
                deliverable = t
        return deliverable

    # ================================================================ the gate
    def _gate_step(self, run, step, result, sandbox) -> _GateOutcome:
        effs = self.store.get_effects(run.run_id)
        effect_dicts = [e.as_gate_dict() for e in effs]
        unver, unverifiable = trustgate.classify_effects(effect_dicts, claimed_done=result.claimed_done)
        # verification_broken sends are FORWARDED-and-not-proven-missing (landed_effects semantics): the
        # send went out, only our own read-back tool is broken. They must NOT drive an owner escalation
        # (that is exactly the "couldn't confirm — can you double-check?" chore P0.1 removes); the +60s
        # timer re-checks them instead. Drop their labels from the gate's unverifiable set so gate_verdict
        # accepts the step. (verified_effects stays strict, so the final report never claims them verified;
        # a genuinely no-read-back send — no readback_error — still escalates below.)
        broken_labels = {e.label for e in effs if _is_verification_broken(e)}
        # An OWNER-CONFIRMED send (attested via a needs_owner_confirm escalation reply, P0.2) is landed for
        # step completion — the owner is the strongest evidence we have — so it must NOT re-escalate when a
        # LATER step in the same run finishes and re-gates the ledger. Drop its labels from unverifiable
        # exactly like a verification_broken send. verified_effects stays strict (owner-confirmed carries no
        # expected fingerprint), so the final report still never claims it independently verified.
        confirmed_labels = {e.label for e in effs if e.phase == "forwarded"
                            and (e.detail or {}).get("verification") == "owner-confirmed"}
        unverifiable = [lbl for lbl in unverifiable
                        if lbl not in broken_labels and lbl not in confirmed_labels]
        bundle = self._bundle(run, sandbox, step)
        obj = trustgate.evaluate_objective_checks(step.done_criteria, bundle)
        stale = self._stale_files(step, sandbox, result)
        breaking = self._gate_breaking(step, result, effect_dicts)
        accept, reason = trustgate.gate_verdict(
            claimed_done=result.claimed_done, ok=result.ok, stale_files=stale, gate_breaking=breaking,
            unverified_external=unver, unverifiable_external=unverifiable, objective_unmet=obj)
        # On an objective-check refusal, fold in the ACTIONABLE per-check detail (e.g. "fetch each of
        # these URLs before citing it, or remove it") so the redirect feedback fixes it in ONE shot,
        # instead of the model having to infer a bare criterion id.
        if not accept and obj:
            detail = trustgate.describe_objective_failures(step.done_criteria, bundle)
            if detail:
                reason = f"{reason} — {detail}"
        # A stale read / reliability flag is a REDO, not an owner-confirm — keep needs_owner False there.
        needs_owner = bool(unverifiable) and not unver and not obj and not stale and not breaking
        owner_prompt = _unverifiable_owner_msg(unverifiable) if needs_owner else ""
        return _GateOutcome(accept=accept, reason=reason, needs_owner=needs_owner, owner_prompt=owner_prompt)

    def _gate_breaking(self, step, result, effect_dicts) -> list:
        """Compute the in-flight reliability-signature census and confirm which still contradict the
        completion at finish time (``trustgate.confirm_gate_breaking``) — wiring the gate's reliability
        floor to REAL producers instead of a hardcoded empty list:

          * ``unsupported-completion`` — a claimed-done that rests on NOTHING the record can show: no
            objective criterion, no verified side-effect, no file produced, AND an empty deliverable. (A
            non-empty writeup IS its own evidence for a synthesis step; an EMPTY claim out of thin air —
            e.g. a blank model turn the executor reads as 'done' — is a fabricated completion -> refuse.)
          * ``forgot-own-edit`` — a flagged path was re-written with IDENTICAL content (an unproductive
            redo); confirmed by ``has_identical_redo`` over the executor's hashed write events.
          * ``failed-retry`` — a flagged command whose FINAL run still failed (plumbed; fires once a
            shell-run producer emits ``run`` events).
        """
        if not (result.claimed_done and result.ok):
            return []
        events = list(result.events or [])
        sigs: list[str] = []
        has_criteria = any(isinstance(c, dict) and isinstance(c.get("objective_check"), dict)
                           and c["objective_check"].get("kind") for c in (step.done_criteria or []))
        has_effects = any(e.get("side_effecting") and e.get("phase") in ("forwarded", "failed")
                          for e in effect_dicts)
        # a successful file write OR shell run is real work the record can show (not "nothing")
        did_work = any(e.get("kind") in ("write", "run") and e.get("ok") for e in events)
        if (not has_criteria and not has_effects and not did_work
                and not (result.text or "").strip()):
            sigs.append("unsupported-completion")
        write_counts: dict[str, int] = {}
        for e in events:
            if e.get("kind") == "write" and e.get("ok") and e.get("path"):
                write_counts[e["path"]] = write_counts.get(e["path"], 0) + 1
        flagged_rewrites = {p for p, n in write_counts.items() if n >= 2}
        flagged_retries = {(e.get("path") or "(run)") for e in events
                           if e.get("kind") == "run" and not e.get("ok", True)}
        if flagged_rewrites:
            sigs.append("forgot-own-edit")
        if flagged_retries:
            sigs.append("failed-retry")
        return trustgate.confirm_gate_breaking(sigs, events, flagged_rewrites, flagged_retries)

    def _stale_files(self, step, sandbox, result) -> list:
        """Box-observation staleness: baseline files whose on-disk content drifted since the step
        started, EXCLUDING the agent's own writes (those are intended edits, not external drift). Empty
        in the single-process model; catches a concurrently-mutated read-set under real isolation."""
        baseline = step.params.get("_box_baseline")
        if not baseline:
            return []
        # Canonicalize the write paths the SAME way snapshot_dir keys baseline paths (relpath ->
        # normcase), so a valid-but-non-canonical write ('./out.txt', 'a//b.txt') isn't mis-flagged.
        wrote = {os.path.normcase(os.path.normpath(e.get("path") or "")) for e in (result.events or [])
                 if e.get("kind") == "write" and e.get("path")}
        try:
            drift = trustgate.snapshot_drift(baseline, sandbox.snapshot())   # compare two snapshots
        except Exception:
            return []
        return [f for f in drift if f not in wrote and os.path.normcase(os.path.normpath(f)) not in wrote]

    def _bundle(self, run, sandbox, step=None) -> dict:
        files, texts = [], {}
        try:
            files = sandbox.list_files()
            for f in files:
                try:
                    texts[f] = sandbox.read_file(f)
                except Exception:
                    pass
        except Exception:
            pass
        # The GATE is the single source of truth for what landed — never re-derive it here.
        effs = self.store.get_effects(run.run_id)
        # effect_landed must rest on THIS step's OWN verified effect, not any run effect that merely shares
        # the label: scope the verified set to the step being gated so a replanned same-label step can't
        # false-pass on an EARLIER step's verified send. Legacy/unstamped effects (step_index None) are
        # kept for back-compat — a real run stamps every effect, so the filter is exact there.
        if step is not None:
            si = step.index
            effs = [e for e in effs
                    if e.detail.get("step_index") is None or e.detail.get("step_index") == si]
        gate_dicts = [e.as_gate_dict() for e in effs]
        # effect_landed rests on ``landed_effects`` (forwarded & not proven-missing — verified OR
        # honestly unverifiable), so a provider-accepted send whose read-back was scope-blocked does NOT
        # force a blind retry (a duplicate send). ``verified_effects`` (strict) is kept for any consumer
        # that needs independent confirmation.
        landed = trustgate.landed_effects(gate_dicts)
        verified = trustgate.verified_effects(gate_dicts)
        # The URLs this run actually fetched through the proxy — lets the gate's source_membership check
        # refuse a deliverable that cites a source the run never retrieved (anti-citation-fabrication).
        fetched = sorted(self._fetched.get(run.run_id, set()))
        return {"files": files, "texts": texts, "fetched_urls": fetched,
                "landed_effects": landed, "verified_effects": verified}

    # ================================================================ waiting / await / monitor
    def _park_wait(self, run, plan, step, *, kind: str):
        params = step.params or {}
        if kind == "await_replies":
            # Floor the window: a zero/negative model-authored window_seconds would make the deadline
            # immediately due and burn a model replan every tick until the replans cap. One minute is
            # the smallest sensible reply window.
            delay = max(_MIN_INTERVAL_S, _num(params.get("window_seconds"), 86400.0) or 86400.0)
            # Self-hosted flowers has no inbound channel to call deliver() the moment a reply lands,
            # so the wait must POLL: a chain of interim check timers (event="check" -> probe, never
            # the deadline path) rides alongside the single window-deadline timer. Without this, a
            # reply sits unseen until the deadline (found live: a reply answered in minutes was
            # invisible for the full two-hour window).
            step.params["_await_deadline_ts"] = self.timers.now() + delay
            check = max(_MIN_INTERVAL_S,
                        _num(params.get("check_seconds"), _AWAIT_CHECK_S) or _AWAIT_CHECK_S)
            if check < delay:
                self.timers.schedule(run_id=run.run_id, wake_at=self.timers.now() + check,
                                     kind="await_check", payload={"step": step.index})
        else:   # monitor / recurring: a floored interval between wakes
            delay = max(_MIN_INTERVAL_S, _num(params.get("interval_seconds"), 3600.0) or 3600.0)
        self.timers.schedule(run_id=run.run_id, wake_at=self.timers.now() + delay, kind=kind,
                             payload={"step": step.index})
        step.status = StepStatus.WAITING
        self.store.save_plan(run.run_id, plan)
        run.status = RunStatus.WAITING
        self.store.save_run(run)
        self._emit(run, "progress", f"waiting on step {step.index + 1}: {step.text}")

    def _resume_waiting(self, run, goal, *, event: str | None = None) -> RunState:
        plan = self.store.get_plan(run.run_id)
        waiting = [s for s in plan.steps if s.status is StepStatus.WAITING]
        if not waiting:
            run.status = RunStatus.RUNNING
            return self._drive(run, goal)
        step = waiting[0]
        if step.kind is StepKind.RECURRING:
            return self._tick_recurring(run, goal, plan, step)   # heartbeat: no probe to consume
        match = (step.params or {}).get("match") or {}
        kind, observed, probe_ok = self._probe(run, step)   # inbox | a URL's text | a browser read
        # A FAILED probe (down / rate-limited / login-wall / no backend) is NOT 'condition met' — never let
        # it satisfy an `absent` watch. Treat it as 'not yet' and keep waiting (bounded by the poll cap).
        matched = self._condition_met(kind, observed, match) if probe_ok else []
        deadline = (event == "timer")   # a timer firing is the window deadline; a delivered reply is a check

        if step.kind is StepKind.AWAIT_REPLIES:
            need = _int((step.params or {}).get("min_replies"), 1)
            if len(matched) >= need:
                self.timers.cancel_for_run(run.run_id)
                # READ what the replies said (deterministic, classify-only) and CARRY the gist forward so
                # downstream steps can act on it (offer/accept/reject/reschedule) — converse, not just wait.
                # The structured `reply_verdicts` ride in signals for
                # a possible future HARD branch, but what actually reaches the next step is the reply SUMMARY
                # in result.text -> `prior` (free text the executor reads). Any action it implies still flows
                # through the mandate + gate, and it NEVER widens the recipient allow-list (an injected reply
                # recipient stays out of scope). Wiring a mechanical accept/reject branch is a future option.
                items = [observed[mid] for mid in matched
                         if isinstance(observed, dict) and mid in observed]
                verdicts = [replies.extract_verdict((it or {}).get("body") or (it or {}).get("snippet") or "")
                            for it in items]
                summary = replies.summarize(items)
                step.status = StepStatus.DONE
                step.result = StepResult(
                    claimed_done=True, ok=True, signals={"reply_verdicts": verdicts},
                    text=(f"Replies received — {summary}" if summary else "Replies received."))
                self.store.save_plan(run.run_id, plan)
                self._emit(run, "notify",
                           f"got {len(matched)} reply(ies): {summary}" if summary
                           else f"got {len(matched)} verified reply(ies) for step {step.index + 1}")
                run.status = RunStatus.RUNNING
                return self._drive(run, goal)
            if deadline:
                self.timers.cancel_for_run(run.run_id)
                # Methodical, RELENTLESS batch outreach: no replies in the window -> send the NEXT batch and
                # wait again, while budget AND the wall-clock deadline have headroom (the wait between
                # batches IS the backoff), up to a hard backstop. Don't give up after a fixed 3 batches.
                done_steps = [s for s in plan.steps if s.status is StepStatus.DONE]
                if self._has_headroom(run) and run.replans < _LADDER_HARD_CAP:
                    run.replans += 1
                    self.store.save_run(run)
                    newplan = self.planner.replan(
                        goal, done_steps, reason="no verified replies arrived in the window",
                        new_info="send the NEXT batch of outreach (a different/second contact) and wait again",
                        broker=self._broker(run), catalog=CAPABILITY_CATALOG,
                        memory=self.store.get_memory())
                    # No-progress guard (as at _replan_remaining / _resume_escalated): planner.replan
                    # FAILS OPEN to a done-steps-only plan on a model/transport error. Driving that plan
                    # would mark the run DONE — a fabricated success at the exact moment the model was
                    # flaky. Escalate honestly instead of finalizing on an empty re-architecture.
                    if len(newplan.steps) > len(done_steps):
                        self.store.save_plan(run.run_id, newplan)
                        self._emit(run, "progress",
                                   f"no replies yet — sending the next batch (round {run.replans})")
                        run.status = RunStatus.RUNNING
                        return self._drive(run, goal)
                step.status = StepStatus.FAILED
                self.store.save_plan(run.run_id, plan)
                self._escalate(run, f"step {step.index + 1}: no verified replies after {run.replans} round(s)")
                return run
            # A non-matching/early check -> keep waiting (never complete on it), and RE-ARM the next
            # interim check while the window has room (the deadline timer itself stays pending).
            deadline_ts = _num((step.params or {}).get("_await_deadline_ts"), 0.0) or 0.0
            check = max(_MIN_INTERVAL_S,
                        _num((step.params or {}).get("check_seconds"), _AWAIT_CHECK_S) or _AWAIT_CHECK_S)
            now = self.timers.now()
            if event == "check" and deadline_ts and now + check < deadline_ts:
                self.timers.schedule(run_id=run.run_id, wake_at=now + check,
                                     kind="await_check", payload={"step": step.index})
            self.store.save_run(run)
            return run

        if step.kind is StepKind.MONITOR:
            # `changed` watch (stateful): fire when the page text DIFFERS from the prior poll's digest —
            # "tell me when this page changes." The first poll only establishes the baseline (never fires).
            if probe_ok and kind != "inbox" and (match or {}).get("changed"):
                # AND `changed` with any other (contains/count/number_near) conditions: those were already
                # evaluated into `matched` by _condition_met. If `changed` is the ONLY condition (matched is
                # empty because _text_condition fails-closed on an unrecognized key), the base is vacuously
                # true. So a contains+changed watch fires only on a relevant change, not any change.
                other_keys = any(k in (match or {}) for k in ("contains", "absent", "count", "number_near"))
                base = bool(matched) if other_keys else True
                digest = trustgate.content_hash(str(observed or ""))
                prior = (step.params or {}).get("_last_digest")
                step.params["_last_digest"] = digest
                matched = ["match"] if (base and prior is not None and prior != digest) else []
            # Debounce: require `confirm_polls` CONSECUTIVE matching reads before firing, so a single
            # transient (an ad/CDN flicker, a one-off fetch glitch) can't end the watch on a false signal.
            # Default 1 for an inbox watch (a verified reply is reliable — fire instantly) but 2 for a
            # page/url watch; a sniping watch sets confirm_polls=1 to catch a fleeting opening instantly.
            confirm_polls = min(max(1, _int((step.params or {}).get("confirm_polls"),
                                            1 if kind == "inbox" else 2)), 10)
            interval = max(_MIN_INTERVAL_S, _num((step.params or {}).get("interval_seconds"), 3600.0) or 3600.0)
            if matched:
                streak = _int((step.params or {}).get("_match_streak"), 0) + 1
                if streak >= confirm_polls:
                    self.timers.cancel_for_run(run.run_id)
                    step.status = StepStatus.DONE
                    self.store.save_plan(run.run_id, plan)
                    note = (step.params or {}).get("notify") or "the watched message arrived"
                    self._emit(run, "notify", f"{note} ({len(matched)} match)")
                    run.status = RunStatus.RUNNING
                    return self._drive(run, goal)
                step.params["_match_streak"] = streak   # matched but not yet CONFIRMED -> watch once more
                self.store.save_plan(run.run_id, plan)
                self.timers.schedule(run_id=run.run_id, wake_at=self.timers.now() + interval,
                                     kind="monitor", payload={"step": step.index})
                self.store.save_run(run)
                return run
            step.params["_match_streak"] = 0   # a non-match BREAKS the confirmation streak
            if deadline:
                polls = _int((step.params or {}).get("_polls"), 0) + 1
                # RELENTLESS watch: poll until the match, a plan-set max_polls, the run's deadline, or the
                # hard backstop — NOT a low default cap. The deadline_ts is the owner's real time bound.
                max_polls = min(_int((step.params or {}).get("max_polls"), _MAX_MONITOR_POLLS_HARD),
                                _MAX_MONITOR_POLLS_HARD)
                if polls >= max_polls or not self._has_headroom(run):
                    self.timers.cancel_for_run(run.run_id)
                    step.status = StepStatus.FAILED
                    self.store.save_plan(run.run_id, plan)
                    self._escalate(run, f"step {step.index + 1}: watched signal did not arrive in time")
                    return run
                step.params["_polls"] = polls
                self.store.save_plan(run.run_id, plan)
                self.timers.schedule(run_id=run.run_id, wake_at=self.timers.now() + interval,
                                     kind="monitor", payload={"step": step.index})
            self.store.save_run(run)
            return run
        return run

    def _tick_recurring(self, run, goal, plan, step) -> RunState:
        """A cron-ish heartbeat: on each timer fire, run ONE occurrence (a notify with the step's task),
        then RE-ARM the durable one-shot timer for the next interval — bounded by max_occurrences, until_ts,
        the run's budget/deadline, AND a HARD occurrence backstop (a notify is free, so neither budget nor an
        absent deadline alone would stop it — the hard cap guarantees termination, R2). Reuses the durable
        timer primitive verbatim. (Each occurrence emits a notify;
        running a full gated sub-action per occurrence is a documented future enhancement.) Model-authored
        bounds are coerced fail-closed (junk -> unbounded-but-hard-capped), never raised out of the tick."""
        params = step.params or {}
        occ = _int(params.get("_occurrences"), 0)
        max_occ = _int(params.get("max_occurrences"), None) if params.get("max_occurrences") is not None else None
        until_ts = _num(params.get("until_ts"), None) if params.get("until_ts") is not None else None
        if (not self._has_headroom(run)
                or occ >= _MAX_RECURRING_OCCURRENCES_HARD
                or (max_occ is not None and occ >= max_occ)
                or (until_ts is not None and self.timers.now() >= until_ts)):
            self.timers.cancel_for_run(run.run_id)
            step.status = StepStatus.DONE
            self.store.save_plan(run.run_id, plan)
            self._emit(run, "progress", f"recurring step {step.index + 1} finished after {occ} run(s)")
            run.status = RunStatus.RUNNING
            return self._drive(run, goal)
        step.params["_occurrences"] = occ + 1
        self.store.save_plan(run.run_id, plan)
        self._emit(run, "notify", str(params.get("notify") or step.text))   # this occurrence's heartbeat
        interval = max(_MIN_INTERVAL_S, _num(params.get("interval_seconds"), 3600.0) or 3600.0)
        self.timers.schedule(run_id=run.run_id, wake_at=self.timers.now() + interval,
                             kind="recurring", payload={"step": step.index})
        self.store.save_run(run)
        return run

    @staticmethod
    def _matching_inbound(inbox: dict, match: dict) -> list:
        expected = [str(v).strip() for v in (match or {}).values() if str(v).strip()]
        if not expected:
            return []   # fail CLOSED: a reply can only be verified against a concrete signature
        return [k for k, item in inbox.items() if effects._item_matches(item or {}, expected)]

    def _probe(self, run, step) -> tuple[str, object, bool]:
        """Run this WAITING step's PROBE and return ``(kind, observed, ok)`` — WHAT is watched over time:
          * ``'inbox'``   (default) -> the inbox items (the await-reply case);
          * ``'url'``     -> the text of a URL fetched out-of-band (watch a public page over days);
          * ``'browser'`` -> the text of a URL navigated+extracted via the browser (login-walled / JS pages).
        ``ok`` is False on a FAILED or EMPTY fetch, or an explicitly-requested-but-unavailable browser probe.
        The caller must NOT evaluate the condition when ``ok`` is False — a failed read must never satisfy an
        ``absent`` condition (which would fire a false 'back in stock'). An explicit url/browser probe NEVER
        falls through to the inbox (that would invert the surface + the absent semantics). The probe SPEC
        lives in ``step.params``, so it is durable and re-runs after a process restart."""
        params = step.params or {}
        probe = str(params.get("probe") or "inbox").lower()
        if probe in ("url", "fetch"):
            try:
                f = self._broker(run).fetch(str(params.get("url") or ""))
                text = f.text if getattr(f, "ok", False) else ""
                return "text", text, bool(getattr(f, "ok", False) and (text or "").strip())
            except Exception:
                return "text", "", False
        if probe == "browser":
            if self.browser is None:
                return "text", "", False    # a browser watch with no backend -> keep waiting, never inbox
            try:
                br = self._broker(run).call_browser(action="extract",
                                                    params={"url": params.get("url", "")},
                                                    user_id=runtime.local_user())
                text = (br.data or {}).get("text", "") if br.status == "ok" else ""
                return "text", text, bool(br.status == "ok" and (text or "").strip())
            except Exception:
                return "text", "", False
        # AWAIT replies: read the Gmail inbox snapshot (the plan's `match` still filters by sender,
        # fail-closed on an empty match). A MONITOR inbox-watch ("tell me when an email from X arrives")
        # reads the same inbox.
        inbox = self.integrations.snapshot(toolkit="gmail", action="GMAIL_FETCH_EMAILS",
                                           params={}, user_id=runtime.local_user()) or {}
        return "inbox", inbox, True

    def _condition_met(self, kind: str, observed, match: dict) -> list:
        """Evaluate the watch CONDITION against the probe result. Returns the 'matches' (inbox: the matching
        item ids; text: a single sentinel when the page condition holds). Fail CLOSED on an empty condition."""
        if kind == "inbox":
            return self._matching_inbound(observed or {}, match or {})
        return self._text_condition(str(observed or ""), match or {})

    @staticmethod
    def _number_after(text: str, anchor: str) -> float | None:
        """The first number appearing AFTER the literal ``anchor`` substring — a BOUNDED character scan
        (NOT a regex over model input, preserving the no-ReDoS invariant: linear, fixed look-ahead). Skips
        anything before the first digit, collects digits + at most one decimal point, ignores thousands
        commas, stops at the first non-number char. Returns the float, or None if no number follows."""
        if not anchor:
            return None
        i = (text or "").lower().find(anchor.lower())
        if i < 0:
            return None
        window = (text or "")[i + len(anchor): i + len(anchor) + 64]   # fixed bounded look-ahead
        num, started = "", False
        for ch in window:
            if ch.isdigit():
                num += ch
                started = True
            elif ch == "." and started and "." not in num:
                num += ch
            elif ch == "," and started:
                continue                      # thousands separator inside the number
            elif started:
                break                         # number ended
        try:
            return float(num) if started else None
        except ValueError:
            return None

    @staticmethod
    def _text_condition(text: str, match: dict) -> list:
        """A page/condition predicate over fetched text. ALL specified keys must hold (>=1 required -> else
        fail CLOSED). Linear, no model-authored regex (a catastrophic-backtracking pattern would hang the
        single-threaded tick loop — a cross-run DoS):
          * ``contains`` / ``absent``: substring(s) that must ALL be present / absent.
          * ``count``: ``{"of": <substr>, "at_least"?: int, "at_most"?: int}`` — occurrence count bounds.
          * ``number_near``: ``{"anchor": <substr>, "at_least"?|"at_most"?|"equals"?: num}`` — the first
            number AFTER the anchor, compared (e.g. ``{"anchor":"$","at_most":50}`` = 'below $50';
            ``{"anchor":"tickets:","at_least":3}``). The number scan is a bounded char walk, not a regex.
        (``changed`` is handled statefully in the monitor loop.) Returns ``['match']`` iff every specified
        condition holds. The caller never evaluates this on a FAILED/empty probe, so ``absent``/``at_most``
        can never fire on a failed read."""
        m = match or {}
        if not any(k in m for k in ("contains", "absent", "count", "number_near")):
            return []   # fail closed: an unspecified condition never fires
        low = (text or "").lower()
        contains = m.get("contains")
        if contains is not None:
            needles = [contains] if isinstance(contains, str) else list(contains)
            if not all(str(n).lower() in low for n in needles if str(n).strip()):
                return []
        absent = m.get("absent")
        if absent is not None:
            bad = [absent] if isinstance(absent, str) else list(absent)
            if any(str(b).lower() in low for b in bad if str(b).strip()):
                return []
        count = m.get("count")
        if isinstance(count, dict) and str(count.get("of") or "").strip():
            n = low.count(str(count["of"]).lower())
            # bounds are MODEL-authored -> coerce fail-closed (junk -> None -> condition not met), never raise
            lo, hi = _num(count.get("at_least"), None), _num(count.get("at_most"), None)
            if "at_least" in count and (lo is None or n < lo):
                return []
            if "at_most" in count and (hi is None or n > hi):
                return []
        nn = m.get("number_near")
        if isinstance(nn, dict) and str(nn.get("anchor") or "").strip():
            val = Operator._number_after(text or "", str(nn["anchor"]))
            if val is None:
                return []   # the anchored number wasn't found -> condition not met (fail closed)
            lo, hi = _num(nn.get("at_least"), None), _num(nn.get("at_most"), None)
            eq = _num(nn.get("equals"), None)
            if "at_least" in nn and (lo is None or val < lo):
                return []
            if "at_most" in nn and (hi is None or val > hi):
                return []
            if "equals" in nn and (eq is None or val != eq):
                return []
        return ["match"]

    # ================================================================ finalize / escalate
    def _release_run_resources(self, run) -> None:
        """Release a run's per-run resources at run END: the browser session AND the sandbox (an E2B
        microVM MUST be killed so it isn't leaked / billed until its timeout). A parked AWAITING_APPROVAL
        run is NOT terminal and keeps its resources for resume-at-action. No-op for backends without
        close() (FakeBrowser / LocalSubprocessSandbox both close cleanly)."""
        br = self.browser
        if br is not None and hasattr(br, "close"):
            try:
                br.close(user_id=runtime.local_user())
            except Exception:
                pass
        sb = self._sandboxes.pop(run.run_id, None)
        if sb is not None:
            try:
                sb.close()
            except Exception:
                pass
        self._fetched.pop(run.run_id, None)   # drop the run's fetched-URL set (no cross-run leak)
        self._discovered.pop(run.run_id, None)   # drop provenance-admitted recipients (no cross-run leak)
        self._connect.pop(run.run_id, None)   # drop any parked-on-connect state (terminal run)
        self._guidance_pleaded.discard(run.run_id)   # drop the rephrase-plea flag (terminal run)

    def _finalize(self, run, plan) -> RunState:
        run.status = RunStatus.DONE
        run.updated_at = now_ts()
        self._release_run_resources(run)
        self.store.save_run(run)
        effs = self.store.get_effects(run.run_id)
        # The gate is the single source of truth for what landed — surface ITS verified set, never a
        # re-derived phase=='forwarded' filter (which would mislabel read-only / unverifiable effects).
        verified = trustgate.verified_effects([e.as_gate_dict() for e in effs])
        # The owner-facing answer is the deliverable the steps produced — surface it, don't bury it
        # under a status line. Take the last substantive step output (the synthesis/report step).
        deliverable = ""
        for s in plan.steps:
            t = (s.result.text if s.result is not None else "") or ""
            if t.strip():
                deliverable = t.strip()
        status = (f"Completed {len(plan.steps)} step(s); verified effects: "
                  f"{', '.join(verified) if verified else '(none)'}; spent ${run.spent_usd:.2f}.")
        report = (f"Done: {run.goal_text}\n\n{deliverable}\n\n— {status}" if deliverable
                  else f"Done: {run.goal_text}\n{status}")
        # verification_broken sends completed the run (landed) but our own delivery-check tooling failed
        # on them. Note that ONCE, in plain reply-style voice — the provider ACCEPTED the send (delivery
        # is exactly what we can't confirm), the CHECK hiccuped, and we'll re-verify on a timer (only
        # pinging back if something's actually wrong). Never a chore.
        broken = sorted({e.label for e in effs if _is_verification_broken(e)})
        if broken:
            clauses = []
            for lbl in broken:
                c = _friendly_effect_clause(lbl)
                if c not in clauses:
                    clauses.append(c)
            did = (clauses[0] if len(clauses) == 1
                   else ", ".join(clauses[:-1]) + f" and {clauses[-1]}")
            report += (f"\n\n(Heads up: I {did} and it was accepted, but I couldn't confirm delivery on my "
                       f"end — I'll re-verify shortly and only flag you if something's actually off.)")
        self._emit(run, "done", report)
        return run

    def _arm_reverify(self, run, broken_effects) -> None:
        """Arm ONE durable +60s re-check per verification_broken send (P0.1c). The payload carries just
        enough to re-run the INDEPENDENT read-back later (toolkit/action/params/label/grant_key). Logged
        at ERROR so the operator sees the broken self-verification tooling; the owner is NOT bothered now
        — only if the re-check later PROVES the send missing. No re-send is ever issued from here."""
        for e in broken_effects or []:
            payload = {"toolkit": e.toolkit, "action": e.action, "label": e.label,
                       "params": (e.detail or {}).get("readback_params") or {},
                       "grant_key": (e.detail or {}).get("grant_key", "")}
            self.timers.schedule(run_id=run.run_id, wake_at=self.timers.now() + _REVERIFY_DELAY_S,
                                 kind="reverify", payload=payload)
            _log.error("verification_broken: %s forwarded on run %s but self-verify read-back failed "
                       "(%s) — armed a +%ss re-check", e.label, run.run_id,
                       (e.detail or {}).get("readback_error"), int(_REVERIFY_DELAY_S))

    def reverify(self, run_id: str, payload: dict) -> RunState | None:
        """Timer entry point (control-plane tick): re-run a verification_broken send's INDEPENDENT
        read-back now that the broken tool may have recovered. Serialized on the per-run lock like any
        other resume so it can't race a concurrent drive."""
        with self._locked_run(run_id):
            return self._reverify(run_id, payload or {})

    def _reverify(self, run_id: str, payload: dict) -> RunState | None:
        run = self.store.get_run(run_id)
        if run is None:
            return None
        toolkit = str(payload.get("toolkit") or "")
        action = str(payload.get("action") or "")
        params = payload.get("params") or {}
        label = str(payload.get("label") or f"{toolkit}:{action}")
        # Re-run the read-back ONLY (this NEVER re-sends — it just OBSERVES). Prefer snapshot_probe so we
        # can still tell a broken probe apart from a legitimately-empty surface.
        probe = getattr(self.integrations, "snapshot_probe", None)
        if callable(probe):
            try:
                after, err = probe(toolkit=toolkit, action=action, params=params,
                                   user_id=runtime.local_user())
            except Exception as exc:  # noqa: BLE001
                after, err = None, f"{type(exc).__name__}: {exc}"
        else:
            after, err = (self.integrations.snapshot(toolkit=toolkit, action=action, params=params,
                                                     user_id=runtime.local_user()), None)
        if after is None:
            # STILL broken — the read-back tool has not recovered. Just record it (log ERROR); no owner
            # message beyond the original soft note. The send already went out; we simply still can't
            # confirm it, and asking the owner to check would be the very chore P0.1 removes.
            _log.error("reverify: read-back still failing for %s on run %s (%s)", label, run_id, err)
            return run
        fp = self.integrations.fingerprint(toolkit=toolkit, action=action, params=params)
        # PRESENCE check against a fresh read-back (empty baseline -> every current item is "added"), so
        # a fingerprint match means the message really is in the Sent surface now.
        present = effects.has_expected_effect({}, after, fp)
        if present is False:
            if run.status is RunStatus.STOPPED:
                # The owner already CLOSED this run. A proven-missing re-check must NOT re-open it with a
                # fresh escalation — that would drag the owner back into a conversation they ended. Record
                # it loudly (log ERROR) and leave the run stopped. (DONE — the designed case — and an
                # already-ESCALATED run still escalate below.)
                _log.error("reverify: %s PROVEN MISSING on the +%ss re-check for run %s, but the run is "
                           "STOPPED — not re-escalating", label, int(_REVERIFY_DELAY_S), run_id)
                return run
            # PROVEN MISSING on a valid probe: the send did NOT actually land. Real failure — notify the
            # owner honestly (we'd earlier reported it delivered). This re-opens the run for their reply.
            # Stamp the escalation with THIS send's own effect record (resolved from the reverify payload's
            # grant_key) so an owner reply flips ONLY it — never a plain-unverifiable sibling on the run. If
            # resolution fails (no grant_key / no match), stamp nothing: the empty-subject fallback then
            # excludes verification_broken records, so nothing of this reverify's is flipped by mistake.
            gk = str(payload.get("grant_key") or "")
            subject = tuple(e.action_id for e in self.store.get_effects(run_id)
                            if e.side_effecting and e.phase == "forwarded" and e.expected_present is None
                            and gk and (e.detail or {}).get("grant_key") == gk) if gk else ()
            self._escalate(run, _reverify_missing_msg([label]), reason_code="reverify_proven_missing",
                           subject_keys=subject)
            return run
        # present is True (confirmed) or None (no fingerprint -> indeterminate): just record it. The send
        # is confirmed or unconfirmable and the owner already got the soft note — no further message.
        _log.info("reverify: %s re-check %s for run %s", label,
                  "verified" if present else "indeterminate", run_id)
        return run

    def _escalate(self, run, reason: str, *, reason_code: str = "", subject_keys=None):
        """Park the run on the owner as a REVIEW question — an escalation is a parked conversation the
        owner can answer to continue (see ``_resume_escalated``), never a dead end. ``reason_code`` is a
        machine-readable tag (``model_error`` / ``budget_exhausted`` / ``owner_declined`` / ... ) riding
        alongside the human text so the dashboard and tests can branch on WHY without parsing prose.
        ``subject_keys`` are the effect ``action_id``s a send-confirmation escalation is about (P0.2), so a
        confirm/deny/retry reply corrects ONLY those records."""
        apr = ApprovalRequest(run_id=run.run_id, kind="review", prompt=reason, options=[],
                              reason_code=reason_code, subject_keys=tuple(subject_keys or ()))
        run.pending_approval = apr
        run.status = RunStatus.ESCALATED
        run.updated_at = now_ts()
        self._release_run_resources(run)   # parked: free the browser session (a resume re-creates one)
        # Zombie-run reaper (P1.1): arm a durable timer keyed to THIS escalation's approval id. If the
        # owner never answers, tick() routes it to reap() after escalation_ttl_h (24h default) and closes
        # the run quietly. The apr.id identity makes a stale timer harmless: answering, finishing, stopping,
        # or RE-escalating (a fresh apr.id carrying its OWN fresh reap timer) all leave this one a no-op —
        # so re-escalating naturally re-arms and each escalation is reaped only on its own clock.
        # Ordering (I4): schedule the reaper BEFORE persisting the run/approval. A crash between the two
        # then leaves a timer whose apr.id never matches a saved escalation — a harmless no-op via the
        # identity guard in ``_reap``. The reverse order (save first) could persist an ESCALATED run with
        # NO reaper if we crashed before scheduling — a permanent zombie, the exact failure P1.1 kills.
        self.timers.schedule(run_id=run.run_id, wake_at=self.timers.now() + self.escalation_ttl_h * 3600.0,
                             kind="reap", payload={"apr_id": apr.id})
        self.store.save_approval(apr)
        self.store.save_run(run)
        extra = {"reason_code": reason_code} if reason_code else {}
        self._emit(run, "escalated", reason, **extra)

    def reap(self, run_id: str, payload: dict) -> RunState | None:
        """Timer entry point (control-plane tick): the zombie-run reaper (P1.1). An escalation the owner
        never answered is a dead conversation cluttering the dashboard; after ``escalation_ttl_h`` close it
        quietly. Serialized on the per-run lock like any other resume so it can't race a concurrent answer
        or drive."""
        with self._locked_run(run_id):
            return self._reap(run_id, payload or {})

    def _reap(self, run_id: str, payload: dict) -> RunState | None:
        run = self.store.get_run(run_id)
        if run is None:
            return None
        apr = run.pending_approval
        # Identity check: reap ONLY a run STILL parked on the EXACT escalation this timer was armed for. If
        # the owner answered / the run finished / it was stopped (status moved off ESCALATED), or it
        # RE-escalated with a different question (a new apr.id — carrying its own fresh reap timer), this
        # stale timer is a harmless no-op. The newer escalation, if any, has its own reaper.
        if run.status is not RunStatus.ESCALATED or apr is None or apr.id != payload.get("apr_id"):
            return None
        # An answer just landed but hasn't been resumed yet (the owner replied at the TTL boundary): don't
        # reap a live conversation — the pending resume will handle it.
        if self._answer_for(run) is not None:
            return None
        run.status = RunStatus.STOPPED
        run.pending_approval = None
        run.updated_at = now_ts()
        self._release_run_resources(run)
        self.store.save_run(run)
        self.timers.cancel_for_run(run.run_id)   # the run is closed — drop any timers it still holds
        self._emit(run, "notify", "closing this out — ping me if you still want it.")
        return run

    # ================================================================ helpers
    def _available_tools(self) -> list:
        return [f"{c['label']} - {c['description']}" for c in CAPABILITY_CATALOG]

    def _money_tripwire(self, run, eff) -> None:
        """A refused (money) attempt is an INTERNAL safety tripwire. Money is removed from the agent's
        surface (the planner never plans it, the affordances never offer it), so a refusal means the model
        REACHED for money anyway — a signal something is off (a prompt-injection, a planner/model misfire).
        Log it to OUR telemetry so we can monitor it; it is NEVER surfaced to the user — the agent is never
        SEEN to even consider spending money. Best-effort; never breaks the run."""
        try:
            with self.tracer.span("money_attempt_refused", run_id=run.run_id,
                                  label=getattr(eff, "label", "") or f"{eff.toolkit}:{eff.action}"):
                pass
        except Exception:
            pass

    def _broker(self, run) -> Broker:
        def on_usage(*, kind, cost_usd, detail):
            self.store.record_usage(run_id=run.run_id, kind=kind,
                                    cost_usd=cost_usd, detail=detail)
            # Emit a cost-bearing telemetry span per metered call, so a wired tracer (the optional
            # Langfuse adapter) can roll up cost per run; NoOp/Local make this ~free.
            with self.tracer.span(kind, run_id=run.run_id,
                                  **(detail or {})) as sp:
                sp.add_cost(float(cost_usd or 0.0))
        # The EFFECTIVE mandate widens the frozen recipient_scope with provenance-admitted recipients
        # (discovered on their own org's fetched page). covers() is unchanged — it just sees a larger,
        # provenance-vetted allow-list. No mandate -> nothing to widen.
        mandate = run.mandate
        disc = self._discovered.get(run.run_id)
        if mandate and disc:
            mandate = {**mandate,
                       "recipient_scope": list(mandate.get("recipient_scope") or []) + sorted(disc)}
        # Seed run-scoped IDEMPOTENCY: grant_keys of side-effects the provider ACCEPTED in PRIOR steps
        # (landed OR unverifiable), so a replanned/re-attempted step that re-issues a byte-identical
        # send is short-circuited (never a duplicate). Same condition the broker uses to ADD a gk
        # in-loop (expected_present is not False), so seed and in-loop set stay consistent across
        # steps + restarts; only a read-back that PROVED the effect missing re-opens the action.
        prior = [e for e in self.store.get_effects(run.run_id)
                 if e.side_effecting and e.phase == "forwarded" and e.detail.get("grant_key")]
        forwarded_gks = {e.detail["grant_key"] for e in prior if e.expected_present is not False}
        verified_gks = {e.detail["grant_key"] for e in prior if e.expected_present is True}
        return Broker(model=self.model, search=self.search, integrations=self.integrations,
                      browser=self.browser, overrides=self.overrides,
                      mandate=mandate, mandate_counts=run.mandate_counts,
                      mandate_auto=bool(run.mandate_auto), send_preview=self.send_preview,
                      trust=self.store.get_trust(), on_usage=on_usage,
                      # Pre-call heartbeats -> ordinary progress events: the dashboard's timeline
                      # animates DURING a long model/tool call instead of looking frozen for minutes.
                      on_activity=lambda text: self._emit(run, "progress", f"step in progress — {text}"),
                      run_id=run.run_id,
                      verify_attempts=self.verify_attempts, verify_delay=self.verify_delay,
                      forwarded_gks=forwarded_gks, verified_gks=verified_gks)

    def _record_trust(self, run, appr) -> None:
        """Count a clean owner approval of a reversible, NON-delivering action class, so learned trust can
        eventually stop asking for it (see mandate.learned_covers). NEVER/money (kind 'never', tier != ASK)
        and delivering actions (send/forward/post/browser) are never counted — learned trust only ever
        auto-covers self-resource actions (label/archive/calendar-confirm)."""
        if appr is None or appr.tier != policy.ASK or not appr.effect_label:
            return
        tk, _, act = appr.effect_label.partition(":")
        if not act or mandate_lib.is_recipient_bearing(tk, act) or policy.is_refused(tk, act):
            return
        counts = self.store.get_trust()
        label = mandate_lib.trust_label(appr.effect_label)
        counts[label] = int(counts.get(label, 0)) + 1
        self.store.save_trust(counts)

    def _persist_mandate_counts(self, run, broker) -> None:
        """Persist the broker's hot magnitude counter back to RunState after a step, so the mandate's
        caps + dedupe survive across steps and a process restart (the broker holds a per-step working
        copy). Only writes when a mandate-covered send actually happened — no extra writes otherwise."""
        counts = broker.mandate_counts
        if counts.get("sends_total"):
            run.mandate_counts = counts
            self.store.save_run(run)

    def _sandbox(self, run_id: str):
        sb = self._sandboxes.get(run_id)
        if sb is None:
            sb = self._sandbox_factory(run_id)
            self._sandboxes[run_id] = sb
        return sb

    def _park(self, run, status, apr: ApprovalRequest):
        run.pending_approval = apr
        run.status = status
        run.updated_at = now_ts()
        self.store.save_approval(apr)
        self.store.save_run(run)
        if apr.kind == "clarify":
            self._emit(run, "clarify", apr.prompt)

    def _unpark_step(self, run):
        plan = self.store.get_plan(run.run_id)
        for s in plan.steps:
            if s.status is StepStatus.RUNNING:
                s.status = StepStatus.PENDING
        self.store.save_plan(run.run_id, plan)

    def _answer_for(self, run) -> str | None:
        if run.pending_approval is None:
            return None
        return self.store.get_answer(run.pending_approval.id)

    def _goal_of(self, run) -> Goal:
        return Goal(text=run.goal_text, budget_usd=run.budget_usd,
                    run_id=run.run_id)

    def _emit(self, run, kind: str, text: str, **extra):
        if self.channel is None:
            return
        try:
            self.channel.emit({"run_id": run.run_id, "kind": kind, "text": text, **extra})
        except Exception:
            # Never let a channel failure break the run — but with a durable event log a dropped emit
            # is data loss, so it must at least leave a trace in the server log.
            _log.exception("emit failed for run %s kind %s", run.run_id, kind)

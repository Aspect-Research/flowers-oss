"""Core data contracts shared across the engine, the trust gate, and the store.

Deliberately pure stdlib (dataclasses + enums) so the trust contract has no heavy dependency. The
single most important type here is :class:`EffectRecord` — the flat, serializable record of a
world-touching action that the deterministic gate (``flowers.trustgate``) adjudicates. Integrations
*produce* EffectRecords; the gate *consumes* them; the store *persists* them. Keeping its shape
explicit (rather than an implicit dict assembled in three places) is the public trust contract.
"""

from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- enums

class RunStatus(enum.StrEnum):
    PENDING = "pending"           # created, not yet planned
    CLARIFYING = "clarifying"     # parked awaiting clarifying answers
    PLANNING = "planning"
    AWAITING_GO = "awaiting_go"   # plan announced, awaiting owner go (hard-gate mode only)
    RUNNING = "running"
    WAITING = "waiting"           # parked on a durable timer / awaited reply / monitor poll
    AWAITING_APPROVAL = "awaiting_approval"  # parked on an ask/never-tier side-effect
    AWAITING_CONNECT = "awaiting_connect"    # parked needing the user to connect an account (OAuth)
    DONE = "done"
    ESCALATED = "escalated"       # parked on the owner: a review question they can answer to continue
    STOPPED = "stopped"           # closed by the owner (e.g. declining to continue an escalated run)


class StepStatus(enum.StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING = "waiting"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class StepKind(enum.StrEnum):
    """How the scheduler treats a step. Most steps are GENERIC (the executor runs a bounded tool
    loop). The two special kinds are what give the loop its methodical, human-like pacing."""
    GENERIC = "generic"            # executor runs the tool loop to satisfy the step
    AWAIT_REPLIES = "await_replies"  # batch-with-wait: park on a durable timer until k verified replies or deadline
    MONITOR = "monitor"            # heartbeat: recurring durable poll until a watched signal arrives, then notify
    RECURRING = "recurring"        # cron-ish: re-arm on each interval, run a bounded action, until occurrences/until


def new_id(prefix: str) -> str:
    """A short, sortable-enough unique id. (uuid4 hex; prefix names the kind.)"""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now_ts() -> float:
    return time.time()


# --------------------------------------------------------------------------- effects

@dataclass
class EffectRecord:
    """The flat record of a world-touching action — the trust contract the gate adjudicates.

    The gate reads these fields (see ``flowers.trustgate.classify_effects``):

      * ``toolkit`` / ``action`` — what was attempted (e.g. ``gmail`` / ``GMAIL_SEND_EMAIL``).
      * ``side_effecting`` — does it mutate the world? Read-only actions carry no effect to verify.
      * ``phase`` — ``"forwarded"`` (executed) vs ``attempted``/``deferred``/``denied``/``failed``.
      * ``drift_present`` — did *anything* change in the read-back surface? (True/False/None).
      * ``expected_present`` — did *this action's own* expected effect land (precise fingerprint)?
      * ``effect_kind`` — ``composio``/``comms``/``cua``/``filesystem``/... (drives provenance rules).
      * ``verification`` — set to ``"self_report"``/``"screenshot"`` for evidence that can NEVER verify.
        No in-tree producer sets it today; it is deliberate contract surface for hand-built or external
        records, and the gate's self-report guard is pinned by tests through this field.
      * ``observer`` / ``actor`` — identities; an observer equal to the actor is self-report.

    A record is built by the broker/integration layer from an INDEPENDENT read-back, never from the
    executor's self-report.
    """
    toolkit: str
    action: str
    side_effecting: bool | None = None
    phase: str = "attempted"
    drift_present: bool | None = None
    expected_present: bool | None = None
    effect_kind: str = "composio"
    verification: str | None = None
    observer: str | None = None
    actor: str | None = None
    action_id: str = field(default_factory=lambda: new_id("eff"))
    label: str = ""
    detail: dict = field(default_factory=dict)
    ts: float = field(default_factory=now_ts)

    def as_gate_dict(self) -> dict:
        """The plain dict the (pure, dict-based) gate consumes. Only the trust-relevant keys."""
        d = {
            "action_id": self.action_id,
            "toolkit": self.toolkit,
            "action": self.action,
            "side_effecting": self.side_effecting,
            "phase": self.phase,
            "drift_present": self.drift_present,
            "expected_present": self.expected_present,
            "effect_kind": self.effect_kind,
        }
        # The grant_key (params-bound action identity) lets the pure gate tell a retry of the SAME
        # action from a different action that merely shares a toolkit:action label — so a verified
        # retry can supersede its own failed attempt without a verified send to one target masking a
        # failed send to another. Absent on legacy records (the gate falls back to action_id).
        gk = (self.detail or {}).get("grant_key")
        if gk:
            d["grant_key"] = gk
        # Only include the optional self-report-guard fields when set, so legacy/auto records flow
        # through the gate byte-identically (the gate's guards are strict no-ops on missing fields).
        if self.verification is not None:
            d["verification"] = self.verification
        if self.observer is not None:
            d["observer"] = self.observer
        if self.actor is not None:
            d["actor"] = self.actor
        return d


# --------------------------------------------------------------------------- goals / plans

@dataclass
class Goal:
    text: str
    budget_usd: float = 2.0
    run_id: str = field(default_factory=lambda: new_id("run"))
    constraints: dict = field(default_factory=dict)   # filled by the clarifier (budget/location/...)
    # Optional WALL-CLOCK budget: keep trying (relentlessly) for up to this many seconds. The operator
    # converts it to an absolute RunState.deadline_ts at start. None = no time bound (budget + hard caps
    # only). This is the load-bearing terminator for non-model work (integration/browser loops cost ~$0).
    max_runtime_s: float | None = None


@dataclass
class PlanStep:
    """One node of the master DAG. ``depends_on`` are 0-based, backward-only indices."""
    index: int
    text: str
    kind: StepKind = StepKind.GENERIC
    depends_on: list[int] = field(default_factory=list)
    needs: list[str] = field(default_factory=list)        # capability ids the step requires
    params: dict = field(default_factory=dict)            # kind-specific config (batch size, wait window/k, monitor match)
    done_criteria: list[dict] = field(default_factory=list)  # objective_check dicts the gate evaluates
    status: StepStatus = StepStatus.PENDING
    result: StepResult | None = None


@dataclass
class Plan:
    steps: list[PlanStep] = field(default_factory=list)
    goal_text: str = ""
    # The PROPOSED mandate the planner emits (the autonomy scope the owner approves once on a card). Empty
    # = no mandate. Persisted with the plan, so it survives the restart between showing the card and the
    # owner answering; the operator copies it to ``RunState.mandate`` only on approval. See flowers.mandate.
    mandate: dict = field(default_factory=dict)

    def ready_indices(self) -> list[int]:
        """Indices whose deps are all DONE and that are themselves not yet done/running/failed."""
        done = {s.index for s in self.steps if s.status is StepStatus.DONE}
        ready = []
        for s in self.steps:
            if s.status is not StepStatus.PENDING:
                continue
            if all(d in done for d in s.depends_on):
                ready.append(s.index)
        return ready

    def is_complete(self) -> bool:
        return all(s.status in (StepStatus.DONE, StepStatus.SKIPPED) for s in self.steps) and bool(self.steps)


# --------------------------------------------------------------------------- results

@dataclass
class ToolCall:
    name: str
    args: dict = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("tc"))


@dataclass
class StepResult:
    """The outcome of running one step's executor loop."""
    claimed_done: bool = False
    ok: bool = True
    text: str = ""                                   # the executor's final summary
    effects: list[EffectRecord] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)  # §5.3-style events for the gate (read/write/run/finish)
    signals: dict = field(default_factory=dict)       # await_replies / replan / capability_missing etc.
    searches: int = 0
    tool_failures: int = 0


# --------------------------------------------------------------------------- approvals / escalation

@dataclass
class ApprovalRequest:
    """A single owner-facing question the run parks on: clarifying questions, a side-effect to
    authorize, the autonomy-mandate card, an undo confirmation, or an escalation review."""
    run_id: str
    kind: str                       # "clarify" | "side_effect" | "never" | "undo" | "mandate" | "review"
    prompt: str
    options: list[str] = field(default_factory=list)
    tier: str | None = None      # for side_effect/never: the policy tier
    effect_label: str = ""          # for side_effect/never: toolkit:action
    id: str = field(default_factory=lambda: new_id("apr"))
    ts: float = field(default_factory=now_ts)


@dataclass
class RunState:
    run_id: str
    goal_text: str
    budget_usd: float
    status: RunStatus = RunStatus.PENDING
    replans: int = 0
    # Whole-plan RE-ARCHITECTURES on step failure (lever 2), bounded by operator._MAX_REPLANS. Kept
    # SEPARATE from ``replans`` (the await/next-batch counter) so the two relentless loops never share a
    # budget. Must round-trip in store._run_to_dict / _run_from_dict (field-explicit).
    dag_replans: int = 0
    spent_usd: float = 0.0
    pending_approval: ApprovalRequest | None = None
    # The ACTIVE mandate the broker enforces (empty until the owner approves the card). When non-empty,
    # an in-scope/in-cap/reversible action is auto-authorized without a per-action prompt — verification is
    # untouched. ``mandate_counts`` is the persisted magnitude counter (the anti-blast cap state). See
    # flowers.mandate. Both must round-trip in store._run_to_dict / _run_from_dict (field-explicit).
    mandate: dict = field(default_factory=dict)
    mandate_counts: dict = field(default_factory=dict)
    # Wall-clock relentlessness budget: an absolute wake-by timestamp (timers clock). None = no time bound.
    # The give-up sites keep trying until budget OR this deadline is exhausted (then escalate honestly).
    deadline_ts: float | None = None
    created_at: float = field(default_factory=now_ts)
    updated_at: float = field(default_factory=now_ts)

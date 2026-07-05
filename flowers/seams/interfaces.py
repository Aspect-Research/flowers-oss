"""The fixed seam contracts. Implementations (Fakes + live adapters) conform to THESE.

Every Protocol carries ``available() -> bool`` so the engine can pick a live adapter when wired and
fall back to a Fake otherwise. Keep this module dependency-light (stdlib + flowers.types) so it can
be imported anywhere without pulling in a backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from flowers.types import EffectRecord, ToolCall

# --------------------------------------------------------------------------- model

@dataclass
class ModelResponse:
    """A single model completion. ``cost_usd`` is the AUTHORITATIVE cost (from the provider's usage
    when live; 0.0 for fakes). ``tool_calls`` is the structured tool-call list the executor dispatches."""
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"        # "stop" | "tool_calls" | "length" | "error"
    cost_usd: float = 0.0
    raw: dict = field(default_factory=dict)


@runtime_checkable
class ModelClient(Protocol):
    """OpenAI-shape chat/tool-calling over OpenRouter, role-keyed and swappable.

    ``role`` selects a model config (e.g. ``"planner"`` -> a strong model @ high reasoning;
    ``"executor"`` -> a cheap high-throughput model). ``messages`` is the OpenAI message list (dicts);
    ``tools`` is the OpenAI tool-spec list (dicts) or None; ``response_format`` is an optional
    json-schema spec for structured output (used for planner output)."""

    def available(self) -> bool: ...

    def complete(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        role: str = "executor",
        response_format: dict | None = None,
        max_tokens: int | None = None,
    ) -> ModelResponse: ...


# --------------------------------------------------------------------------- search

@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""


@dataclass
class SearchResponse:
    """THE load-bearing fix vs an earlier prototype's silent-empty-as-success bug:

      * a SUCCESSFUL search with no hits  -> ``ok=True,  results=[]``
      * a BLOCKED/failed/rate-limited one -> ``ok=False, reason="..."`` (never disguised as empty)

    The executor must treat ``ok=False`` as a tool failure (circuit-break / switch), and ``ok=True``
    with ``results=[]`` as a genuine "no results" (use what you have / refine ONCE)."""
    ok: bool
    query: str
    results: list[SearchResult] = field(default_factory=list)
    reason: str | None = None       # why it failed, when ok is False (rate_limited/blocked/error)
    error: str | None = None


@dataclass
class FetchResponse:
    ok: bool
    url: str
    status: int = 0
    title: str = ""
    text: str = ""
    error: str | None = None


@runtime_checkable
class SearchClient(Protocol):
    """Web search + fetch. The honest ``ok`` contract above is mandatory for every implementation."""

    def available(self) -> bool: ...

    def search(self, query: str, *, k: int = 6) -> SearchResponse: ...

    def fetch(self, url: str) -> FetchResponse: ...   # MUST apply the SSRF guard


# --------------------------------------------------------------------------- integrations

@dataclass
class ExecResult:
    ok: bool
    data: Any = None
    error: str | None = None


@runtime_checkable
class Integrations(Protocol):
    """Per-user OAuth tool calling (Gmail/Calendar/Slack/...), backed by Arcade when keyed.

    The model never sees the OAuth token. The broker orchestrates verification by taking an
    INDEPENDENT ``snapshot`` of the read-back surface BEFORE and AFTER ``execute``, then matching the
    AFTER state against ``fingerprint`` (see ``flowers.effects``). An action with no read-back surface
    returns ``snapshot(...) is None`` -> the gate routes it to ``unverifiable`` (ask the owner).
    """

    def available(self) -> bool: ...

    def execute(self, *, toolkit: str, action: str, params: dict, user_id: str) -> ExecResult: ...

    def snapshot(self, *, toolkit: str, action: str, params: dict, user_id: str) -> dict | None:
        """An independent read-back of the surface this action affects, as ``{item_id: {field: value}}``.
        Return ``None`` when no reliable read-back exists for this (toolkit, action)."""
        ...

    def fingerprint(self, *, toolkit: str, action: str, params: dict) -> dict | None:
        """The expected-effect fingerprint for an added item (e.g. ``{"to": "...", "subject": "..."}``).
        Return ``None`` when the action has no precise fingerprint (verification falls back to drift)."""
        ...


# --------------------------------------------------------------------------- browser (no-API last-mile)

@dataclass
class BrowserActResult:
    """The outcome of one browser action. ``actor`` is the identity of the SESSION that performed it —
    load-bearing for the trust path: the gate verifies a browser side-effect only via an observer
    DISTINCT from this actor (never the actor's own screenshot/self-report)."""
    ok: bool
    actor: str = ""               # identity of the acting browser session (observer must differ)
    text: str = ""               # page/extracted text after the action (for read/extract actions)
    url: str = ""
    data: Any = None
    elements: list = field(default_factory=list)   # candidate actionable elements from an `inspect` read
    error: str | None = None


@runtime_checkable
class Browser(Protocol):
    """Headless browser automation for the no-API LAST MILE (a form submit, a booking) — the capability
    that needs a browser precisely because there is no API. FakeBrowser (offline) drives a scriptable
    world model; BrowserbaseBrowser drives a real cloud session.

    THE trust contract (mirrors Integrations, but provenance-required): a side-effecting browser action
    is NEVER verified by the acting agent's own screenshot/self-report — the gate rejects those. It is
    verified only by an INDEPENDENT observation: ``observe(...)`` re-reads the affected surface (a fresh
    session re-loading the confirmation page, or an out-of-band read) as ``{item_id: {field: value}}`` —
    the SAME shape ``effects.snapshot_diff`` consumes — performed under ``observer_id(...)``, an identity
    DISTINCT from the action's ``actor``. The broker emits an ``effect_kind='cua'`` record with that
    observer!=actor, so the gate's provenance branch can match the expected fingerprint independently.
    ``observe`` returns ``None`` when no independent observation exists -> the gate routes to ask-owner.
    """

    def available(self) -> bool: ...

    def act(self, *, action: str, params: dict, user_id: str) -> BrowserActResult: ...

    def observe(self, *, action: str, params: dict, user_id: str) -> dict | None: ...

    def observer_id(self, user_id: str) -> str: ...   # identity of the INDEPENDENT observer (!= actor)

    def fingerprint(self, *, action: str, params: dict) -> dict | None: ...


# --------------------------------------------------------------------------- sandbox

@dataclass
class SandboxResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


@runtime_checkable
class Sandbox(Protocol):
    """Isolated execution for a run. LocalSubprocessSandbox (the wired default) runs in a scoped workdir
    with the environment stripped of secrets and a dangerous-shell guard; the optional E2BSandbox adapter
    (flowers/extras/sandbox.py) runs in a Firecracker microVM whose only egress is the broker. The
    executor's shell/file tools go through THIS — it never holds platform credentials."""

    def available(self) -> bool: ...

    def workdir(self) -> str: ...

    def run(self, command: str, *, timeout: float = 60.0) -> SandboxResult: ...

    def write_file(self, relpath: str, content: str) -> None: ...

    def read_file(self, relpath: str) -> str: ...

    def list_files(self) -> list[str]: ...   # workdir-relative paths

    def snapshot(self) -> dict: ...          # {relpath: hash} — for box-observation staleness

    def close(self) -> None: ...


# --------------------------------------------------------------------------- store

@runtime_checkable
class Store(Protocol):
    """Durable run state + plan + effect log + approvals + usage. The wired ``SqliteStore`` and the
    optional ``PostgresStore`` implement this. Crash-anytime: every mutation is committed, so a fresh
    process can resume from ``get_run`` + ``get_plan`` + the persisted timers."""

    # --- runs ---
    def create_run(self, run) -> None: ...                # run: flowers.types.RunState
    def get_run(self, run_id: str): ...                   # -> RunState | None
    def save_run(self, run) -> None: ...
    def list_runs(self, tenant_id: str) -> list: ...      # -> list[RunState]
    def running_runs(self) -> list: ...                   # -> list[RunState] in status RUNNING (crash sweep)

    # --- plans ---
    def save_plan(self, run_id: str, plan) -> None: ...   # plan: flowers.types.Plan
    def get_plan(self, run_id: str): ...                  # -> Plan | None

    # --- effects ---
    def append_effect(self, run_id: str, effect: EffectRecord) -> None: ...
    def get_effects(self, run_id: str) -> list[EffectRecord]: ...

    # --- events (the durable owner-facing per-run log the dashboard replays) ---
    # append_event assigns and returns a per-run monotonic id (1-based, gapless) — the SSE resume
    # cursor (Last-Event-ID). Durability is the point: a reconnecting client and a RESTARTED server
    # replay the same timeline (the in-memory-only log was the "restart blanks the dashboard" bug).
    def append_event(self, run_id: str, event: dict) -> int: ...
    def get_events(self, run_id: str, *, after: int = 0) -> list[dict]: ...

    # --- mid-run owner notes (messages that arrive while the run is driving) ---
    # A durable queue, consumed atomically at the operator's next decision point. Notes are prompt
    # CONTEXT only — they never mint grants or bypass the approval path.
    def add_note(self, run_id: str, text: str) -> None: ...
    def take_notes(self, run_id: str) -> list: ...        # -> list[str], marks them consumed

    # --- approvals ---
    def save_approval(self, approval) -> None: ...        # approval: flowers.types.ApprovalRequest
    # The read-side of save_approval. No product caller today (the operator resolves via
    # get_answer), but it completes the approvals CRUD contract every Store impl honors + is the natural hook
    # for an approvals admin/inspection surface. Kept as deliberate Protocol surface, tested as the contract.
    def get_approval(self, approval_id: str): ...         # -> ApprovalRequest | None
    def resolve_approval(self, approval_id: str, answer: str) -> None: ...
    def get_answer(self, approval_id: str) -> str | None: ...

    # --- usage / metering ---
    def record_usage(self, *, tenant_id: str, run_id: str, kind: str, cost_usd: float, detail: dict) -> None: ...
    def run_spend(self, run_id: str) -> float: ...

    # --- continuation (durable resume-at-action: authorized grants + parked executor resume-state) ---
    # A per-run JSON blob the operator persists so a FRESH process can resume a parked run exactly,
    # instead of re-deriving the action under a bare-label grant (the P3-review cross-restart gap).
    def save_continuation(self, run_id: str, data: dict) -> None: ...
    def get_continuation(self, run_id: str) -> dict | None: ...

    # --- per-user memory (cross-session, self-curated markdown the operator carries between runs) ---
    # A long-lived genie should get to know its user: standing preferences, important facts, corrections/
    # redirections. The agent updates it via the `remember` tool; it is injected into planning/execution.
    def get_memory(self, tenant_id: str) -> str: ...
    def save_memory(self, tenant_id: str, content: str) -> None: ...

    # --- learned-trust counters (per-user clean-approval counts per action class) ---
    # After N clean owner approvals of a reversible non-delivering action class, flowers stops asking for
    # it (see flowers.mandate.learned_covers). A small {label: count} dict; money/NEVER are never counted.
    def get_trust(self, tenant_id: str) -> dict: ...
    def save_trust(self, tenant_id: str, counts: dict) -> None: ...

    # --- persistent browser contexts (logged-in session profiles, keyed by site) ---
    # A Browserbase context id that persists cookies/localStorage for one (tenant, site), so a logged-in
    # session survives across runs (the login-wall unlock). The id is a CAPABILITY (treat like a secret):
    # it is stored here + handed to Browserbase, never surfaced to the model. Site-scoped: a context is
    # only ever reused for the SAME site that created it (no credential bleed across sites).
    def get_browser_context(self, tenant_id: str, site: str) -> str | None: ...
    def save_browser_context(self, tenant_id: str, site: str, context_id: str) -> None: ...



# --------------------------------------------------------------------------- durable timers

@dataclass
class Timer:
    id: str
    run_id: str
    wake_at: float
    kind: str                      # "await_replies" | "monitor" | "clarify" | ...
    payload: dict = field(default_factory=dict)


@runtime_checkable
class DurableTimers(Protocol):
    """The durable wait/heartbeat primitive — the antidote to "re-search instead of waiting."

    The scheduler parks a run by ``schedule``-ing a timer (persisted); a poller resumes runs whose
    timers are ``due``. ``LocalTimers`` uses a real clock plus ``advance`` for the virtual clock the
    offline test suite fast-forwards. Crash-safety: timers persist, so a restarted process re-arms them
    from ``due``."""

    def available(self) -> bool: ...

    def now(self) -> float: ...

    def schedule(self, *, run_id: str, wake_at: float, kind: str, payload: dict | None = None) -> Timer: ...

    def due(self, *, at: float | None = None) -> list[Timer]: ...

    def cancel(self, timer_id: str) -> None: ...

    def cancel_for_run(self, run_id: str) -> None: ...

    def advance(self, seconds: float) -> None: ...   # virtual-clock fast-forward (dev/test only)


# --------------------------------------------------------------------------- telemetry

@dataclass
class Span:
    name: str
    tenant_id: str = ""
    run_id: str = ""
    attributes: dict = field(default_factory=dict)
    cost_usd: float = 0.0
    error: str | None = None


@runtime_checkable
class Tracer(Protocol):
    """Observability seam. NoOpTracer (default) discards; LocalTracer records spans in-memory for
    tests + a local dashboard; the optional Langfuse adapter ships OTel spans tagged ``run_id`` for
    per-run cost rollup. Used as a context manager: ``with tracer.span("plan", run_id=...) as s:``."""

    def available(self) -> bool: ...

    def span(self, name: str, *, tenant_id: str = "", run_id: str = "", **attrs) -> SpanHandle: ...

    def spans(self) -> list[Span]: ...   # recorded spans (LocalTracer); [] for NoOp/live


@runtime_checkable
class SpanHandle(Protocol):
    """The object returned by ``Tracer.span`` — a context manager that records duration/cost/error."""

    def __enter__(self) -> SpanHandle: ...
    def __exit__(self, exc_type, exc, tb) -> bool: ...
    # Set arbitrary span attributes. Used by tests as the contract; no product caller
    # yet (the engine sets cost via add_cost), but it is core OTel span-attribute surface every tracer
    # honors and the obvious hook for richer per-span tagging. Kept as deliberate Protocol surface.
    def set(self, **attrs) -> None: ...
    def add_cost(self, usd: float) -> None: ...

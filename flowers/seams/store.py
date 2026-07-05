"""Store seam — durable run state + plan + effect log + approvals + usage.

The :class:`Store` Protocol (see ``flowers.seams.interfaces``) has no ``available()`` /
network surface: it is a pure persistence seam. SqliteStore is the wired default implementation
(``sqlite3``); the optional ``PostgresStore`` adapter (``flowers/extras/store.py``) is an alternative
that reuses the serializers defined here. Both speak the same Protocol.

Serialization lives HERE, not in ``flowers.types`` (those stay pure dataclasses+enums). We
convert dataclasses to JSON-friendly dicts for sqlite (enums via ``.value``, nested
dataclasses recursed, Optional fields handled) and reconstruct fully-typed objects on read.
Round-trip fidelity is the property the tests pin down.

Crash-anytime: every mutation commits immediately, so a fresh process re-opening the same db
path resumes from ``get_run`` + ``get_plan`` + the persisted effects/approvals.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading

from flowers.types import (
    ApprovalRequest,
    EffectRecord,
    Plan,
    PlanStep,
    RunState,
    RunStatus,
    StepKind,
    StepResult,
    StepStatus,
)

# --------------------------------------------------------------------------- (de)serialization

def _effect_to_dict(e: EffectRecord) -> dict:
    """Flatten an EffectRecord to a JSON-serializable dict (all fields, in declaration order)."""
    return {
        "toolkit": e.toolkit,
        "action": e.action,
        "side_effecting": e.side_effecting,
        "phase": e.phase,
        "drift_present": e.drift_present,
        "expected_present": e.expected_present,
        "effect_kind": e.effect_kind,
        "verification": e.verification,
        "observer": e.observer,
        "actor": e.actor,
        "action_id": e.action_id,
        "label": e.label,
        "detail": dict(e.detail),
        "ts": e.ts,
    }


def _effect_from_dict(d: dict) -> EffectRecord:
    return EffectRecord(
        toolkit=d["toolkit"],
        action=d["action"],
        side_effecting=d.get("side_effecting"),
        phase=d.get("phase", "attempted"),
        drift_present=d.get("drift_present"),
        expected_present=d.get("expected_present"),
        effect_kind=d.get("effect_kind", "composio"),
        verification=d.get("verification"),
        observer=d.get("observer"),
        actor=d.get("actor"),
        action_id=d["action_id"],
        label=d.get("label", ""),
        detail=dict(d.get("detail") or {}),
        ts=d["ts"],
    )


def _step_result_to_dict(r: StepResult) -> dict:
    return {
        "claimed_done": r.claimed_done,
        "ok": r.ok,
        "text": r.text,
        "effects": [_effect_to_dict(e) for e in r.effects],
        "events": [dict(ev) for ev in r.events],
        "signals": dict(r.signals),
        "searches": r.searches,
        "tool_failures": r.tool_failures,
    }


def _step_result_from_dict(d: dict | None) -> StepResult | None:
    if d is None:
        return None
    return StepResult(
        claimed_done=d.get("claimed_done", False),
        ok=d.get("ok", True),
        text=d.get("text", ""),
        effects=[_effect_from_dict(e) for e in d.get("effects", [])],
        events=[dict(ev) for ev in d.get("events", [])],
        signals=dict(d.get("signals") or {}),
        searches=d.get("searches", 0),
        tool_failures=d.get("tool_failures", 0),
    )


def _step_to_dict(s: PlanStep) -> dict:
    return {
        "index": s.index,
        "text": s.text,
        "kind": s.kind.value,
        "depends_on": list(s.depends_on),
        "needs": list(s.needs),
        "params": dict(s.params),
        "done_criteria": [dict(c) for c in s.done_criteria],
        "status": s.status.value,
        "result": _step_result_to_dict(s.result) if s.result is not None else None,
    }


def _coerce_step_kind(value) -> StepKind:
    """Degrade an unknown StepKind to GENERIC (forward/back-compat: a plan persisted by newer code with a
    kind this build doesn't know still loads), mirroring the planner's _coerce_kind."""
    try:
        return StepKind(value or StepKind.GENERIC.value)
    except ValueError:
        return StepKind.GENERIC


def _step_from_dict(d: dict) -> PlanStep:
    return PlanStep(
        index=d["index"],
        text=d["text"],
        kind=_coerce_step_kind(d.get("kind", StepKind.GENERIC.value)),
        depends_on=list(d.get("depends_on", [])),
        needs=list(d.get("needs", [])),
        params=dict(d.get("params") or {}),
        done_criteria=[dict(c) for c in d.get("done_criteria", [])],
        status=StepStatus(d.get("status", StepStatus.PENDING.value)),
        result=_step_result_from_dict(d.get("result")),
    )


def _plan_to_dict(p: Plan) -> dict:
    return {"steps": [_step_to_dict(s) for s in p.steps], "goal_text": p.goal_text,
            "mandate": dict(p.mandate or {})}


def _plan_from_dict(d: dict) -> Plan:
    return Plan(steps=[_step_from_dict(s) for s in d.get("steps", [])], goal_text=d.get("goal_text", ""),
                mandate=dict(d.get("mandate") or {}))


def _approval_to_dict(a: ApprovalRequest) -> dict:
    return {
        "run_id": a.run_id,
        "kind": a.kind,
        "prompt": a.prompt,
        "options": list(a.options),
        "tier": a.tier,
        "effect_label": a.effect_label,
        "id": a.id,
        "ts": a.ts,
    }


def _approval_from_dict(d: dict) -> ApprovalRequest:
    return ApprovalRequest(
        run_id=d["run_id"],
        kind=d["kind"],
        prompt=d["prompt"],
        options=list(d.get("options", [])),
        tier=d.get("tier"),
        effect_label=d.get("effect_label", ""),
        id=d["id"],
        ts=d["ts"],
    )


def _run_to_dict(r: RunState) -> dict:
    return {
        "run_id": r.run_id,
        "tenant_id": r.tenant_id,
        "goal_text": r.goal_text,
        "budget_usd": r.budget_usd,
        "status": r.status.value,
        "replans": r.replans,
        "dag_replans": r.dag_replans,
        "spent_usd": r.spent_usd,
        "pending_approval": _approval_to_dict(r.pending_approval) if r.pending_approval is not None else None,
        "mandate": dict(r.mandate or {}),
        "mandate_counts": dict(r.mandate_counts or {}),
        "deadline_ts": r.deadline_ts,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
    }


def _run_from_dict(d: dict) -> RunState:
    pa = d.get("pending_approval")
    return RunState(
        run_id=d["run_id"],
        tenant_id=d["tenant_id"],
        goal_text=d["goal_text"],
        budget_usd=d["budget_usd"],
        status=RunStatus(d.get("status", RunStatus.PENDING.value)),
        replans=d.get("replans", 0),
        dag_replans=d.get("dag_replans", 0),
        spent_usd=d.get("spent_usd", 0.0),
        pending_approval=_approval_from_dict(pa) if pa is not None else None,
        mandate=dict(d.get("mandate") or {}),
        mandate_counts=dict(d.get("mandate_counts") or {}),
        deadline_ts=d.get("deadline_ts"),
        created_at=d["created_at"],
        updated_at=d["updated_at"],
    )


# --------------------------------------------------------------------------- store

class SqliteStore:
    """``sqlite3``-backed implementation of the :class:`flowers.seams.interfaces.Store` Protocol.

    Tables are created on init. Every mutation commits immediately (crash-anytime). Pass a file
    path to persist across processes; the default ``":memory:"`` is ephemeral (good for tests).
    """

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        # ONE connection shared by every thread that touches the store: the event-loop thread (SSE
        # polls, readiness probes), the background drive worker, and the tick poller. sqlite3 forbids
        # concurrent use of one connection, so ALL access — reads included — is serialized by a
        # process-local lock (``_locked``). WAL + busy_timeout guard the other axis: a SECOND process
        # (or the timers db) on the same file waits briefly instead of raising "database is locked",
        # and WAL readers never block the writer.
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._db.execute("PRAGMA busy_timeout = 5000")
        self._db.execute("PRAGMA journal_mode = WAL")     # no-op ("memory") for :memory: stores
        self._db.execute("PRAGMA synchronous = NORMAL")   # safe under WAL; every mutation still commits
        self._init_tables()

    @contextlib.contextmanager
    def _locked(self):
        """Serialize all access to the shared connection (reads AND writes — concurrent reads on one
        sqlite3 connection raise 'recursive use of cursors'). Re-entrant, so a compound operation can
        hold the lock across a read-then-write without deadlocking on its own helpers."""
        with self._lock:
            yield self._db

    def _init_tables(self) -> None:
        with self._locked() as c:
            self._create_tables(c)

    @staticmethod
    def _create_tables(c) -> None:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id    TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                data      TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS plans (
                run_id TEXT PRIMARY KEY,
                data   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS effects (
                seq    INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                data   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                run_id      TEXT NOT NULL,
                data        TEXT NOT NULL,
                answer      TEXT
            );
            CREATE TABLE IF NOT EXISTS usage (
                seq       INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                run_id    TEXT NOT NULL,
                kind      TEXT NOT NULL,
                cost_usd  REAL NOT NULL,
                detail    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS continuations (
                run_id TEXT PRIMARY KEY,
                data   TEXT NOT NULL
            );
            -- The durable owner-facing event log (plan announcements, progress, approvals, done):
            -- what the dashboard replays after a reconnect OR a server restart. eid is the per-run
            -- monotonic SSE resume cursor. Retained for the life of the run's row, like effects/usage
            -- (it is the audit trail); events are small JSON rows appended at human pace.
            CREATE TABLE IF NOT EXISTS events (
                seq    INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                eid    INTEGER NOT NULL,
                data   TEXT NOT NULL
            );
            -- Owner messages that arrived while the run was mid-drive (no parked question to answer).
            -- A side table rather than a RunState field: the drive thread rewrites the run's JSON blob
            -- on every save_run, so a note stored inside RunState by the answer thread would be lost
            -- to a concurrent overwrite. take_notes() consumes atomically under the store lock.
            CREATE TABLE IF NOT EXISTS run_notes (
                seq      INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id   TEXT NOT NULL,
                text     TEXT NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS user_memory (
                tenant_id TEXT PRIMARY KEY,
                content   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trust_counts (
                tenant_id TEXT PRIMARY KEY,
                data      TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS browser_contexts (
                tenant_id  TEXT NOT NULL,
                site       TEXT NOT NULL,
                context_id TEXT NOT NULL,
                PRIMARY KEY (tenant_id, site)
            );
            CREATE INDEX IF NOT EXISTS idx_effects_run   ON effects(run_id, seq);
            CREATE INDEX IF NOT EXISTS idx_usage_run     ON usage(run_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_events_run_eid ON events(run_id, eid);
            CREATE INDEX IF NOT EXISTS idx_notes_run     ON run_notes(run_id, consumed);
            """
        )
        c.commit()

    # --- runs ---

    def create_run(self, run: RunState) -> None:
        with self._locked() as c:
            c.execute(
                "INSERT INTO runs (run_id, tenant_id, data) VALUES (?, ?, ?)",
                (run.run_id, run.tenant_id, json.dumps(_run_to_dict(run))),
            )
            c.commit()

    def get_run(self, run_id: str) -> RunState | None:
        with self._locked() as c:
            row = c.execute("SELECT data FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return _run_from_dict(json.loads(row["data"]))

    def save_run(self, run: RunState) -> None:
        """Upsert: update an existing run or insert if new (idempotent persistence)."""
        with self._locked() as c:
            c.execute(
                "INSERT INTO runs (run_id, tenant_id, data) VALUES (?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET tenant_id = excluded.tenant_id, data = excluded.data",
                (run.run_id, run.tenant_id, json.dumps(_run_to_dict(run))),
            )
            c.commit()

    def list_runs(self, tenant_id: str) -> list[RunState]:
        with self._locked() as c:
            rows = c.execute(
                "SELECT data FROM runs WHERE tenant_id = ? ORDER BY rowid", (tenant_id,)
            ).fetchall()
        return [_run_from_dict(json.loads(r["data"])) for r in rows]

    def running_runs(self) -> list[RunState]:
        """Runs in a synchronous in-flight state (RUNNING or PLANNING), across all runs — the crash-recovery
        sweep's input. Neither state schedules a timer, so a run found here at process startup is necessarily
        a crash orphan (nothing is actively driving yet). Filtered in SQL so a growing runs table is not
        fully deserialized."""
        with self._locked() as c:
            rows = c.execute(
                "SELECT data FROM runs WHERE json_extract(data, '$.status') IN (?, ?) ORDER BY rowid",
                (RunStatus.RUNNING.value, RunStatus.PLANNING.value)).fetchall()
        return [_run_from_dict(json.loads(r["data"])) for r in rows]

    # --- plans ---

    def save_plan(self, run_id: str, plan: Plan) -> None:
        with self._locked() as c:
            c.execute(
                "INSERT INTO plans (run_id, data) VALUES (?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET data = excluded.data",
                (run_id, json.dumps(_plan_to_dict(plan))),
            )
            c.commit()

    def get_plan(self, run_id: str) -> Plan | None:
        with self._locked() as c:
            row = c.execute("SELECT data FROM plans WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return _plan_from_dict(json.loads(row["data"]))

    # --- effects ---

    def append_effect(self, run_id: str, effect: EffectRecord) -> None:
        with self._locked() as c:
            c.execute(
                "INSERT INTO effects (run_id, data) VALUES (?, ?)",
                (run_id, json.dumps(_effect_to_dict(effect))),
            )
            c.commit()

    def get_effects(self, run_id: str) -> list[EffectRecord]:
        """Return this run's effects in append order (the autoincrement seq preserves it)."""
        with self._locked() as c:
            rows = c.execute(
                "SELECT data FROM effects WHERE run_id = ? ORDER BY seq", (run_id,)
            ).fetchall()
        return [_effect_from_dict(json.loads(r["data"])) for r in rows]

    # --- events (the durable owner-facing per-run log the dashboard replays) ---

    def append_event(self, run_id: str, event: dict) -> int:
        """Append an owner-facing event to the run's durable log; returns its per-run monotonic id.
        The id is assigned under the store lock (MAX(eid)+1), so ids are gapless and strictly
        increasing per run — the resume cursor the SSE protocol (Last-Event-ID) relies on."""
        with self._locked() as c:
            row = c.execute(
                "SELECT COALESCE(MAX(eid), 0) + 1 AS n FROM events WHERE run_id = ?", (run_id,)
            ).fetchone()
            eid = int(row["n"])
            c.execute(
                "INSERT INTO events (run_id, eid, data) VALUES (?, ?, ?)",
                (run_id, eid, json.dumps(dict(event))),
            )
            c.commit()
        return eid

    def get_events(self, run_id: str, *, after: int = 0) -> list[dict]:
        """The run's events with eid > ``after``, in order. Each dict carries its ``id`` (the eid)."""
        with self._locked() as c:
            rows = c.execute(
                "SELECT eid, data FROM events WHERE run_id = ? AND eid > ? ORDER BY eid",
                (run_id, int(after)),
            ).fetchall()
        return [{**json.loads(r["data"]), "id": r["eid"]} for r in rows]

    # --- mid-run owner notes (messages that arrive while the run is driving) ---

    def add_note(self, run_id: str, text: str) -> None:
        with self._locked() as c:
            c.execute("INSERT INTO run_notes (run_id, text) VALUES (?, ?)", (run_id, text))
            c.commit()

    def take_notes(self, run_id: str) -> list[str]:
        """Return + mark-consumed the run's unconsumed owner notes, in arrival order — atomic under
        the store lock, so a note is folded into exactly one decision point (never two, never zero)."""
        with self._locked() as c:
            rows = c.execute(
                "SELECT seq, text FROM run_notes WHERE run_id = ? AND consumed = 0 ORDER BY seq",
                (run_id,),
            ).fetchall()
            if rows:
                c.executemany("UPDATE run_notes SET consumed = 1 WHERE seq = ?",
                              [(r["seq"],) for r in rows])
                c.commit()
        return [r["text"] for r in rows]

    # --- approvals ---

    def save_approval(self, approval: ApprovalRequest) -> None:
        with self._locked() as c:
            c.execute(
                "INSERT INTO approvals (approval_id, run_id, data, answer) VALUES (?, ?, ?, NULL) "
                "ON CONFLICT(approval_id) DO UPDATE SET run_id = excluded.run_id, data = excluded.data",
                (approval.id, approval.run_id, json.dumps(_approval_to_dict(approval))),
            )
            c.commit()

    def get_approval(self, approval_id: str) -> ApprovalRequest | None:
        with self._locked() as c:
            row = c.execute(
                "SELECT data FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        if row is None:
            return None
        return _approval_from_dict(json.loads(row["data"]))

    def resolve_approval(self, approval_id: str, answer: str) -> None:
        with self._locked() as c:
            c.execute(
                "UPDATE approvals SET answer = ? WHERE approval_id = ?", (answer, approval_id)
            )
            c.commit()

    def get_answer(self, approval_id: str) -> str | None:
        with self._locked() as c:
            row = c.execute(
                "SELECT answer FROM approvals WHERE approval_id = ?", (approval_id,)
            ).fetchone()
        if row is None:
            return None
        return row["answer"]

    # --- usage / metering ---

    def record_usage(
        self, *, tenant_id: str, run_id: str, kind: str, cost_usd: float, detail: dict
    ) -> None:
        with self._locked() as c:
            c.execute(
                "INSERT INTO usage (tenant_id, run_id, kind, cost_usd, detail) VALUES (?, ?, ?, ?, ?)",
                (tenant_id, run_id, kind, float(cost_usd), json.dumps(dict(detail or {}))),
            )
            c.commit()

    def run_spend(self, run_id: str) -> float:
        with self._locked() as c:
            row = c.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM usage WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return float(row["total"])

    # --- continuation (durable resume-at-action) ---

    def save_continuation(self, run_id: str, data: dict) -> None:
        with self._locked() as c:
            c.execute(
                "INSERT INTO continuations (run_id, data) VALUES (?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET data = excluded.data",
                (run_id, json.dumps(dict(data or {}))),
            )
            c.commit()

    def get_continuation(self, run_id: str) -> dict | None:
        with self._locked() as c:
            row = c.execute(
                "SELECT data FROM continuations WHERE run_id = ?", (run_id,)
            ).fetchone()
        return json.loads(row["data"]) if row is not None else None

    # --- per-user memory (cross-session, self-curated markdown) ---

    def get_memory(self, tenant_id: str) -> str:
        with self._locked() as c:
            row = c.execute(
                "SELECT content FROM user_memory WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
        return row["content"] if row is not None else ""

    def save_memory(self, tenant_id: str, content: str) -> None:
        with self._locked() as c:
            c.execute(
                "INSERT INTO user_memory (tenant_id, content) VALUES (?, ?) "
                "ON CONFLICT(tenant_id) DO UPDATE SET content = excluded.content",
                (tenant_id, content or ""),
            )
            c.commit()

    # --- learned-trust counters (per-user approval counts per action class) ---

    def get_trust(self, tenant_id: str) -> dict:
        with self._locked() as c:
            row = c.execute(
                "SELECT data FROM trust_counts WHERE tenant_id = ?", (tenant_id,)
            ).fetchone()
        return json.loads(row["data"]) if row is not None else {}

    def save_trust(self, tenant_id: str, counts: dict) -> None:
        with self._locked() as c:
            c.execute(
                "INSERT INTO trust_counts (tenant_id, data) VALUES (?, ?) "
                "ON CONFLICT(tenant_id) DO UPDATE SET data = excluded.data",
                (tenant_id, json.dumps(counts or {})),
            )
            c.commit()

    # --- persistent browser contexts (per (tenant, site) logged-in session profile) ---

    def get_browser_context(self, tenant_id: str, site: str) -> str | None:
        with self._locked() as c:
            row = c.execute(
                "SELECT context_id FROM browser_contexts WHERE tenant_id = ? AND site = ?",
                (tenant_id, site),
            ).fetchone()
        return row["context_id"] if row is not None else None

    def save_browser_context(self, tenant_id: str, site: str, context_id: str) -> None:
        with self._locked() as c:
            c.execute(
                "INSERT INTO browser_contexts (tenant_id, site, context_id) VALUES (?, ?, ?) "
                "ON CONFLICT(tenant_id, site) DO UPDATE SET context_id = excluded.context_id",
                (tenant_id, site, context_id),
            )
            c.commit()

    def close(self) -> None:
        with self._locked() as c:
            c.close()

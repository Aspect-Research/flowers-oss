"""Optional store adapter — PostgreSQL (use your own Postgres instead of the default sqlite).

``PostgresStore`` is an optional adapter template (not wired into the default ``build_app``; the wired
default is ``SqliteStore`` in ``flowers/seams/store.py``). It implements the same ``Store`` Protocol with
the SAME JSON (de)serialization reused verbatim — only the engine differs, so a fabricated-completion
refusal, durable resume, etc. behave identically on Postgres. Backed by a Neon-aware
``psycopg_pool.ConnectionPool``; psycopg is imported lazily so the core imports without it. To use this,
swap it for ``SqliteStore`` in ``build_app`` and install the ``postgres`` extra.
"""

from __future__ import annotations

from flowers import runtime
from flowers.seams.store import (
    _approval_from_dict,
    _approval_to_dict,
    _effect_from_dict,
    _effect_to_dict,
    _plan_from_dict,
    _plan_to_dict,
    _run_from_dict,
    _run_to_dict,
)
from flowers.types import (
    ApprovalRequest,
    EffectRecord,
    Plan,
    RunState,
    RunStatus,
)


class PostgresStore:
    """PostgreSQL implementation of the :class:`flowers.seams.interfaces.Store` Protocol — an alternative
    to :class:`SqliteStore`. Same schema, the SAME JSON (de)serialization reused verbatim;
    only the engine differs (so a fabricated-completion refusal, durable resume, etc. behave identically
    on Postgres). Backed by a Neon-aware ``psycopg_pool.ConnectionPool``.

    Neon/PgBouncer specifics: ``prepare_threshold=None`` (the transaction pooler can't keep
    session-scoped prepared statements), ``check=ConnectionPool.check_connection`` (validate/replace a
    connection the scale-to-zero pooler silently dropped), ``sslmode=require`` in the DSN (Neon refuses
    non-SSL). Each method is one ``with pool.connection()`` block = one auto-committed transaction —
    preserving SqliteStore's commit-immediately semantics. JSON is stored as native ``jsonb`` (psycopg
    auto-loads it back to a dict). psycopg is imported lazily so the stdlib core imports without it.
    """

    def __init__(self, dsn: str | None = None, *, min_size: int = 1, max_size: int = 8) -> None:
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
        from psycopg_pool import ConnectionPool
        self._Jsonb = Jsonb
        dsn = dsn or runtime.env("FLOWERS_DATABASE_URL") or runtime.env("DBOS_DATABASE_URL")
        if not dsn:
            raise RuntimeError(
                "PostgresStore needs a DSN (pass dsn=, or set FLOWERS_DATABASE_URL / DBOS_DATABASE_URL)")
        self._pool = ConnectionPool(
            dsn, min_size=min_size, max_size=max_size, open=True,
            check=ConnectionPool.check_connection,
            kwargs={"prepare_threshold": None, "row_factory": dict_row, "autocommit": False},
        )
        self._init_tables()

    def _init_tables(self) -> None:
        ddl = """
            CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, data JSONB NOT NULL);
            CREATE TABLE IF NOT EXISTS plans (run_id TEXT PRIMARY KEY, data JSONB NOT NULL);
            CREATE TABLE IF NOT EXISTS effects (seq BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL, data JSONB NOT NULL);
            CREATE TABLE IF NOT EXISTS approvals (approval_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, data JSONB NOT NULL, answer TEXT);
            CREATE TABLE IF NOT EXISTS usage_log (seq BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, cost_usd DOUBLE PRECISION NOT NULL, detail JSONB NOT NULL);
            CREATE TABLE IF NOT EXISTS events (seq BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL, eid BIGINT NOT NULL, data JSONB NOT NULL);
            CREATE TABLE IF NOT EXISTS run_notes (seq BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL, text TEXT NOT NULL, consumed BOOLEAN NOT NULL DEFAULT FALSE);
            CREATE TABLE IF NOT EXISTS continuations (run_id TEXT PRIMARY KEY, data JSONB NOT NULL);
            CREATE TABLE IF NOT EXISTS user_memory (id INTEGER PRIMARY KEY CHECK (id = 1), content TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trust_counts (id INTEGER PRIMARY KEY CHECK (id = 1), data JSONB NOT NULL);
            CREATE TABLE IF NOT EXISTS browser_contexts (site TEXT PRIMARY KEY, context_id TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_runs_status  ON runs((data->>'status'));
            CREATE INDEX IF NOT EXISTS idx_effects_run  ON effects(run_id, seq);
            CREATE INDEX IF NOT EXISTS idx_usage_run    ON usage_log(run_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_events_run_eid ON events(run_id, eid);
            CREATE INDEX IF NOT EXISTS idx_notes_run    ON run_notes(run_id, consumed);
        """
        stmts = [s.strip() for s in ddl.split(";") if s.strip()]
        with self._pool.connection() as conn:
            for stmt in stmts:
                conn.execute(stmt)


    # --- runs ---
    def create_run(self, run: RunState) -> None:
        with self._pool.connection() as conn:
            conn.execute("INSERT INTO runs (run_id, data) VALUES (%s, %s)",
                         (run.run_id, self._Jsonb(_run_to_dict(run))))

    def get_run(self, run_id: str) -> RunState | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT data FROM runs WHERE run_id = %s", (run_id,)).fetchone()
            return _run_from_dict(row["data"]) if row is not None else None

    def save_run(self, run: RunState) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, data) VALUES (%s, %s) "
                "ON CONFLICT (run_id) DO UPDATE SET data = EXCLUDED.data",
                (run.run_id, self._Jsonb(_run_to_dict(run))))

    def list_runs(self) -> list[RunState]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT data FROM runs "
                "ORDER BY (data->>'created_at')::double precision NULLS LAST, run_id").fetchall()
            return [_run_from_dict(r["data"]) for r in rows]

    def running_runs(self) -> list[RunState]:
        """Runs in a synchronous in-flight state (RUNNING/PLANNING), across all runs — the crash sweep's
        input (see SqliteStore). Index-backed by idx_runs_status."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT data FROM runs WHERE data->>'status' = ANY(%s)",
                ([RunStatus.RUNNING.value, RunStatus.PLANNING.value],)).fetchall()
            return [_run_from_dict(r["data"]) for r in rows]

    # --- plans ---
    def save_plan(self, run_id: str, plan: Plan) -> None:
        with self._pool.connection() as conn:
            conn.execute("INSERT INTO plans (run_id, data) VALUES (%s, %s) "
                         "ON CONFLICT (run_id) DO UPDATE SET data = EXCLUDED.data",
                         (run_id, self._Jsonb(_plan_to_dict(plan))))

    def get_plan(self, run_id: str) -> Plan | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT data FROM plans WHERE run_id = %s", (run_id,)).fetchone()
            return _plan_from_dict(row["data"]) if row is not None else None

    # --- effects ---
    def append_effect(self, run_id: str, effect: EffectRecord) -> None:
        with self._pool.connection() as conn:
            conn.execute("INSERT INTO effects (run_id, data) VALUES (%s, %s)",
                         (run_id, self._Jsonb(_effect_to_dict(effect))))

    def get_effects(self, run_id: str) -> list[EffectRecord]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT data FROM effects WHERE run_id = %s ORDER BY seq",
                                (run_id,)).fetchall()
            return [_effect_from_dict(r["data"]) for r in rows]

    # --- events (the durable owner-facing per-run log the dashboard replays) ---
    def append_event(self, run_id: str, event: dict) -> int:
        """Same contract as SqliteStore.append_event: a per-run monotonic, gapless eid (the SSE
        resume cursor). One transaction; the SELECT-then-INSERT races are resolved by the unique
        (run_id, eid) index + Postgres serializing on it (a conflict retries once)."""
        with self._pool.connection() as conn:
            for _ in range(2):
                row = conn.execute(
                    "SELECT COALESCE(MAX(eid), 0) + 1 AS n FROM events WHERE run_id = %s",
                    (run_id,)).fetchone()
                eid = int(row["n"])
                try:
                    conn.execute("INSERT INTO events (run_id, eid, data) VALUES (%s, %s, %s)",
                                 (run_id, eid, self._Jsonb(dict(event))))
                    return eid
                except Exception:   # unique-index race with a concurrent emitter: recompute once
                    conn.rollback()
            raise RuntimeError(f"could not append event for run {run_id} (eid contention)")

    def get_events(self, run_id: str, *, after: int = 0) -> list[dict]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT eid, data FROM events WHERE run_id = %s AND eid > %s ORDER BY eid",
                (run_id, int(after))).fetchall()
            return [{**r["data"], "id": r["eid"]} for r in rows]

    # --- mid-run owner notes (messages that arrive while the run is driving) ---
    def add_note(self, run_id: str, text: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("INSERT INTO run_notes (run_id, text) VALUES (%s, %s)", (run_id, text))

    def take_notes(self, run_id: str) -> list[str]:
        """Return + mark-consumed atomically (one transaction, row-locked by the UPDATE)."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "UPDATE run_notes SET consumed = TRUE "
                "WHERE run_id = %s AND consumed = FALSE RETURNING seq, text",
                (run_id,)).fetchall()
            return [r["text"] for r in sorted(rows, key=lambda r: r["seq"])]

    # --- approvals ---
    def save_approval(self, approval: ApprovalRequest) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO approvals (approval_id, run_id, data, answer) VALUES (%s, %s, %s, NULL) "
                "ON CONFLICT (approval_id) DO UPDATE SET run_id = EXCLUDED.run_id, data = EXCLUDED.data",
                (approval.id, approval.run_id, self._Jsonb(_approval_to_dict(approval))))

    def get_approval(self, approval_id: str) -> ApprovalRequest | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT data FROM approvals WHERE approval_id = %s",
                               (approval_id,)).fetchone()
            return _approval_from_dict(row["data"]) if row is not None else None

    def resolve_approval(self, approval_id: str, answer: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("UPDATE approvals SET answer = %s WHERE approval_id = %s", (answer, approval_id))

    def get_answer(self, approval_id: str) -> str | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT answer FROM approvals WHERE approval_id = %s",
                               (approval_id,)).fetchone()
            return row["answer"] if row is not None else None

    # --- usage / metering ---
    def record_usage(self, *, run_id: str, kind: str, cost_usd: float, detail: dict) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO usage_log (run_id, kind, cost_usd, detail) VALUES (%s, %s, %s, %s)",
                (run_id, kind, float(cost_usd), self._Jsonb(dict(detail or {}))))

    def run_spend(self, run_id: str) -> float:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM usage_log WHERE run_id = %s",
                               (run_id,)).fetchone()
            return float(row["total"])

    # --- continuation (durable resume-at-action) ---
    def save_continuation(self, run_id: str, data: dict) -> None:
        with self._pool.connection() as conn:
            conn.execute("INSERT INTO continuations (run_id, data) VALUES (%s, %s) "
                         "ON CONFLICT (run_id) DO UPDATE SET data = EXCLUDED.data",
                         (run_id, self._Jsonb(dict(data or {}))))

    def get_continuation(self, run_id: str) -> dict | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT data FROM continuations WHERE run_id = %s", (run_id,)).fetchone()
            return row["data"] if row is not None else None

    # --- per-user memory (cross-session, self-curated markdown) ---
    def get_memory(self) -> str:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT content FROM user_memory WHERE id = 1").fetchone()
            return row["content"] if row is not None else ""

    def save_memory(self, content: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("INSERT INTO user_memory (id, content) VALUES (1, %s) "
                         "ON CONFLICT (id) DO UPDATE SET content = EXCLUDED.content",
                         (content or "",))

    # --- learned-trust counters (per-user approval counts per action class) ---
    def get_trust(self) -> dict:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT data FROM trust_counts WHERE id = 1").fetchone()
            return dict(row["data"]) if row is not None else {}

    def save_trust(self, counts: dict) -> None:
        with self._pool.connection() as conn:
            conn.execute("INSERT INTO trust_counts (id, data) VALUES (1, %s) "
                         "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data",
                         (self._Jsonb(dict(counts or {})),))

    # --- persistent browser contexts (per-site logged-in session profile) ---
    def get_browser_context(self, site: str) -> str | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT context_id FROM browser_contexts WHERE site = %s",
                               (site,)).fetchone()
            return row["context_id"] if row is not None else None

    def save_browser_context(self, site: str, context_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("INSERT INTO browser_contexts (site, context_id) VALUES (%s, %s) "
                         "ON CONFLICT (site) DO UPDATE SET context_id = EXCLUDED.context_id",
                         (site, context_id))

    def close(self) -> None:
        self._pool.close()

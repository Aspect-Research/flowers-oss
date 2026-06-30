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
            CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, data JSONB NOT NULL);
            CREATE TABLE IF NOT EXISTS plans (run_id TEXT PRIMARY KEY, data JSONB NOT NULL);
            CREATE TABLE IF NOT EXISTS effects (seq BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL, data JSONB NOT NULL);
            CREATE TABLE IF NOT EXISTS approvals (approval_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, data JSONB NOT NULL, answer TEXT);
            CREATE TABLE IF NOT EXISTS usage_log (seq BIGSERIAL PRIMARY KEY, tenant_id TEXT NOT NULL, run_id TEXT NOT NULL, kind TEXT NOT NULL, cost_usd DOUBLE PRECISION NOT NULL, detail JSONB NOT NULL);
            CREATE TABLE IF NOT EXISTS continuations (run_id TEXT PRIMARY KEY, data JSONB NOT NULL);
            CREATE TABLE IF NOT EXISTS user_memory (tenant_id TEXT PRIMARY KEY, content TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS trust_counts (tenant_id TEXT PRIMARY KEY, data JSONB NOT NULL);
            CREATE TABLE IF NOT EXISTS browser_contexts (tenant_id TEXT NOT NULL, site TEXT NOT NULL, context_id TEXT NOT NULL, PRIMARY KEY (tenant_id, site));
            CREATE INDEX IF NOT EXISTS idx_runs_status  ON runs((data->>'status'));
            CREATE INDEX IF NOT EXISTS idx_effects_run  ON effects(run_id, seq);
            CREATE INDEX IF NOT EXISTS idx_usage_run    ON usage_log(run_id);
        """
        stmts = [s.strip() for s in ddl.split(";") if s.strip()]
        with self._pool.connection() as conn:
            for stmt in stmts:
                conn.execute(stmt)


    # --- runs ---
    def create_run(self, run: RunState) -> None:
        with self._pool.connection() as conn:
            conn.execute("INSERT INTO runs (run_id, tenant_id, data) VALUES (%s, %s, %s)",
                         (run.run_id, run.tenant_id, self._Jsonb(_run_to_dict(run))))

    def get_run(self, run_id: str) -> RunState | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT data FROM runs WHERE run_id = %s", (run_id,)).fetchone()
            return _run_from_dict(row["data"]) if row is not None else None

    def save_run(self, run: RunState) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO runs (run_id, tenant_id, data) VALUES (%s, %s, %s) "
                "ON CONFLICT (run_id) DO UPDATE SET tenant_id = EXCLUDED.tenant_id, data = EXCLUDED.data",
                (run.run_id, run.tenant_id, self._Jsonb(_run_to_dict(run))))

    def list_runs(self, tenant_id: str) -> list[RunState]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT data FROM runs WHERE tenant_id = %s "
                "ORDER BY (data->>'created_at')::double precision NULLS LAST, run_id",
                (tenant_id,)).fetchall()
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
    def record_usage(self, *, tenant_id: str, run_id: str, kind: str, cost_usd: float, detail: dict) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO usage_log (tenant_id, run_id, kind, cost_usd, detail) VALUES (%s, %s, %s, %s, %s)",
                (tenant_id, run_id, kind, float(cost_usd), self._Jsonb(dict(detail or {}))))

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
    def get_memory(self, tenant_id: str) -> str:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT content FROM user_memory WHERE tenant_id = %s",
                               (tenant_id,)).fetchone()
            return row["content"] if row is not None else ""

    def save_memory(self, tenant_id: str, content: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("INSERT INTO user_memory (tenant_id, content) VALUES (%s, %s) "
                         "ON CONFLICT (tenant_id) DO UPDATE SET content = EXCLUDED.content",
                         (tenant_id, content or ""))

    # --- learned-trust counters (per-user approval counts per action class) ---
    def get_trust(self, tenant_id: str) -> dict:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT data FROM trust_counts WHERE tenant_id = %s",
                               (tenant_id,)).fetchone()
            return dict(row["data"]) if row is not None else {}

    def save_trust(self, tenant_id: str, counts: dict) -> None:
        with self._pool.connection() as conn:
            conn.execute("INSERT INTO trust_counts (tenant_id, data) VALUES (%s, %s) "
                         "ON CONFLICT (tenant_id) DO UPDATE SET data = EXCLUDED.data",
                         (tenant_id, self._Jsonb(dict(counts or {}))))

    # --- persistent browser contexts (per (tenant, site) logged-in session profile) ---
    def get_browser_context(self, tenant_id: str, site: str) -> str | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT context_id FROM browser_contexts WHERE tenant_id = %s AND site = %s",
                               (tenant_id, site)).fetchone()
            return row["context_id"] if row is not None else None

    def save_browser_context(self, tenant_id: str, site: str, context_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("INSERT INTO browser_contexts (tenant_id, site, context_id) VALUES (%s, %s, %s) "
                         "ON CONFLICT (tenant_id, site) DO UPDATE SET context_id = EXCLUDED.context_id",
                         (tenant_id, site, context_id))

    def close(self) -> None:
        self._pool.close()

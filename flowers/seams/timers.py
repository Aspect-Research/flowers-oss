"""The durable timers seam — the wait/heartbeat primitive.

This is the antidote to "re-search instead of waiting." Instead of an agent busy-looping or
re-running a search because it can't wait for a reply, the scheduler *parks* a run by ``schedule``-ing
a durable timer, and a poller resumes runs whose timers are ``due``. Because timers are persisted, a
crashed/restarted process re-arms its parked runs simply by re-reading ``due``.

``LocalTimers`` backs timers with sqlite so they survive process restart, and exposes a *virtual clock*
(``advance``) so the offline test suite can fast-forward long waits without sleeping in real time.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time

from flowers.seams.interfaces import Timer
from flowers.types import new_id


class LocalTimers:
    """Sqlite-backed, virtual-clock implementation of :class:`DurableTimers`.

    Durability
        Every timer is committed to a sqlite table (``id``, ``run_id``, ``wake_at``, ``kind``,
        ``payload`` JSON, plus ``cancelled``/``fired`` flags). Pass a file path to the constructor
        for cross-restart durability; the default ``":memory:"`` is fine for unit tests that don't
        exercise persistence.

    Virtual clock
        ``now()`` is ``real_time + offset``. ``advance(seconds)`` bumps the offset so a 100-second
        wait becomes due instantly. The offset is process-local (it is the *test/dev* clock), not
        persisted — a fresh process starts at real time and re-arms persisted timers via ``due``.

    Due semantics (chosen + tested)
        ``due()`` returns the timers whose ``wake_at <= (at or now())`` that are neither cancelled
        nor already fired, marking each returned timer as ``fired`` so the same due-check never hands
        the poller a duplicate. The caller owns the resumed run and typically ``cancel``-s the timer
        once handled; re-scheduling (a fresh ``schedule``) is how a recurring monitor re-arms.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        # One connection shared by the tick-poller thread and the drive worker(s): sqlite3 forbids
        # concurrent use of a single connection, so every method serializes on a process-local lock.
        # WAL + busy_timeout make a second PROCESS on the same file wait briefly instead of raising
        # "database is locked".
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA busy_timeout = 5000")
        self._conn.execute("PRAGMA journal_mode = WAL")     # no-op ("memory") for :memory: dbs
        self._conn.execute("PRAGMA synchronous = NORMAL")   # safe under WAL; mutations still commit
        self._offset: float = 0.0
        self._ensure_schema()

    # --- schema ---------------------------------------------------------------

    def _ensure_schema(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS timers (
                    id        TEXT PRIMARY KEY,
                    run_id    TEXT NOT NULL,
                    wake_at   REAL NOT NULL,
                    kind      TEXT NOT NULL,
                    payload   TEXT NOT NULL DEFAULT '{}',
                    cancelled INTEGER NOT NULL DEFAULT 0,
                    fired     INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS ix_timers_run ON timers(run_id)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS ix_timers_wake ON timers(wake_at)")
            self._conn.commit()

    # --- DurableTimers protocol ----------------------------------------------

    def available(self) -> bool:
        """Always available: LocalTimers is the in-repo, network-free default."""
        return True

    def now(self) -> float:
        """The virtual clock: real wall-clock time plus the accumulated ``advance`` offset."""
        return time.time() + self._offset

    def schedule(
        self,
        *,
        run_id: str,
        wake_at: float,
        kind: str,
        payload: dict | None = None,
    ) -> Timer:
        """Persist a timer that should fire at ``wake_at`` (an absolute virtual-clock timestamp)."""
        timer = Timer(
            id=new_id("tmr"),
            run_id=run_id,
            wake_at=float(wake_at),
            kind=kind,
            payload=dict(payload or {}),
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO timers (id, run_id, wake_at, kind, payload) VALUES (?, ?, ?, ?, ?)",
                (timer.id, timer.run_id, timer.wake_at, timer.kind, json.dumps(timer.payload)),
            )
            self._conn.commit()
        return timer

    def due(self, *, at: float | None = None) -> list[Timer]:
        """Return + mark-fired the timers due at ``at`` (defaults to ``now()``).

        Only live (not cancelled, not already fired) timers are returned; each is flagged ``fired``
        so a subsequent due-check never returns it twice. A recurring kind re-arms by ``schedule``-ing
        a new timer.
        """
        cutoff = self.now() if at is None else float(at)
        # SELECT + mark-fired as ONE locked section: two concurrent due() callers (e.g. overlapping
        # tick threads) must never both claim the same timer.
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM timers WHERE wake_at <= ? AND cancelled = 0 AND fired = 0 "
                "ORDER BY wake_at ASC, id ASC",
                (cutoff,),
            ).fetchall()
            timers = [self._row_to_timer(r) for r in rows]
            if timers:
                self._conn.executemany(
                    "UPDATE timers SET fired = 1 WHERE id = ?",
                    [(t.id,) for t in timers],
                )
                self._conn.commit()
        return timers

    def cancel(self, timer_id: str) -> None:
        """Cancel a single timer so it can never become ``due``."""
        with self._lock:
            self._conn.execute("UPDATE timers SET cancelled = 1 WHERE id = ?", (timer_id,))
            self._conn.commit()

    def cancel_for_run(self, run_id: str) -> None:
        """Cancel every timer parked for ``run_id`` (e.g. the run finished or was stopped)."""
        with self._lock:
            self._conn.execute("UPDATE timers SET cancelled = 1 WHERE run_id = ?", (run_id,))
            self._conn.commit()

    def advance(self, seconds: float) -> None:
        """Fast-forward the virtual clock by ``seconds`` (dev/test only — no real sleeping)."""
        self._offset += float(seconds)

    # --- helpers --------------------------------------------------------------

    @staticmethod
    def _row_to_timer(row: sqlite3.Row) -> Timer:
        return Timer(
            id=row["id"],
            run_id=row["run_id"],
            wake_at=row["wake_at"],
            kind=row["kind"],
            payload=json.loads(row["payload"] or "{}"),
        )

    def close(self) -> None:
        """Close the underlying sqlite connection (file dbs persist on disk regardless)."""
        with self._lock:
            self._conn.close()

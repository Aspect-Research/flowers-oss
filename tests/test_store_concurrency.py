"""The store/timers under concurrent threads — the live serving topology the in-request suite misses.

A served app touches ONE SqliteStore connection from at least three threads at once (the SSE loop
polling get_run, the background drive worker writing, the tick poller), and sqlite3 forbids concurrent
use of a single connection. These tests hammer that topology directly; before the store lock landed
they failed with "Recursive use of cursors" / "database is locked" — exactly the intermittent live
500s and dropped SSE streams.
"""

from __future__ import annotations

import threading

from flowers.seams.store import SqliteStore
from flowers.seams.timers import LocalTimers
from flowers.types import EffectRecord, RunState, RunStatus


def _run(rid: str) -> RunState:
    return RunState(run_id=rid, tenant_id="local", goal_text="g", budget_usd=1.0,
                    status=RunStatus.RUNNING)


def test_store_survives_concurrent_threads(tmp_path):
    store = SqliteStore(str(tmp_path / "conc.db"))
    rids = [f"run_{i}" for i in range(4)]
    for rid in rids:
        store.create_run(_run(rid))
    errors: list[BaseException] = []
    per_thread_effects = 50

    def worker(i: int):
        rid = rids[i % len(rids)]
        try:
            for _ in range(per_thread_effects):
                store.save_run(_run(rid))
                store.get_run(rid)
                store.append_effect(rid, EffectRecord(toolkit="t", action="a"))
                store.record_usage(tenant_id="local", run_id=rid, kind="model",
                                   cost_usd=0.001, detail={})
                store.run_spend(rid)
                store.running_runs()
        except BaseException as e:  # noqa: BLE001 — collect ANY failure for the assertion
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"concurrent access failed: {errors[:3]}"
    # No lost writes either: every append landed exactly once.
    total = sum(len(store.get_effects(rid)) for rid in rids)
    assert total == 8 * per_thread_effects


def test_event_ids_are_gapless_under_concurrent_appends(tmp_path):
    # append_event assigns eids under the store lock — concurrent emitters (drive thread + tick
    # thread) must produce a strictly increasing, gapless 1..N sequence (the SSE resume cursor).
    store = SqliteStore(str(tmp_path / "ev.db"))
    n_threads, per_thread = 8, 25

    def emitter():
        for _ in range(per_thread):
            store.append_event("run_x", {"kind": "progress", "text": "tick"})

    threads = [threading.Thread(target=emitter) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    ids = [e["id"] for e in store.get_events("run_x")]
    assert ids == list(range(1, n_threads * per_thread + 1))


def test_get_events_after_cursor(tmp_path):
    store = SqliteStore(str(tmp_path / "cur.db"))
    for i in range(5):
        store.append_event("r1", {"kind": "progress", "text": f"e{i}"})
    tail = store.get_events("r1", after=3)
    assert [e["text"] for e in tail] == ["e3", "e4"]
    assert [e["id"] for e in tail] == [4, 5]
    assert store.get_events("r1", after=5) == []


def test_notes_queue_take_consumes_once():
    store = SqliteStore()
    store.add_note("r1", "also check Tuesday")
    store.add_note("r1", "budget is $50")
    store.add_note("r2", "other run's note")
    assert store.take_notes("r1") == ["also check Tuesday", "budget is $50"]
    assert store.take_notes("r1") == []          # consumed exactly once
    assert store.take_notes("r2") == ["other run's note"]


def test_timers_due_claims_each_timer_once_under_contention(tmp_path):
    timers = LocalTimers(str(tmp_path / "t.db"))
    now = timers.now()
    scheduled = [timers.schedule(run_id=f"r{i}", wake_at=now - 1, kind="poll") for i in range(200)]
    claimed: list[str] = []
    lock = threading.Lock()

    def poller():
        got = timers.due()
        with lock:
            claimed.extend(t.id for t in got)

    threads = [threading.Thread(target=poller) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Every timer claimed, none claimed twice (due()'s SELECT+mark-fired is one locked section).
    assert sorted(claimed) == sorted(t.id for t in scheduled)


def test_wal_and_busy_timeout_applied(tmp_path):
    store = SqliteStore(str(tmp_path / "wal.db"))
    with store._locked() as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    timers = LocalTimers(str(tmp_path / "walt.db"))
    with timers._lock:
        assert timers._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

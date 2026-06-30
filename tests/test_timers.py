"""LocalTimers — the durable wait/heartbeat seam, exercised entirely offline.

Covers: the virtual clock + ``due`` semantics, ``cancel`` / ``cancel_for_run``, and — the
load-bearing property — persistence across a simulated process restart (reopen the same file db and
re-arm the parked timer). No network, no real sleeping.
"""

from __future__ import annotations

import os

from flowers.seams.interfaces import DurableTimers, Timer
from flowers.seams.timers import LocalTimers


def test_conforms_to_protocol_and_is_available():
    t = LocalTimers()
    assert isinstance(t, DurableTimers)   # structural Protocol conformance
    assert t.available() is True


def test_schedule_then_due_after_advance():
    t = LocalTimers()
    wake_at = t.now() + 100.0
    timer = t.schedule(run_id="run_a", wake_at=wake_at, kind="await_replies", payload={"k": 3})
    assert isinstance(timer, Timer)
    assert timer.run_id == "run_a" and timer.kind == "await_replies" and timer.payload == {"k": 3}

    # Not yet due: the wait window hasn't elapsed on the virtual clock.
    assert t.due() == []

    # Fast-forward past the wake time without sleeping; now it's due exactly once.
    t.advance(101)
    due = t.due()
    assert [d.id for d in due] == [timer.id]
    assert due[0].payload == {"k": 3}

    # Due semantics: a fired timer is not returned again on the next due-check.
    assert t.due() == []


def test_due_respects_explicit_at():
    t = LocalTimers()
    base = t.now()
    timer = t.schedule(run_id="run_b", wake_at=base + 50.0, kind="monitor")
    assert t.due(at=base + 49.0) == []
    assert [d.id for d in t.due(at=base + 50.0)] == [timer.id]


def test_cancel_removes_from_due():
    t = LocalTimers()
    timer = t.schedule(run_id="run_c", wake_at=t.now() + 10.0, kind="clarify")
    t.cancel(timer.id)
    t.advance(20)
    assert t.due() == []


def test_cancel_for_run_clears_all_run_timers():
    t = LocalTimers()
    t.schedule(run_id="run_d", wake_at=t.now() + 10.0, kind="monitor")
    t.schedule(run_id="run_d", wake_at=t.now() + 20.0, kind="await_replies")
    keep = t.schedule(run_id="run_e", wake_at=t.now() + 10.0, kind="monitor")

    t.cancel_for_run("run_d")
    t.advance(100)
    due = t.due()
    # Only the other run's timer survives.
    assert [d.id for d in due] == [keep.id]


def test_persistence_across_reopen(tmp_path):
    db = os.path.join(str(tmp_path), "timers.db")

    first = LocalTimers(db)
    wake_at = first.now() + 100.0
    timer = first.schedule(run_id="run_f", wake_at=wake_at, kind="await_replies", payload={"n": 1})
    first.close()   # simulate process exit

    # Fresh process: reopen the SAME db path and re-arm from persisted state.
    second = LocalTimers(db)
    # Before the wake time it isn't due...
    assert second.due(at=wake_at - 1.0) == []
    # ...and at/after the wake time the persisted timer is still there.
    due = second.due(at=wake_at)
    assert [d.id for d in due] == [timer.id]
    assert due[0].run_id == "run_f" and due[0].payload == {"n": 1}
    second.close()

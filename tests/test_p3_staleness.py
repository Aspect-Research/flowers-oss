"""Box-observation staleness wired into the gate.

A baseline read-file that drifts EXTERNALLY (not via the agent's own write) makes a claimed-done a
stale read -> the gate refuses. The agent's OWN edits are not drift. An empty baseline is a no-op
(so research/file-less steps are unaffected — the whole offline suite stays green).
"""

from __future__ import annotations

from _harness import build, make_brain

from flowers import trustgate as g
from flowers.types import PlanStep, StepResult


def _operator():
    return build(model=make_brain(steps=[{"text": "noop"}]))["op"]


def test_external_drift_of_a_read_file_is_flagged_stale():
    op = _operator()
    sandbox = op._sandbox("run-stale")
    sandbox.write_file("data.txt", "v1")
    baseline = g.snapshot_dir(sandbox.workdir())          # the read-set BEFORE the step
    step = PlanStep(index=0, text="use data", params={"_box_baseline": baseline})

    sandbox.write_file("data.txt", "v2-changed-externally")   # drifts underneath the agent
    res = StepResult(claimed_done=True, ok=True, text="done", events=[])
    assert op._stale_files(step, sandbox, res) == ["data.txt"]

    # the gate refuses a completion claimed over a stale read
    accept, reason = g.gate_verdict(claimed_done=True, ok=True, stale_files=["data.txt"],
                                    gate_breaking=[], unverified_external=[], unverifiable_external=[])
    assert accept is False and "data.txt" in reason


def test_the_agents_own_write_is_not_treated_as_drift():
    op = _operator()
    sandbox = op._sandbox("run-selfwrite")
    sandbox.write_file("data.txt", "v1")
    baseline = g.snapshot_dir(sandbox.workdir())
    step = PlanStep(index=0, text="edit data", params={"_box_baseline": baseline})

    sandbox.write_file("data.txt", "v2-edited-by-agent")
    res = StepResult(claimed_done=True, ok=True, text="done",
                     events=[{"kind": "write", "path": "data.txt", "ok": True}])
    assert op._stale_files(step, sandbox, res) == []      # intended edit, not stale


def test_noncanonical_write_path_is_not_a_false_positive():
    # the agent writing via a valid-but-non-canonical path ('./out.txt') must NOT be flagged as drift
    op = _operator()
    sandbox = op._sandbox("run-noncanon")
    sandbox.write_file("out.txt", "v1")
    baseline = g.snapshot_dir(sandbox.workdir())
    step = PlanStep(index=0, text="edit out", params={"_box_baseline": baseline})
    sandbox.write_file("out.txt", "v2-edited")
    res = StepResult(claimed_done=True, ok=True, text="done",
                     events=[{"kind": "write", "path": "./out.txt", "ok": True}])   # non-canonical path
    assert op._stale_files(step, sandbox, res) == []


def test_empty_baseline_is_a_noop():
    op = _operator()
    sandbox = op._sandbox("run-empty")
    step = PlanStep(index=0, text="research only", params={})   # no _box_baseline
    res = StepResult(claimed_done=True, ok=True, text="done", events=[])
    assert op._stale_files(step, sandbox, res) == []

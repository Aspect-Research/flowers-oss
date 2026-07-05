"""Regression locks for the 2026-06-22 independent-audit CODE fixes.

These prove the trust-path gaps the audit found are actually closed through the REAL production path
(operator + gate + broker over scripted seams), not just in isolation:

  * the criterion-conditional fabrication hole (a no-evidence claimed-done now refused);
  * gate_breaking wired to a real census (unsupported-completion / forgot-own-edit fire);
  * source_membership made functional (fetched_urls plumbed into the bundle);
  * fingerprint-less side-effects route to the owner (in test_trustgate.py);
  * the policy / auth / search / web fixes (in their own modules' tests).
"""

from __future__ import annotations

import json

from _harness import build, make_brain, tc

from flowers.seams.interfaces import ModelResponse
from flowers.seams.model import FakeModel
from flowers.types import Goal, RunState, RunStatus, ToolCall


def _brain(steps, executor_fn):
    """A FakeModel whose clarifier/planner are scripted and whose EXECUTOR turn is fully custom."""
    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            return ModelResponse(content=json.dumps({"steps": steps}))
        return executor_fn(messages)
    return FakeModel(on_complete=fn)


# --------------------------------------------------------------- fabrication hole (HIGH)

def test_empty_completion_with_no_evidence_is_refused():
    # The hole: the model ends its turn with NO tool calls and NO content; the executor reads that as
    # 'done'. The gate now REFUSES it (unsupported-completion) — nothing in the record supports the claim
    # (no objective criterion, no verified effect, no file produced, no deliverable text).
    brain = _brain([{"text": "do the thing"}],
                   lambda msgs: ModelResponse(content="", finish_reason="stop"))
    h = build(model=brain)
    run = h["op"].start(Goal(text="a goal"))
    assert run.status is RunStatus.ESCALATED


def test_blank_finish_summary_is_refused():
    # Same hole via an explicit finish(completed=true) carrying an EMPTY summary + no other evidence.
    brain = _brain([{"text": "do the thing"}],
                   lambda msgs: ModelResponse(
                       tool_calls=[ToolCall(name="finish", args={"completed": True, "summary": "   "})],
                       finish_reason="tool_calls"))
    h = build(model=brain)
    run = h["op"].start(Goal(text="a goal"))
    assert run.status is RunStatus.ESCALATED


def test_real_deliverable_is_still_accepted():
    # Control: a non-empty deliverable IS its own evidence for a synthesis step -> DONE. The fix must not
    # refuse legitimate no-side-effect work.
    brain = _brain([{"text": "summarize"}],
                   lambda msgs: ModelResponse(
                       tool_calls=[ToolCall(name="finish", args={
                           "completed": True, "summary": "Here is the full writeup with all the details."})],
                       finish_reason="tool_calls"))
    h = build(model=brain)
    run = h["op"].start(Goal(text="a goal"))
    assert run.status is RunStatus.DONE


def test_file_producing_step_is_supported():
    # A step that produced a FILE is supported even with a terse summary -> DONE (not unsupported).
    brain = make_brain(steps=[{"text": "write a file"}],
                       actions={"write a file": [tc("write_file", path="out.txt", content="real output")]})
    h = build(model=brain)
    run = h["op"].start(Goal(text="g"))
    assert run.status is RunStatus.DONE


# --------------------------------------------------------------- gate_breaking census wired (MEDIUM)

def test_identical_redo_is_refused_forgot_own_edit():
    # gate_breaking is no longer hardcoded []: an IDENTICAL-content redo of the same file is an
    # unproductive loop the gate now catches (forgot-own-edit, confirmed by has_identical_redo over the
    # executor's hashed write events).
    brain = make_brain(steps=[{"text": "edit the file"}],
                       actions={"edit the file": [tc("write_file", path="out.txt", content="SAME"),
                                                  tc("write_file", path="out.txt", content="SAME")]})
    h = build(model=brain)
    run = h["op"].start(Goal(text="g"))
    assert run.status is RunStatus.ESCALATED


# --------------------------------------------------------------- source_membership plumbed (MEDIUM)

_URL = "https://example.com/article"
_SRC_CRIT = [{"id": "src",
              "objective_check": {"kind": "source_membership", "params": {"deliverable": "report.md"}}}]


def test_deliverable_citing_an_unfetched_source_is_refused():
    # source_membership is now FUNCTIONAL (fetched_urls plumbed into the bundle): a deliverable that cites
    # a URL the run never fetched through the proxy is refused (anti-citation-fabrication). Previously the
    # check was inert because fetched_urls was hardcoded [].
    brain = make_brain(
        steps=[{"text": "write report", "done_criteria": _SRC_CRIT}],
        actions={"write report": [tc("write_file", path="report.md", content=f"See {_URL} for details.")]})
    h = build(model=brain)
    run = h["op"].start(Goal(text="g"))
    assert run.status is RunStatus.ESCALATED


def test_deliverable_citing_a_fetched_source_is_accepted():
    # Fetch the source FIRST, then cite it -> source_membership MET -> DONE (the fetched-URL log reaches
    # the gate's bundle).
    brain = make_brain(
        steps=[{"text": "write report", "done_criteria": _SRC_CRIT}],
        actions={"write report": [tc("web_fetch", url=_URL),
                                  tc("write_file", path="report.md", content=f"See {_URL} for details.")]})
    h = build(model=brain)
    run = h["op"].start(Goal(text="g"))
    assert run.status is RunStatus.DONE


def test_run_shell_tool_executes_and_completes():
    # run_shell now has a REAL production call site (sandbox.run, previously dormant): a successful
    # command + finish reaches DONE.
    brain = make_brain(steps=[{"text": "compute it"}],
                       actions={"compute it": [tc("run_shell", command="echo hello")]})
    h = build(model=brain)
    run = h["op"].start(Goal(text="g"))
    assert run.status is RunStatus.DONE


def test_failed_command_then_claimed_done_is_refused():
    # The failed-retry floor is now LIVE (the executor emits run events): a command whose FINAL run failed
    # — here a dangerous command the sandbox floor refuses (ok=False, never executed) — cannot underlie a
    # claimed-done. The gate refuses and the run escalates.
    brain = make_brain(steps=[{"text": "do compute"}],
                       actions={"do compute": [tc("run_shell", command="rm -rf /")]})
    h = build(model=brain)
    run = h["op"].start(Goal(text="g"))
    assert run.status is RunStatus.ESCALATED


def test_fetched_urls_survive_a_continuation_restart():
    # Review fix: the per-run fetched-URL set is part of the durable continuation, so a citing step that
    # resumes in a FRESH process after a park still sees URLs an earlier step fetched (otherwise
    # source_membership would falsely refuse a legitimate deliverable across a restart).
    h = build(model=make_brain())
    op, store = h["op"], h["store"]
    store.create_run(RunState(run_id="r1", goal_text="g", budget_usd=1.0,
                              status=RunStatus.RUNNING))
    op._fetched["r1"] = {"https://example.com/a"}
    op._grants["r1"] = {"gmail:GMAIL_SEND_EMAIL|deadbeef"}     # a parked run with a grant
    op._persist_continuation("r1")
    # simulate a fresh process: drop every hot cache
    for cache in (op._fetched, op._grants, op._pending_grant, op._resume_state):
        cache.clear()
    op._load_continuation("r1")
    assert op._fetched.get("r1") == {"https://example.com/a"}   # rehydrated from the Store
    assert op._grants.get("r1") == {"gmail:GMAIL_SEND_EMAIL|deadbeef"}

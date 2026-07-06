"""The live failure modes the in-request suite couldn't see — reproduced offline.

Each test here is a regression for a way the served app used to break in REAL use while every
in-request test stayed green: a crashed drive leaving a run silently stuck RUNNING, the in-memory
event log blanking the dashboard on restart, SSE reconnects dropping events, mid-run chat messages
vanishing, and escalations being dead ends.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time

from _harness import make_brain
from starlette.testclient import TestClient

from flowers.channels.web import WebChannel, create_app
from flowers.controlplane import ControlPlane
from flowers.engine.operator import Operator
from flowers.seams.integrations import FakeIntegrations
from flowers.seams.interfaces import ModelResponse
from flowers.seams.model import FakeModel
from flowers.seams.search import FakeSearch
from flowers.seams.store import SqliteStore
from flowers.seams.timers import LocalTimers
from flowers.types import Goal, Plan, PlanStep, RunStatus, ToolCall


@contextlib.contextmanager
def _wire(model, *, store=None, **app_kw):
    """Yield (client, store, cp) with the TestClient ENTERED: background drive tasks need the
    client's persistent portal loop — an un-entered TestClient runs them on a per-request portal
    whose shutdown races the task (exactly the class of lifecycle bug this file exists to catch)."""
    store = store if store is not None else SqliteStore()
    ch = WebChannel(store)
    op = Operator(store=store, model=model, search=FakeSearch(),
                  integrations=FakeIntegrations(), timers=LocalTimers(), channel=ch)
    cp = ControlPlane(store=store, operator=op)
    with TestClient(create_app(cp, ch, **app_kw)) as client:
        yield client, store, cp


def _settle(client, rid, *, want, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get(f"/api/runs/{rid}").json().get("status")
        if st == want:
            return st
        time.sleep(0.02)
    return client.get(f"/api/runs/{rid}").json().get("status")


def _events(client, rid, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(f"/api/runs/{rid}/events" + (f"?{q}" if q else "")).json()["events"]


# --------------------------------------------------------------------- silent death

def test_crashing_model_yields_escalated_not_stuck_running():
    # The owner's main live symptom: a model that RAISES mid-run (no key -> exhausted FakeModel,
    # transport bug) used to escape the executor, get swallowed by _spawn, and leave the run
    # RUNNING forever with a spinner that never cleared. It must surface as an honest escalation.
    def fn(messages, tools, role):
        if role == "planner" and "intake step" in messages[0]["content"]:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            return ModelResponse(content=json.dumps({"steps": [{"text": "do the thing"}]}))
        raise RuntimeError("script exhausted")   # the executor call blows up

    with _wire(FakeModel(on_complete=fn)) as (client, _store, _cp):
        rid = client.post("/api/goal", json={"text": "do the thing"}).json()["run_id"]
        assert _settle(client, rid, want="escalated") == "escalated"
        esc = [e for e in _events(client, rid) if e["kind"] == "escalated"]
        assert esc and esc[-1]["reason_code"] == "model_error"
        assert "model call failed" in esc[-1]["text"]


def test_no_model_key_returns_actionable_503():
    # With no usable model, POST /api/goal fails FAST with the fix instruction — it never creates
    # a run that would die mid-drive.
    with _wire(FakeModel([]), degraded="no model is configured — set OPENROUTER_API_KEY") as (client, store, _cp):
        r = client.post("/api/goal", json={"text": "anything"})
        assert r.status_code == 503
        body = r.json()
        assert body["reason_code"] == "model_unavailable" and "OPENROUTER_API_KEY" in body["error"]
        assert store.running_runs() == []


def test_spawn_failure_emits_terminal_event(monkeypatch):
    # Even an exception OUTSIDE the operator (a bug in the drive plumbing itself) must leave the
    # run honestly ESCALATED with a visible event — never silently RUNNING.
    with _wire(make_brain(steps=[{"text": "noop"}])) as (client, _store, cp):
        monkeypatch.setattr(cp, "drive", lambda run, goal: (_ for _ in ()).throw(RuntimeError("boom")))
        rid = client.post("/api/goal", json={"text": "noop"}).json()["run_id"]
        assert _settle(client, rid, want="escalated") == "escalated"
        esc = [e for e in _events(client, rid) if e["kind"] == "escalated"]
        assert esc and esc[-1]["reason_code"] == "internal_error"


# --------------------------------------------------------------------- durable event log / SSE

def test_events_survive_restart(tmp_path):
    # The event log lives in the store now: rebuild the whole app over the same file (the restart
    # simulation) and the pre-restart timeline must still replay, ids intact.
    db = str(tmp_path / "f.db")
    brain = make_brain(steps=[{"text": "write a brief"}],
                       actions={"write a brief": [ToolCall(name="write_file",
                                                           args={"path": "b.md", "content": "x"})]})
    with _wire(brain, store=SqliteStore(db)) as (client, _store, _cp):
        rid = client.post("/api/goal", json={"text": "write a brief"}).json()["run_id"]
        assert _settle(client, rid, want="done") == "done"
        before = _events(client, rid)
    assert len(before) >= 2

    # "Restart": the first app is fully torn down, a fresh one opens the same file.
    with _wire(make_brain(), store=SqliteStore(db)) as (client2, _s2, _cp2):
        after = _events(client2, rid)
        assert [e["id"] for e in after] == [e["id"] for e in before]
        with client2.stream("GET", f"/events/{rid}?replay_only=1") as r:
            body = "".join(r.iter_text())
        assert "event: plan_announce" in body and "event: done" in body


def test_sse_events_carry_monotonic_ids():
    brain = make_brain(steps=[{"text": "noop"}])
    with _wire(brain) as (client, _store, _cp):
        rid = client.post("/api/goal", json={"text": "noop"}).json()["run_id"]
        _settle(client, rid, want="done")
        with client.stream("GET", f"/events/{rid}?replay_only=1") as r:
            body = "".join(r.iter_text())
        ids = [int(line.split(":", 1)[1]) for line in body.splitlines() if line.startswith("id:")]
        assert ids == sorted(ids) and ids[0] == 1 and len(ids) >= 2


def test_sse_resumes_from_cursor():
    # Both resume forms — the Last-Event-ID header (EventSource auto-reconnect) and ?after= (manual
    # reconnect) — must skip already-delivered events and never drop the rest.
    brain = make_brain(steps=[{"text": "noop"}])
    with _wire(brain) as (client, _store, _cp):
        rid = client.post("/api/goal", json={"text": "noop"}).json()["run_id"]
        _settle(client, rid, want="done")
        all_ids = [e["id"] for e in _events(client, rid)]
        mid = all_ids[len(all_ids) // 2]
        for kwargs in ({"headers": {"Last-Event-ID": str(mid)}}, {}):
            url = f"/events/{rid}?replay_only=1" + ("" if kwargs else f"&after={mid}")
            with client.stream("GET", url, **kwargs) as r:
                body = "".join(r.iter_text())
            got = [int(line.split(":", 1)[1]) for line in body.splitlines() if line.startswith("id:")]
            assert got == [i for i in all_ids if i > mid]


def test_sse_keepalive_during_quiet_run():
    # A parked run emits nothing for minutes; the stream must stay open and visibly alive
    # (comment lines), not hit a connection ceiling. The stream is read to natural completion
    # (a background approval drives the run to done, which closes it) — TestClient cannot
    # abandon a stream mid-read without deadlocking on the portal.
    brain = make_brain(steps=[{"text": "email the venue"}],
                       actions={"email the venue": [ToolCall(name="send_email",
                                args={"to": "bob@acme.com", "subject": "hi"})]})
    with _wire(brain, keepalive_seconds=0.05) as (client, _store, _cp):
        rid = client.post("/api/goal", json={"text": "email the venue"}).json()["run_id"]
        assert _settle(client, rid, want="awaiting_approval") == "awaiting_approval"

        def approve_soon():
            time.sleep(0.4)   # long enough for several 0.05s keepalives to be emitted first
            client.post("/api/answer", json={"run_id": rid, "text": "yes"})

        t = threading.Thread(target=approve_soon)
        t.start()
        with client.stream("GET", f"/events/{rid}") as r:
            body = "".join(r.iter_text())   # ends when the run reaches done
        t.join()
        assert ": keepalive" in body and "event: done" in body


# --------------------------------------------------------------------- chat while running

def _wire_direct(model):
    """Operator/ControlPlane over a store-backed WebChannel, NO TestClient: these tests need the
    drive genuinely blocked in another thread while the answer path runs (TestClient completes
    background tasks before a response returns, so a run is never observably RUNNING through it)."""
    store = SqliteStore()
    ch = WebChannel(store)
    op = Operator(store=store, model=model, search=FakeSearch(),
                  integrations=FakeIntegrations(), timers=LocalTimers(), channel=ch)
    return ControlPlane(store=store, operator=op), store


def test_mid_run_message_is_acked_and_folded_into_next_step():
    # A message typed while the agent is mid-step used to vanish (resume() had no RUNNING branch)
    # and the UI spinner stuck forever. Now: an immediate ack event, and the note reaches the NEXT
    # step's executor prompt.
    gate, in_step1 = threading.Event(), threading.Event()
    seen_prompts: list[str] = []

    def fn(messages, tools, role):
        if role == "planner" and "intake step" in messages[0]["content"]:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            return ModelResponse(content=json.dumps(
                {"steps": [{"text": "research venues"}, {"text": "summarize findings"}]}))
        user = messages[1]["content"]
        seen_prompts.append(user)
        if "YOUR STEP (1)" in user and not gate.is_set():
            in_step1.set()
            gate.wait(5.0)   # hold step 1 open so the run is observably RUNNING
        return ModelResponse(tool_calls=[ToolCall(name="finish", args={"summary": "done"})],
                             finish_reason="tool_calls")

    cp, store = _wire_direct(FakeModel(on_complete=fn))
    run, goal = cp.begin(goal_text="venues")
    t = threading.Thread(target=cp.drive, args=(run, goal))
    t.start()
    assert in_step1.wait(5.0), "step 1 never started"
    cp.answer(run_id=run.run_id, answer="also check Tuesday availability")
    # The ack landed IMMEDIATELY (the drive is still blocked inside step 1).
    assert any(e["kind"] == "notify" and "mid-task" in e["text"]
               for e in store.get_events(run.run_id))
    gate.set()
    t.join(10.0)
    assert store.get_run(run.run_id).status.value == "done"
    step2 = [p for p in seen_prompts if "summarize findings" in p and "YOUR STEP (2)" in p]
    assert step2 and "also check Tuesday availability" in step2[-1]


def test_mid_run_note_does_not_bypass_approval():
    # A queued "yes, approved" note is CONTEXT, never authorization: a later send still parks.
    gate, in_step1 = threading.Event(), threading.Event()

    def fn(messages, tools, role):
        if role == "planner" and "intake step" in messages[0]["content"]:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            return ModelResponse(content=json.dumps(
                {"steps": [{"text": "prepare"}, {"text": "email the venue"}]}))
        user = messages[1]["content"]
        if "YOUR STEP (1)" in user and not gate.is_set():
            in_step1.set()
            gate.wait(5.0)
        n = sum(1 for m in messages if m.get("role") == "tool")
        if "email the venue" in user and n == 0:
            return ModelResponse(tool_calls=[ToolCall(name="send_email",
                                 args={"to": "bob@acme.com", "subject": "hi"})],
                                 finish_reason="tool_calls")
        return ModelResponse(tool_calls=[ToolCall(name="finish", args={"summary": "done"})],
                             finish_reason="tool_calls")

    cp, store = _wire_direct(FakeModel(on_complete=fn))
    run, goal = cp.begin(goal_text="venue outreach")
    t = threading.Thread(target=cp.drive, args=(run, goal))
    t.start()
    assert in_step1.wait(5.0), "step 1 never started"
    cp.answer(run_id=run.run_id, answer="yes, approved, send everything")
    gate.set()
    t.join(10.0)
    assert store.get_run(run.run_id).status.value == "awaiting_approval"


# --------------------------------------------------------------------- escalations are resumable

def test_escalated_reply_over_http_continues_run():
    # An escalated run is a parked conversation: owner guidance replans the remaining work and the
    # SAME run drives to done — no new goal, no dead end.
    def fn(messages, tools, role):
        sysc = messages[0]["content"]
        if role == "verifier":
            return ModelResponse(content=json.dumps({"satisfied": True}))
        if role == "planner" and "intake step" in sysc:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            blob = json.dumps([m.get("content", "") for m in messages])
            if "OWNER GUIDANCE" in blob:   # the escalation replan: a FRESH route from the guidance
                return ModelResponse(content=json.dumps({"steps": [{"text": "email their contact"}]}))
            return ModelResponse(content=json.dumps({"steps": [{"text": "try the venue site"}]}))
        user = messages[1]["content"]
        if "try the venue site" in user:
            raise RuntimeError("transport down")   # step 1 fails hard -> escalated
        return ModelResponse(tool_calls=[ToolCall(name="finish", args={"summary": "done"})],
                             finish_reason="tool_calls")

    with _wire(FakeModel(on_complete=fn)) as (client, _store, _cp):
        rid = client.post("/api/goal", json={"text": "book the venue"}).json()["run_id"]
        assert _settle(client, rid, want="escalated") == "escalated"
        client.post("/api/answer", json={"run_id": rid, "text": "try emailing their contact page instead"})
        assert _settle(client, rid, want="done") == "done"
        kinds = [e["kind"] for e in _events(client, rid)]
        assert kinds.count("escalated") == 1 and kinds[-1] == "done"


def test_escalated_reply_no_stops_the_run():
    def fn(messages, tools, role):
        if role == "planner" and "intake step" in messages[0]["content"]:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            return ModelResponse(content=json.dumps({"steps": [{"text": "do it"}]}))
        raise RuntimeError("down")

    with _wire(FakeModel(on_complete=fn)) as (client, _store, _cp):
        rid = client.post("/api/goal", json={"text": "x"}).json()["run_id"]
        assert _settle(client, rid, want="escalated") == "escalated"
        client.post("/api/answer", json={"run_id": rid, "text": "no"})
        assert _settle(client, rid, want="stopped") == "stopped"
        assert any(e["kind"] == "notify" and "leaving it here" in e["text"]
                   for e in _events(client, rid))


def test_activity_heartbeats_animate_the_timeline():
    # The broker emits a pre-call heartbeat for every provider call, so the dashboard timeline
    # moves DURING a long model/search call instead of freezing until the step settles.
    brain = make_brain(steps=[{"text": "research venues"}],
                       actions={"research venues": [ToolCall(name="web_search",
                                                             args={"query": "florists open sunday"})]})
    cp, store = _wire_direct(brain)
    run, goal = cp.begin(goal_text="find florists")
    cp.drive(run, goal)
    texts = [e["text"] for e in store.get_events(run.run_id) if e["kind"] == "progress"]
    assert any("thinking" in t for t in texts)
    assert any("searching: florists open sunday" in t for t in texts)


def test_escalated_resume_without_headroom_stays_parked():
    # Owner guidance can't override the budget terminator: continuing an escalated run with the
    # budget spent gets an honest refusal and the run stays parked.
    def fn(messages, tools, role):
        if role == "planner" and "intake step" in messages[0]["content"]:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            return ModelResponse(content=json.dumps({"steps": [{"text": "do it"}]}))
        raise RuntimeError("down")

    cp, store = _wire_direct(FakeModel(on_complete=fn))
    run, goal = cp.begin(goal_text="x", budget_usd=0.50)
    cp.drive(run, goal)
    assert store.get_run(run.run_id).status.value == "escalated"
    store.record_usage(run_id=run.run_id, kind="model",
                       cost_usd=1.00, detail={})   # the budget is now spent
    got = cp.answer(run_id=run.run_id, answer="keep going, try harder")
    assert got.status.value == "escalated"
    assert any(e["kind"] == "notify" and "can't continue" in e["text"]
               for e in store.get_events(run.run_id))


def test_recovery_emits_into_durable_log(tmp_path):
    # A crash orphan (a run left RUNNING by a dead process) is re-driven at startup — its recovery
    # output must land in the durable event log, where a reconnecting dashboard actually sees it
    # (it used to go into a fresh empty in-memory channel nobody was watching).
    db = str(tmp_path / "crash.db")
    brain = make_brain(steps=[{"text": "noop"}])
    store = SqliteStore(db)
    ch = WebChannel(store)
    op = Operator(store=store, model=brain, search=FakeSearch(),
                  integrations=FakeIntegrations(), timers=LocalTimers(), channel=ch)
    cp = ControlPlane(store=store, operator=op)
    run = op.begin(Goal(text="noop"))
    run.status = RunStatus.RUNNING       # what a process crash mid-drive leaves behind
    store.save_run(run)
    store.save_plan(run.run_id, Plan(steps=[PlanStep(index=0, text="noop")], goal_text="noop"))

    with TestClient(create_app(cp, ch, poll_interval=0.01)) as client:
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if client.get(f"/api/runs/{run.run_id}").json().get("status") == "done":
                break
            time.sleep(0.02)
        evs = client.get(f"/api/runs/{run.run_id}/events").json()["events"]
    texts = [e["text"] for e in evs]
    assert any("recovering after a restart" in t for t in texts)
    assert any(e["kind"] == "done" for e in evs)


# --------------------------------------------------------------------- request hygiene

def test_run_status_reports_live_spend():
    # GET /api/runs/{id} must sum the usage ledger, not echo the persisted RunState field — that
    # field only refreshes at settle points, so it reads $0.00 through a long in-flight step
    # (caught in live verification: a 10-minute run showed spent_usd 0.0 the whole way).
    brain = make_brain(steps=[{"text": "noop"}])
    with _wire(brain) as (client, store, _cp):
        rid = client.post("/api/goal", json={"text": "noop"}).json()["run_id"]
        _settle(client, rid, want="done")
        store.record_usage(run_id=rid, kind="model", cost_usd=0.42, detail={})
        assert abs(client.get(f"/api/runs/{rid}").json()["spent_usd"] - 0.42) < 1e-9


def test_list_runs_endpoint_surfaces_open_runs_first():
    # GET /api/runs is how a FRESH browser (no localStorage) finds the conversation to reattach
    # to — runs started via curl or another client must be discoverable server-side.
    brain = make_brain(steps=[{"text": "email the venue"}],
                       actions={"email the venue": [ToolCall(name="send_email",
                                args={"to": "bob@acme.com", "subject": "hi"})]})
    with _wire(brain) as (client, _store, _cp):
        rid = client.post("/api/goal", json={"text": "email the venue"}).json()["run_id"]
        assert _settle(client, rid, want="awaiting_approval") == "awaiting_approval"
        runs = client.get("/api/runs").json()["runs"]
        assert runs and runs[0]["run_id"] == rid
        assert runs[0]["status"] == "awaiting_approval" and "email the venue" in runs[0]["goal"]


def test_post_answer_missing_run_id_is_400():
    with _wire(make_brain()) as (client, _store, _cp):
        assert client.post("/api/answer", json={"text": "yes"}).status_code == 400
        assert client.post("/api/answer", json={"run_id": "", "text": "y"}).status_code == 400


def test_non_finite_budget_is_rejected():
    # NaN/Infinity budget would silently disable the dollar ceiling (spent > NaN is always False),
    # letting a run spend without bound. It must be a 400, not an accepted goal.
    with _wire(make_brain(steps=[{"text": "noop"}])) as (client, store, _cp):
        for bad in ("NaN", "Infinity", -1):
            r = client.post("/api/goal", json={"text": "x", "budget": bad})
            assert r.status_code == 400, f"budget {bad!r} was not rejected"
        assert store.running_runs() == []


def test_malformed_bodies_are_400_not_500():
    with _wire(make_brain()) as (client, _store, _cp):
        r1 = client.post("/api/goal", content=b"{not json", headers={"content-type": "application/json"})
        assert r1.status_code == 400
        # a non-string reply must not crash the worker (coerced to str)
        r2 = client.post("/api/answer", json={"run_id": "nope", "text": {"a": 1}})
        assert r2.status_code == 404   # run not found, but NOT a 500 from a type crash


def test_terminal_done_event_is_never_lost_to_the_close_race():
    # The SSE stream closes when the run reaches done/stopped; the final 'done' event (the run's
    # result) must always be flushed before close, even if it lands between the batch fetch and the
    # status read. A full replay stream must therefore always end with the done event.
    brain = make_brain(steps=[{"text": "noop"}])
    with _wire(brain) as (client, _store, _cp):
        rid = client.post("/api/goal", json={"text": "noop"}).json()["run_id"]
        _settle(client, rid, want="done")
        with client.stream("GET", f"/events/{rid}?replay_only=1") as r:
            body = "".join(r.iter_text())
        assert "event: done" in body


def test_escalation_guidance_starting_with_no_is_not_a_stop():
    # Found live: "No email needed, just finish" tripped the yes/no parser's "no " prefix and
    # STOPPED the run instead of steering it. In the escalation context, only a bare decline
    # (<= 3 words) stops; substantive guidance replans — which is safe, it authorizes nothing.
    def fn(messages, tools, role):
        if role == "planner" and "intake step" in messages[0]["content"]:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            blob = json.dumps([m.get("content", "") for m in messages])
            if "OWNER GUIDANCE" in blob:
                return ModelResponse(content=json.dumps({"steps": [{"text": "wrap up the summary"}]}))
            return ModelResponse(content=json.dumps({"steps": [{"text": "do the thing"}]}))
        user = messages[1]["content"]
        if "do the thing" in user:
            raise RuntimeError("down")   # step 1 fails -> escalated
        return ModelResponse(tool_calls=[ToolCall(name="finish", args={"summary": "done"})],
                             finish_reason="tool_calls")

    cp, store = _wire_direct(FakeModel(on_complete=fn))
    run, goal = cp.begin(goal_text="x")
    cp.drive(run, goal)
    assert store.get_run(run.run_id).status.value == "escalated"
    got = cp.answer(run_id=run.run_id, answer="No email needed, just finish the summary")
    assert got.status.value == "done"    # guidance, not a stop

    # a bare decline still stops
    run2, goal2 = cp.begin(goal_text="x")
    cp.drive(run2, goal2)
    assert store.get_run(run2.run_id).status.value == "escalated"
    assert cp.answer(run_id=run2.run_id, answer="no").status.value == "stopped"


def test_concurrent_tick_and_answer_do_not_double_drive_a_parked_run():
    # The per-run drive lock: a due timer (tick -> resume) and an owner answer (resume) hitting the
    # SAME parked run at the same instant must not both drive it. We count how many times the parked
    # AWAITING_APPROVAL run's approved action is executed — it must be exactly one, never two.
    import threading as _t
    sends = []
    lock = _t.Lock()

    def fn(messages, tools, role):
        if role == "planner" and "intake step" in messages[0]["content"]:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            return ModelResponse(content=json.dumps({"steps": [{"text": "email the venue"}]}))
        user = messages[1]["content"]
        n = sum(1 for m in messages if m.get("role") == "tool")
        if "email the venue" in user and n == 0:
            with lock:
                sends.append(1)
            return ModelResponse(tool_calls=[ToolCall(name="send_email",
                                 args={"to": "bob@acme.com", "subject": "hi"})],
                                 finish_reason="tool_calls")
        return ModelResponse(tool_calls=[ToolCall(name="finish", args={"summary": "done"})],
                             finish_reason="tool_calls")

    store = SqliteStore()
    ch = WebChannel(store)
    op = Operator(store=store, model=FakeModel(on_complete=fn), search=FakeSearch(),
                  integrations=FakeIntegrations(), timers=LocalTimers(), channel=ch)
    cp = ControlPlane(store=store, operator=op)
    run, goal = cp.begin(goal_text="venue")
    cp.drive(run, goal)
    assert store.get_run(run.run_id).status.value == "awaiting_approval"

    # two threads race to resume the same parked run with the same approval
    barrier = _t.Barrier(2)

    def racer():
        barrier.wait()
        try:
            cp.answer(run_id=run.run_id, answer="yes")
        except Exception:
            pass

    threads = [_t.Thread(target=racer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert store.get_run(run.run_id).status.value == "done"
    assert len(sends) == 1, f"the approved send ran {len(sends)} times — double-drive!"

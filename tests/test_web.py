"""The web dashboard — exercised through Starlette's TestClient (offline, no server)."""

from __future__ import annotations

import contextlib
import json
import threading
import time

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
from flowers.types import ToolCall


def _brain(steps, actions=None):
    actions = actions or {}
    def fn(messages, tools, role):
        if role == "planner" and "intake step" in messages[0]["content"]:
            return ModelResponse(content=json.dumps({"questions": []}))
        if role == "planner":
            return ModelResponse(content=json.dumps({"steps": steps}))
        user = messages[1]["content"]
        acts = next((a for s, a in actions.items() if s in user), [])
        n = sum(1 for m in messages if m.get("role") == "tool")
        if n < len(acts):
            return ModelResponse(tool_calls=[acts[n]], finish_reason="tool_calls")
        return ModelResponse(tool_calls=[ToolCall(name="finish", args={"summary": "done"})],
                             finish_reason="tool_calls")
    return FakeModel(on_complete=fn)


@contextlib.contextmanager
def _client(model, *, integrations=None):
    """Yield (client, store) with the TestClient ENTERED, so background drive tasks run on the
    client's persistent portal loop instead of racing a per-request portal's shutdown."""
    store = SqliteStore()
    ch = WebChannel(store)
    op = Operator(store=store, model=model, search=FakeSearch(),
                  integrations=integrations or FakeIntegrations(), timers=LocalTimers(), channel=ch)
    cp = ControlPlane(store=store, operator=op)
    with TestClient(create_app(cp, ch)) as client:
        yield client, store


def _settle(client, rid, *, want, timeout=5.0):
    """Poll the run until it reaches `want` — the drive now runs in a background thread, so a POST returns
    before the run settles."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get(f"/api/runs/{rid}").json().get("status")
        if st == want:
            return st
        time.sleep(0.02)
    return client.get(f"/api/runs/{rid}").json().get("status")


def test_index_serves_dashboard():
    with _client(_brain([{"text": "noop"}])) as (client, _):
        r = client.get("/")
        assert r.status_code == 200 and "flowers" in r.text


def test_index_missing_asset_is_clear_500(monkeypatch):
    # The dashboard is package data; if it's missing (broken install), GET / must say so plainly —
    # an actionable JSON 500, not a stack trace or an import-time crash.
    import flowers.channels.web as web_mod
    monkeypatch.setattr(web_mod, "_dashboard_html", lambda: None)
    with _client(_brain([{"text": "noop"}])) as (client, _):
        r = client.get("/")
        assert r.status_code == 500 and "reinstall" in r.json()["error"]


def test_post_goal_runs_and_logs_events():
    model = _brain([{"text": "write a brief"}],
                   {"write a brief": [ToolCall(name="write_file", args={"path": "b.md", "content": "x"})]})
    with _client(model) as (client, _):
        r = client.post("/api/goal", json={"identity": "web:u1", "text": "write a brief", "budget": 1.0})
        assert r.status_code == 200
        rid = r.json()["run_id"]
        assert _settle(client, rid, want="done") == "done"
        events = client.get(f"/api/runs/{rid}/events").json()["events"]
        kinds = [e["kind"] for e in events]
        assert "plan_announce" in kinds and "done" in kinds


def test_approval_flow_over_http():
    model = _brain([{"text": "email the venue"}],
                   {"email the venue": [ToolCall(name="send_email",
                    args={"to": "bob@acme.com", "subject": "Venue inquiry"})]})
    with _client(model) as (client, _):
        rid = client.post("/api/goal", json={"identity": "web:u1", "text": "email the venue"}).json()["run_id"]
        assert _settle(client, rid, want="awaiting_approval") == "awaiting_approval"
        client.post("/api/answer", json={"run_id": rid, "text": "yes"})
        assert _settle(client, rid, want="done") == "done"


def test_unknown_run_is_404():
    with _client(_brain([{"text": "noop"}])) as (client, _):
        assert client.get("/api/runs/nope").status_code == 404


def test_sse_replay_streams_logged_events():
    model = _brain([{"text": "write a brief"}],
                   {"write a brief": [ToolCall(name="write_file", args={"path": "b.md", "content": "x"})]})
    with _client(model) as (client, _):
        rid = client.post("/api/goal", json={"identity": "web:u1", "text": "write a brief"}).json()["run_id"]
        _settle(client, rid, want="done")   # let the background drive log its events before we replay
        with client.stream("GET", f"/events/{rid}?replay_only=1") as r:
            body = "".join(chunk for chunk in r.iter_text())
        assert "event: plan_announce" in body and "event: done" in body


def test_served_app_drives_the_timer_poller():
    # The durable-timer DRIVER: a SERVED app (lifespan entered) must call control_plane.tick() so parked
    # await/monitor runs resume. (tick()'s resume semantics are covered by test_operator / test_e2e.)
    fired = threading.Event()

    class _CP:
        def stalled_run_ids(self):
            return []

        def tick(self):
            fired.set()
            return []

    app = create_app(_CP(), WebChannel(SqliteStore()), poll_interval=0.01)
    with TestClient(app):
        assert fired.wait(2.0), "the served app did not drive control_plane.tick()"


def test_startup_does_not_block_on_slow_crash_recovery():
    # Regression: crash recovery must NOT run before the app serves. The lifespan snapshots orphan ids
    # synchronously (fast) but re-drives them in the background — so a slow/stuck recovery never hangs
    # startup. Here recover_run_ids blocks until released; entering the served context must still succeed
    # promptly, and recovery must run for the snapshotted ids once we're serving.
    release = threading.Event()
    recovered_with = []

    class _CP:
        def stalled_run_ids(self):
            return ["orphan-1"]

        def recover_run_ids(self, run_ids):
            recovered_with.append(list(run_ids))
            release.wait(2.0)   # simulate a slow re-drive (real model/network)
            return []

        def tick(self):
            return []

    app = create_app(_CP(), WebChannel(SqliteStore()), poll_interval=0.01)
    t0 = time.monotonic()
    with TestClient(app):                       # would hang here if recovery blocked startup
        entered = time.monotonic() - t0
        assert entered < 1.0, f"startup blocked on recovery ({entered:.2f}s)"
        # recovery runs in the background for exactly the snapshotted orphan ids
        for _ in range(200):
            if recovered_with:
                break
            time.sleep(0.01)
        release.set()
    assert recovered_with == [["orphan-1"]]


def test_no_poller_without_a_poll_interval():
    # The default (poll_interval=None) attaches NO lifespan poller — runs execute in-request only.
    calls = []

    class _CP:
        def tick(self):
            calls.append(1)
            return []

    app = create_app(_CP(), WebChannel(SqliteStore()))
    with TestClient(app):
        time.sleep(0.1)
    assert calls == []

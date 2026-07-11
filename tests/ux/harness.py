"""The conversation-replay driver (P2 / §5).

A scenario is a script of inbound owner texts driven END-TO-END through the REAL stack at the WEB
layer: a Starlette ``TestClient`` over ``flowers.channels.web.create_app``, backed by the real
``ControlPlane`` + ``Operator`` + trust gate + SQLite store — with only the *inputs* scripted (the
model's output via a Fake ``on_complete`` brain, the integration world via ``FakeIntegrations``, and
the clock via ``LocalTimers``). It is the same "real decisions, scripted inputs" contract as
``tests/_harness.build``, lifted to the HTTP surface a chat front end actually talks to.

Why not ``flowers.app.build_app`` verbatim: offline (no keys) ``build_app`` wires ``FakeModel([])``
(no script) and sets ``degraded`` — so ``POST /api/goal`` 503s and no scripted conversation is
possible. This driver mirrors ``build_app``'s wiring (same ``send_preview`` / ``fast_path`` /
``escalation_ttl_h`` knobs, same Fakes-when-unkeyed substrate) but injects a *scripted* brain and a
*controllable* integration world + clock, which is the whole point of a replay harness.

MAINTENANCE CONTRACT: this mirror and ``flowers.app.build_app`` must be kept in sync BY HAND — if
``build_app`` grows a new Operator knob or changes a default/wrapping, update ``UX.__init__`` to
match, or these scenarios stay green while production wiring drifts. (Cross-linked comment lives on
``build_app``'s Operator construction.)

A scenario step is: inbound text -> drain the run's durable event log -> assert invariants. The
``TestClient`` is ENTERED (a persistent portal) so the web layer's BACKGROUND drive tasks actually
run (a ``POST /api/goal`` returns before the run settles — exactly as in production), then
``_settle`` polls the run to its next parked/terminal state. Timer scenarios advance the VIRTUAL
clock (``advance``) and pump ``tick()`` directly — no real sleeping, no wall-clock dependence.

Keep this driver small: it is the template every future scenario copies.
"""

from __future__ import annotations

import time

from starlette.testclient import TestClient

from flowers.channels.web import WebChannel, create_app
from flowers.controlplane import ControlPlane
from flowers.engine.operator import Operator
from flowers.seams.integrations import FakeIntegrations
from flowers.seams.search import FakeSearch
from flowers.seams.store import SqliteStore
from flowers.seams.timers import LocalTimers

# Run statuses that mean "still driving" — ``_settle`` polls until the run leaves this set for a
# parked (awaiting the owner) or terminal state. Mirrors the transient PENDING/PLANNING/RUNNING trio.
_IN_FLIGHT = frozenset({"pending", "planning", "running"})


class UX:
    """One scenario's live stack: a TestClient over the real web app + handles on the seams a scenario
    drives (the integration world, the virtual clock, the store). Use as a context manager so the
    persistent portal is up while background drives run::

        with UX(model=brain, integrations=FakeIntegrations()) as ux:
            rid = ux.goal("send an email to marc@acme.com saying hi")
            ux.answer(rid, "yes")
            invariants.one_touch(ux.events(rid))
    """

    def __init__(self, *, model, integrations=None, send_preview="always", fast_path_enabled=True,
                 verify_attempts=1, verify_delay=0.0, escalation_ttl_h=24.0):
        self.store = SqliteStore()                      # :memory: — ephemeral, thread-safe (WAL + lock)
        self.channel = WebChannel(self.store)           # events write THROUGH to the durable store log
        self.integ = integrations if integrations is not None else FakeIntegrations()
        self.timers = LocalTimers()                     # virtual clock: advance() fast-forwards timers
        self.op = Operator(
            store=self.store, model=model, search=FakeSearch(), integrations=self.integ,
            timers=self.timers, channel=self.channel,
            send_preview=send_preview, fast_path_enabled=fast_path_enabled,
            verify_attempts=verify_attempts, verify_delay=verify_delay,
            escalation_ttl_h=escalation_ttl_h)
        self.cp = ControlPlane(store=self.store, operator=self.op)
        # No poll_interval: the scenario pumps tick() itself (deterministic), rather than a real-time
        # background poller. The TestClient portal still runs the web layer's background DRIVE tasks.
        self._tc = TestClient(create_app(self.cp, self.channel))
        self.client = None

    def __enter__(self) -> UX:
        self.client = self._tc.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        self._tc.__exit__(*exc)

    # ---- inbound owner texts (the conversation) ------------------------------------------------

    def goal(self, text: str, *, budget: float = 2.0, settle: bool = True) -> str:
        """POST a goal -> return its run_id, settled to the next parked/terminal state (the drive runs
        in the background, so a bare POST returns before the run has done anything)."""
        r = self.client.post("/api/goal", json={"text": text, "budget": budget})
        assert r.status_code == 200, f"/api/goal {r.status_code}: {r.text}"
        rid = r.json()["run_id"]
        if settle:
            self.settle(rid)   # a fresh goal starts in-flight (pending) and parks/terminates exactly once
        return rid

    def answer(self, run_id: str, text: str, *, settle: bool = True) -> str:
        """POST an owner answer (approval yes/no, clarify reply, escalation reply) -> settled status.

        The background resume starts AFTER the POST returns, so at that instant the run still shows its
        pre-answer PARKED status (awaiting_approval / escalated / clarifying) — a plain 'not in-flight'
        poll would return it immediately, racing past the resume. So we settle on the answer being
        CONSUMED: the run is terminal, OR it re-parked on a genuinely NEW question (a fresh approval id).
        Every park/escalate mints a new ApprovalRequest id, so an id change is the crisp 'this answer was
        processed' signal (clarify->preview, preview->escalation, escalation->done, etc.)."""
        pre = self.store.get_run(run_id)
        apr_before = pre.pending_approval.id if pre and pre.pending_approval else None
        r = self.client.post("/api/answer", json={"run_id": run_id, "text": text})
        assert r.status_code == 200, f"/api/answer {r.status_code}: {r.text}"
        return self._settle_answer(run_id, apr_before) if settle else self.status(run_id)

    def route(self, text: str, runs: list[dict]) -> dict:
        """POST /api/route — the chat client's task-vs-reply-vs-chat classifier hook. Synchronous (no drive)."""
        r = self.client.post("/api/route", json={"text": text, "runs": runs})
        assert r.status_code == 200, f"/api/route {r.status_code}: {r.text}"
        return r.json()

    def chat(self, text: str, *, history=None, runs=None) -> str:
        r = self.client.post("/api/chat", json={"text": text, "history": history or [], "runs": runs or []})
        assert r.status_code == 200, f"/api/chat {r.status_code}: {r.text}"
        return r.json()["reply"]

    # ---- observation ---------------------------------------------------------------------------

    def settle(self, run_id: str, *, timeout: float = 5.0) -> str:
        """Poll the run until it leaves the in-flight (still-driving) states — the background drive
        settles a POST asynchronously, so we wait for a parked/terminal status. Same synchronization
        pattern as tests/test_web._settle (a bounded poll, not a timing dependence): fakes settle in
        milliseconds; the timeout only guards a hang."""
        deadline = time.time() + timeout
        st = self.status(run_id)
        while st in _IN_FLIGHT and time.time() < deadline:
            time.sleep(0.01)
            st = self.status(run_id)
        return st

    def _settle_answer(self, run_id: str, apr_before, *, timeout: float = 5.0) -> str:
        """Wait until an owner answer has been CONSUMED by the background resume: the run is terminal
        (done/stopped), or it re-parked on a NEW question (its pending approval id changed from
        ``apr_before``). Robust against the resume emitting interim progress events while the persisted
        status still reads the stale pre-answer parked value."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            run = self.store.get_run(run_id)
            st = run.status.value if run else "stopped"
            apr = run.pending_approval if run else None
            if st in ("done", "stopped"):
                return st
            if st not in _IN_FLIGHT and apr is not None and apr.id != apr_before:
                return st
            time.sleep(0.01)
        return self.status(run_id)

    def status(self, run_id: str) -> str:
        r = self.client.get(f"/api/runs/{run_id}")
        assert r.status_code == 200, f"/api/runs/{run_id} {r.status_code}: {r.text}"
        return r.json()["status"]

    def events(self, run_id: str) -> list[dict]:
        """The run's DURABLE owner-facing event log (the same timeline SSE replays), drained over HTTP —
        the deterministic surface the invariants assert against."""
        r = self.client.get(f"/api/runs/{run_id}/events")
        assert r.status_code == 200, f"/api/runs/{run_id}/events {r.status_code}: {r.text}"
        return r.json()["events"]

    def run_ids(self) -> list[str]:
        """Every run the server knows about (how a chat client discovers open runs) — for terminal_runs and
        the 'no new run spawned' checks."""
        return [r["run_id"] for r in self.client.get("/api/runs").json()["runs"]]

    def effects(self, run_id: str):
        return self.store.get_effects(run_id)

    def run(self, run_id: str):
        return self.store.get_run(run_id)

    # ---- virtual clock (timer scenarios) -------------------------------------------------------

    def advance(self, seconds: float) -> None:
        """Fast-forward the virtual clock (no real sleeping) so durable timers become due."""
        self.timers.advance(seconds)

    def tick(self) -> None:
        """Pump due timers -> resume/reverify/reap their runs. Runs synchronously in the caller's thread
        (the reaper/reverify mutate the store + emit events in-line), so no settle is needed after it."""
        self.cp.tick()

"""ASGI entrypoint — ``uvicorn flowers.app:app`` (needs the ``[web]`` extra).

One factory, ``build_app``, wires the flowers REST API on the minimal local substrate (SQLite store,
LocalTimers, LocalTracer, a per-run local sandbox). It picks the live model/search/integrations/browser
adapter when its key is present and otherwise the offline fake, so the dashboard serves at $0 with no
credentials configured. It is a single-user local surface with no auth — bind it to localhost.

Optional cloud adapters (Postgres store, E2B sandbox, Langfuse telemetry, Brave search) live in
``flowers/extras/`` as importable templates; they are NOT wired here. To use one, swap it into
``build_app`` in place of the default (see the README).

Secrets/config come from the environment (see ``.env.example``). The broker remains the single metered
egress: the executor runs sandboxed with no credentials, and every world-touching call is routed through
the broker, which takes the independent before/after read-back the trust gate adjudicates.
"""

from __future__ import annotations

import logging
import os

from flowers import runtime
from flowers.channels.web import WebChannel, create_app
from flowers.controlplane import ControlPlane
from flowers.engine.operator import Operator
from flowers.seams.browser import BrowserbaseBrowser, FakeBrowser
from flowers.seams.integrations import ArcadeIntegrations, FakeIntegrations
from flowers.seams.model import FakeModel, OpenRouterModel
from flowers.seams.search import FakeSearch, TavilySearch
from flowers.seams.store import SqliteStore
from flowers.seams.telemetry import LocalTracer
from flowers.seams.timers import LocalTimers

_log = logging.getLogger("flowers.app")


def _pick(live, fake):
    return live if live.available() else fake


def _tick_seconds(*, default: float) -> float:
    """The background timer-poll interval (seconds). FLOWERS_TICK_SECONDS overrides; 0 disables the
    poller. Invalid values fall back to the default rather than crash the app at import."""
    raw = runtime.env("FLOWERS_TICK_SECONDS")
    if raw is None or raw == "":
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


def build_app(*, db_path: str = "flowers.db", timers_path: str = "flowers_timers.db"):
    """Assemble the flowers REST API on the minimal local substrate (the published default).

    Wires the live model/search/integrations/browser when their keys are present, else the offline
    fakes — so the app serves at $0 with no credentials. It is a single-user local surface with no auth;
    bind it to localhost. Optional cloud adapters (Postgres, E2B, Langfuse, Brave) live in flowers/extras/
    and are not wired here — see the README to swap one in.
    """
    store = SqliteStore(db_path)
    channel = WebChannel(store)   # events write through to the store: the log survives a restart
    live_model = OpenRouterModel()
    # With no model key, POST /api/goal must FAIL FAST with an actionable message — the wired FakeModel
    # has no scripted answers outside the test suite, so accepting a goal would let it die mid-run.
    # Everything else (the dashboard, /health, the event log) still serves at $0.
    degraded = (None if live_model.available() else
                "no model is configured — set OPENROUTER_API_KEY in .env (see .env.example) and "
                "restart; flowers cannot run goals without a model")
    operator = Operator(
        channel=channel,
        model=_pick(live_model, FakeModel([])),
        search=_pick(TavilySearch(), FakeSearch()),
        integrations=_pick(ArcadeIntegrations(), FakeIntegrations()),
        # the store doubles as the browser-context registry (persistent per-site logins).
        browser=_pick(BrowserbaseBrowser(context_store=store), FakeBrowser()),
        store=store,
        timers=LocalTimers(timers_path),
        tracer=LocalTracer(),
    )
    control_plane = ControlPlane(store=store, operator=operator)
    return create_app(control_plane, channel, poll_interval=_tick_seconds(default=15.0),
                      degraded=degraded)


# Load `.env` (KEY=VALUE) into the environment before wiring adapters, so keys set there are seen by
# the availability gates. A real env var always wins; FLOWERS_ENV_FILE overrides the path. This is why
# copying `.env.example` to `.env` "just works" — uvicorn does not read `.env` on its own.
runtime.load_dotenv(os.environ.get("FLOWERS_ENV_FILE", ".env"))

# The module-level app uvicorn imports. FLOWERS_DB / FLOWERS_TIMERS_DB override the on-disk paths.
app = build_app(db_path=os.environ.get("FLOWERS_DB", "flowers.db"),
                timers_path=os.environ.get("FLOWERS_TIMERS_DB", "flowers_timers.db"))

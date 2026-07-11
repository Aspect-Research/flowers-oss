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

import json
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


def _role_config_from_env() -> dict[str, dict] | None:
    """Optional model-routing override from ``FLOWERS_ROLE_CONFIG_JSON`` (a JSON map of role ->
    {model, reasoning}, same shape as ``DEFAULT_ROLE_CONFIG``). Because role resolution falls back to
    the ``executor`` entry, ``{"executor": {"model": "...", "reasoning": "low"}}`` reroutes EVERY role
    — the one-line way to run flowers on a single (e.g. free) model. Invalid JSON logs a warning and
    is ignored rather than crashing the app at import."""
    raw = runtime.env("FLOWERS_ROLE_CONFIG_JSON")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        _log.warning("FLOWERS_ROLE_CONFIG_JSON is not valid JSON — using the default role config")
        return None
    if not isinstance(parsed, dict) or not all(isinstance(v, dict) for v in parsed.values()):
        _log.warning("FLOWERS_ROLE_CONFIG_JSON must be a JSON object of role -> config — ignoring")
        return None
    return parsed


def _verify_polling(*, live: bool) -> tuple[int, float]:
    """Read-back polling for effect verification -> ``(attempts, delay_seconds)``.

    A live provider indexes a just-written effect with a few seconds' lag (Gmail's Sent label is the
    canonical case), so the LIVE default polls a few times with a short delay rather than a single
    instant check — otherwise a real send reads back as absent/unverifiable and falsely escalates
    ("I sent it but couldn't confirm it landed"). Offline the fakes are instant + deterministic, so it
    stays a single check to keep the suite fast and $0. FLOWERS_VERIFY_ATTEMPTS / FLOWERS_VERIFY_DELAY
    override either default; invalid values fall back rather than crash at import."""
    attempts_default, delay_default = (4, 1.5) if live else (1, 0.0)

    def _int(name: str, default: int) -> int:
        raw = runtime.env(name)
        try:
            return max(1, int(raw)) if raw not in (None, "") else default
        except (TypeError, ValueError):
            return default

    def _float(name: str, default: float) -> float:
        raw = runtime.env(name)
        try:
            return max(0.0, float(raw)) if raw not in (None, "") else default
        except (TypeError, ValueError):
            return default

    return _int("FLOWERS_VERIFY_ATTEMPTS", attempts_default), _float("FLOWERS_VERIFY_DELAY", delay_default)


def _send_preview() -> str:
    """The draft-preview mode for OWNER-GRANT (auto-committed) sends -> ``"always"`` (default) shows the
    outgoing draft as the single owner confirm; ``"never"`` sends it directly (zero touches). Set via
    FLOWERS_SEND_PREVIEW; any other value falls back to the safe default rather than crash at import."""
    return "never" if runtime.env("FLOWERS_SEND_PREVIEW").lower() == "never" else "always"


def _escalation_ttl_h(*, default: float = 24.0) -> float:
    """Hours an ESCALATED run may sit unanswered before the zombie-run reaper closes it (P1.1).
    FLOWERS_ESCALATION_TTL_H overrides; non-positive / invalid values fall back to the default rather
    than crash at import (and never arm an instant-fire reaper)."""
    raw = runtime.env("FLOWERS_ESCALATION_TTL_H")
    if raw in (None, ""):
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return default
    return val if val > 0 else default


def _fast_path() -> bool:
    """The single-action fast path (P1.3): on (default) skips the clarifier + planner for a self-contained
    'email <one named address> saying <content>' (~5 -> <=2 model calls). FLOWERS_FAST_PATH=off (also 0 /
    false / no) disables it -> even the canonical goal runs the full pipeline; any other value keeps the
    default. Same env-knob pattern as :func:`_send_preview`."""
    return runtime.env("FLOWERS_FAST_PATH").lower() not in ("off", "0", "false", "no")


def build_app(*, db_path: str = "flowers.db", timers_path: str = "flowers_timers.db"):
    """Assemble the flowers REST API on the minimal local substrate (the published default).

    Wires the live model/search/integrations/browser when their keys are present, else the offline
    fakes — so the app serves at $0 with no credentials. It is a single-user local surface with no auth;
    bind it to localhost. Optional cloud adapters (Postgres, E2B, Langfuse, Brave) live in flowers/extras/
    and are not wired here — see the README to swap one in.
    """
    store = SqliteStore(db_path)
    channel = WebChannel(store)   # events write through to the store: the log survives a restart
    live_model = OpenRouterModel(role_config=_role_config_from_env())
    # With no model key, POST /api/goal must FAIL FAST with an actionable message — the wired FakeModel
    # has no scripted answers outside the test suite, so accepting a goal would let it die mid-run.
    # Everything else (the dashboard, /health, the event log) still serves at $0.
    degraded = (None if live_model.available() else
                "no model is configured — set OPENROUTER_API_KEY in .env (see .env.example) and "
                "restart; flowers cannot run goals without a model")
    arcade = _pick(ArcadeIntegrations(), FakeIntegrations())
    browser = _pick(BrowserbaseBrowser(context_store=store), FakeBrowser())  # store = browser-context registry
    # A live side-effecting provider is wired -> tolerate its read-back lag; all-fakes stays instant.
    live_io = isinstance(arcade, ArcadeIntegrations) or isinstance(browser, BrowserbaseBrowser)
    verify_attempts, verify_delay = _verify_polling(live=live_io)
    # NOTE: tests/ux/harness.py mirrors this Operator construction (it can't call build_app — the
    # offline degrade would 503 every goal). If you add/change a knob or wrapping here, update the
    # UX harness to match, or the usability regression floor silently tests stale wiring.
    operator = Operator(
        channel=channel,
        model=_pick(live_model, FakeModel([])),
        search=_pick(TavilySearch(), FakeSearch()),
        integrations=arcade,
        browser=browser,
        store=store,
        timers=LocalTimers(timers_path),
        tracer=LocalTracer(),
        verify_attempts=verify_attempts,
        verify_delay=verify_delay,
        send_preview=_send_preview(),
        escalation_ttl_h=_escalation_ttl_h(),
        fast_path_enabled=_fast_path(),
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

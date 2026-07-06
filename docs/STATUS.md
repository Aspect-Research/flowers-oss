# STATUS — what's real vs. offline-fake

This is an honest map of what actually works, what is a live adapter you must key in, and what is a
deliberate no-op. flowers is maintained part-time with a deliberately small scope (see the maintenance
note in the README) — treat this document as the ground truth of the current surface, not a roadmap.

## The trust core (the point) — real

`flowers/trustgate.py`, `flowers/effects.py`, and `flowers/policy.py` are pure, dependency-free, and fully
exercised by the offline test suite. Every world-touching call goes through a single credentialed
**broker** (`flowers/broker.py`); the executor runs sandboxed with **no** credentials. The broker takes an
independent before/after read-back, builds a typed `EffectRecord`, and the gate adjudicates. The verdict is
a pure function of that record — no LLM is in it — and the authorization levers (policy overrides, the
autonomy mandate) can only raise strictness, never authorize what the gate refuses. This is the part worth
reading.

## Seams — wired by default

Each seam (`flowers/seams/`) has an offline **Fake** and a key-gated **live adapter**; the default
`build_app` picks the live adapter when its key is present, else the Fake. With no keys set, the whole app
serves at **$0 / no network**.

| Seam | Wired default | Live when keyed |
|---|---|---|
| model | `FakeModel` | `OpenRouterModel` (`OPENROUTER_API_KEY`) |
| search | `FakeSearch` | `TavilySearch` (`TAVILY_API_KEY`) |
| integrations | `FakeIntegrations` | `ArcadeIntegrations` (`ARCADE_API_KEY`; Gmail send/search/fetch/label/trash + Google Calendar, each verified by an independent read-back; dev mode = your own Google account) |
| browser | `FakeBrowser` | `BrowserbaseBrowser` — **OFF by default**; set `BROWSERBASE_API_KEY` + `BROWSERBASE_PROJECT_ID` |
| sandbox | `LocalSubprocessSandbox` (real local box, not a stub) | — (see extras `E2BSandbox`) |
| store | `SqliteStore` | — (see extras `PostgresStore`) |
| timers | `LocalTimers` (sqlite-durable) | — |
| telemetry | `LocalTracer` | — (see extras `LangfuseTracer`) |

(The browser live adapter also needs the `browser` extra installed: `pip install -e ".[browser]"`.)

## Optional adapter templates — `flowers/extras/` (NOT wired)

`PostgresStore`, `E2BSandbox`, `LangfuseTracer`, `BraveSearch`. They
satisfy the same seam Protocols and are lint-clean and importable, but are not constructed by `build_app`.
To use one, swap it into `build_app` and install the matching extra. They are reference templates, not a
supported surface.

## Money is out — architectural

flowers never moves money — there is no billing, charging, or payment code in the tree at all. Money-moving
toolkits and actions are categorically refused by the policy layer (`is_money_action` → `REFUSE`, decided
before any tier or override). The only spend control is `Goal.budget_usd`, a per-run dollar ceiling that
escalates a run when hit (it never charges anyone).

## Surface

One REST API (`flowers/channels/web.py`, reference in [`API.md`](API.md)): `POST /api/goal`,
`POST /api/answer`, `GET /api/runs`, `GET /api/runs/{id}`, `GET /api/runs/{id}/events`,
`GET /events/{id}` (SSE with durable ids + `Last-Event-ID` resume), `/health`, `/ready`, plus the chat
dashboard at `/` (a static asset shipped in the package). Launched by the `flowers serve` console
command (or `uvicorn flowers.app:app`). It's a single-user local surface with **no auth** — run it on
localhost. There are no phone/SMS/email-inbound channels in this release.

## Connect

The OAuth connect round-trip (`needs_auth → AWAITING_CONNECT → poll-resume`) is real and tested: when a
goal needs an account you haven't connected, the run parks, surfaces a consent link, polls for the grant on
the durable tick, and resumes the exact pending action once connected. Arcade "dev mode" connects your own
Google account; there is no multi-user verifier route in this release.

## Tests

`pytest` runs the whole suite offline, at $0, with no keys and no network (enforced by the root
`conftest.py`). Postgres tests skip unless a DSN is set. Live-adapter checks are opt-in only.

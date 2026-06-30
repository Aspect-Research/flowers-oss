# flowers

**A trustable agent that never lies about what it accomplished.** Give it a goal in plain language; it
uses tools to make something happen in the world, and a deterministic, **no-LLM** gate refuses to report
"done" unless the world actually reflects the effect.

> Experimental, provided as-is, **not actively maintained**. It exists to demonstrate one idea — a
> verification gate that no language model can talk its way past. Fork it freely.

## The trust gate (this is the point)

Most agents decide they succeeded by *asking a model* whether they succeeded. flowers doesn't. Instead:

- Every world-touching call goes through a single credentialed **broker** (`flowers/broker.py`). The
  executor (the part the LLM drives) **holds no credentials** — it cannot authenticate to a provider
  directly; it asks the broker, which is the single egress for credentialed effects. (The wired-default
  sandbox runs shell/file work locally with secrets stripped from the environment; the optional `E2BSandbox`
  in `flowers/extras/` adds full microVM isolation whose only egress is the broker.)
- For each side-effect, the broker takes an **independent before/after read-back** from the provider (e.g.
  after a "send email," it reads the Sent mailbox) and builds a typed `EffectRecord` — a factual record of
  what actually changed in the world.
- A pure, zero-dependency gate adjudicates that record:
  [`flowers/trustgate.py`](flowers/trustgate.py) + [`flowers/effects.py`](flowers/effects.py) +
  [`flowers/policy.py`](flowers/policy.py). The verdict is computed by fingerprint-matching the expected
  effect against the read-back — **no LLM is in this path.**
- The verdict is a **pure function of that `EffectRecord`** — no LLM is in it. The only levers a
  model/owner can pull (the auto/ask/never policy overrides and the autonomy mandate) can *raise*
  strictness; they never touch verification, so they cannot authorize what the gate refuses. A fabricated
  "done" that never landed is refused by construction, proven by tests through the real code path.

Money-out is an **architectural refusal**, not a prompt: money-moving toolkits are categorically rejected
by the policy layer, and there is no charging path anywhere in the code.

If you read three files, read those three.

## Architecture

flowers is built on **seams**: one `Protocol` per external dependency (`flowers/seams/interfaces.py`), each
with an offline **Fake** and a key-gated **live adapter**. The engine run-loop is methodical —
clarify → plan → execute → **gate** → durable await/monitor/recurring — and parks long-running work on
durable timers so it survives a restart. Because every dependency has a Fake, the **entire test suite runs
at $0, offline, with no keys** (enforced by `conftest.py`).

```
flowers/
  trustgate.py  effects.py  policy.py   the deterministic, no-LLM verdict core — the showcase
  broker.py                             the single credentialed egress (the executor holds no keys)
  types.py                              core dataclasses + the EffectRecord trust contract
  engine/                               planner, operator, executor, clarifier, announcer, scheduler
  seams/                                model, search, integrations, browser, sandbox, store, timers, ...
  channels/                             web (the REST API), inproc, base
  extras/                               optional cloud adapter templates (NOT wired by default)
docs/STATUS.md                          honest "what's real vs offline-fake"
tests/                                  offline ($0 / no-network) suite by contract
```

## Quickstart

```bash
pip install -e ".[web]"          # the REST API (Starlette + uvicorn). The core needs only the stdlib.
py -3 -m pytest                  # the whole suite runs offline, no keys, no network
uvicorn flowers.app:app          # serve the dashboard + REST API at http://127.0.0.1:8000
```

Open `http://127.0.0.1:8000` for a minimal chat dashboard. With no keys, flowers wires the offline fakes,
so the dashboard, `/health`, the REST endpoints, and the **full test suite** all work at $0. Note that
*running a goal needs a model*: set `OPENROUTER_API_KEY` (below) before `POST /api/goal`, or the run errors
out — the offline fake model only has scripted answers inside the test suite, not for ad-hoc goals.

For real work, set three keys — copy `.env.example` to `.env` and fill them in (the app loads `.env`
from the working directory at startup; a real environment variable always wins):

```bash
OPENROUTER_API_KEY=...    # the model (planner + executor)
TAVILY_API_KEY=...        # web search
ARCADE_API_KEY=...        # Gmail send + Google Calendar; Arcade "dev mode" connects your own Google account
```

flowers is a **single-user, local** tool: the dashboard and REST API have **no auth**, so run it on
`localhost` (the default `uvicorn` bind) and don't expose the port to an untrusted network. See
[`.env.example`](.env.example) for every knob.

## Using the REST API

A terminal or chat client is just `curl`:

```bash
# start a goal
curl -X POST localhost:8000/api/goal -H 'content-type: application/json' \
     -d '{"text":"find three nearby florists open on Sunday and summarize their hours"}'

# stream events for a run (Server-Sent Events)
curl -N localhost:8000/events/<run_id>

# answer an approval / clarification the agent is waiting on (the reply text goes in "text")
curl -X POST localhost:8000/api/answer -H 'content-type: application/json' \
     -d '{"run_id":"<run_id>","text":"yes"}'
```

Other routes: `GET /api/runs/{id}`, `GET /health` (liveness), `GET /ready` (store reachable). There is no
auth — it's a single-user localhost surface.

## Optional adapters (`flowers/extras/`)

Optional, heavier-weight adapters live in `flowers/extras/` as importable, lint-clean **templates** — they are not
wired into `build_app`. Each satisfies the same seam Protocol as its wired default; to use one, construct
it in place of the default in `flowers/app.py` and install the matching extra:

| Adapter | Replaces | Extra |
|---|---|---|
| `PostgresStore` | `SqliteStore` | `.[postgres]` |
| `E2BSandbox` | `LocalSubprocessSandbox` | `.[e2b]` |
| `LangfuseTracer` | `LocalTracer` | — (stdlib) |
| `BraveSearch` | (search fallback) | — (stdlib) |

## Browser last-mile

The browser seam (observer-verified web actions) is **off by default**. Install the extra
(`pip install -e ".[browser]"`) and set `BROWSERBASE_API_KEY` + `BROWSERBASE_PROJECT_ID` to enable
`BrowserbaseBrowser`; otherwise the offline `FakeBrowser` is used. Even
live, browser actions are independently observed so the gate can confirm what actually happened.

## Status & honesty

[`docs/STATUS.md`](docs/STATUS.md) is the one honest doc: exactly what is real, what is a live adapter you
must key in, and what is a deliberate no-op.

## License & maintenance

Apache-2.0 (see [`LICENSE`](LICENSE)). **Experimental, provided as-is, and not actively maintained** — it
was built to demonstrate the verification gate. Fork it, lift the gate, do what you like.

# The flowers REST + SSE API

One single-user, local, no-auth surface, served by `flowers serve` (default `http://127.0.0.1:8000`).
Bodies are JSON; errors come back as `{"error": "...", ...}` with a meaningful status code.

## Routes

| Method | Path | What it does |
|---|---|---|
| `POST` | `/api/goal` | Start a run. Body `{"text": "...", "budget": 2.0}` (budget in USD, optional). Returns `{"run_id", "status"}` immediately; the run drives in the background and streams events. **503** with `reason_code: "model_unavailable"` when no model key is configured. |
| `POST` | `/api/answer` | Send the owner's reply to a run: an approval ("yes"/"no"), a clarifying answer, guidance for an escalated run, or a mid-run message (acknowledged and folded into the agent's next step). Body `{"run_id": "...", "text": "..."}`. **400** without `run_id`. |
| `GET` | `/api/runs` | Recent runs, most recently touched first (`{"runs": [{run_id, status, goal, updated_at}]}`) — how a fresh dashboard discovers the open run to reattach to. |
| `GET` | `/api/runs/{id}` | Run status: `{"run_id", "status", "goal", "spent_usd"}` (spend summed live from the usage ledger). |
| `GET` | `/api/runs/{id}/events` | The run's full durable event log as JSON (`?after=<id>` for a tail) — the polling fallback. |
| `GET` | `/events/{id}` | **SSE stream**: replays the durable log, then tails live. `?replay_only=1` for replay-then-close, `?after=<id>` to resume. |
| `GET` | `/` | The dashboard (a static page in the package). |
| `GET` | `/health` | Liveness (no backend touch). |
| `GET` | `/ready` | Readiness — 503 when the store is unreachable. |

## The SSE stream

Standard Server-Sent Events. Each event carries the SSE `id:` field with a **durable, per-run
monotonic id** — assigned by the store, so it survives server restarts:

```
id: 7
event: progress
data: {"run_id": "run_...", "kind": "progress", "text": "step 1 done: ...", "id": 7}
```

- **Resume:** on auto-reconnect, `EventSource` sends `Last-Event-ID` and the stream continues after
  that id. A manual client can pass `?after=<id>`. Either way nothing is dropped or duplicated,
  even across a server restart.
- **Keepalive:** during quiet stretches (a long model call, a parked run) the server emits an SSE
  comment (`: keepalive`) every ~15s.
- **Lifetime:** the stream closes when the run **closes** (`done` or `stopped`). An `escalated` run
  keeps the stream open — it is parked on you, and your reply continues it in the same stream.

## Event kinds

| `kind` | Meaning | Extra fields |
|---|---|---|
| `plan_announce` | the plan, as owner-readable numbered steps | |
| `progress` | step lifecycle ("step 2 done: …") and in-step activity heartbeats ("step in progress — searching: …") | |
| `clarify` | the agent needs answers before planning | |
| `approval` | a side-effect (or the autonomy-mandate card) awaits your yes/no | `effect_label`, `tier`; `mandate: true` on the autonomy card |
| `connect` | an account needs connecting (OAuth) | `url`, `provider` |
| `notify` | informational: mid-run acknowledgments, watch results, honest refusals | |
| `done` | the run finished and the gate accepted the completion | |
| `escalated` | the run is parked on you with a question — reply to continue, "no" to stop | `reason_code` |

`reason_code` values on `escalated`: `model_error`, `tool_failed`, `budget_exhausted`,
`deadline_exhausted`, `owner_declined`, `internal_error` — machine-readable "why", alongside the human
text. (`model_unavailable` is not an escalation code — it is returned only in the `POST /api/goal` 503
body when no model is configured, before any run is created.)

## Run status lifecycle

`pending → clarifying? → planning → awaiting_go? → running → …`

| Status | Parked on | How it resumes |
|---|---|---|
| `clarifying` | your answers to clarifying questions | `POST /api/answer` |
| `awaiting_go` | the autonomy-mandate card | `POST /api/answer` yes/no |
| `awaiting_approval` | a specific side-effect | `POST /api/answer` yes/no (a "yes" authorizes only the exact action shown) |
| `awaiting_connect` | an OAuth connect link | connect the account; flowers polls and resumes itself |
| `waiting` | a durable timer (awaited replies / monitor / recurring) | the background tick |
| `escalated` | a review question | `POST /api/answer` — guidance continues the run, "no" stops it |
| `done` / `stopped` | — | terminal |

A message sent while the run is `running`/`planning` is never dropped: it is acknowledged with a
`notify` event and folded into the agent's next step or replan as context (it can never authorize a
side-effect — approvals only happen against a parked approval).

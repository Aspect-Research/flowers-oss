# Contributing to flowers

Thanks for looking. flowers is deliberately small — a deterministic trust gate with an agent around
it — and contributions that keep it small are the easiest to land.

## Dev setup

```bash
pip install -e ".[web,dev]"
pytest                # the whole suite, offline, no keys, $0 — by contract
ruff check .
```

That's the entire CI, too (`.github/workflows/ci.yml`) — if those two commands pass locally on
Python 3.11+, CI will agree.

## The three invariants

A PR must not break these; they are the project:

1. **The core is stdlib-only.** `flowers/` (outside `extras/`) imports nothing beyond the standard
   library. Web is an optional extra; heavier adapters live in `flowers/extras/` as unwired templates.
2. **The trust gate stays pure and closed.** `flowers/trustgate.py` imports nothing from `flowers`,
   and no change may put an LLM in the verification path or let any authorization lever (override,
   mandate, learned trust) weaken what the gate refuses. Wider autonomy is fine; weaker verification
   is not.
3. **The test suite runs offline at $0.** The root `conftest.py` force-blanks every provider key.
   New code needs offline tests through the real code path (see `tests/_harness.py` — scripted
   FakeModel driving the real engine). Live-adapter checks are opt-in: `FLOWERS_LIVE=1 pytest -m live`.

## House style

- Comments explain **why**, not what. The codebase carries heavy design-rationale comments — the
  reason a guard exists, the bug class it prevents — and new code should match. If a constraint isn't
  visible in the code itself, write it down next to the code.
- One `Protocol` per external dependency (`flowers/seams/interfaces.py`), each with an offline Fake
  and a key-gated live adapter. To add an integration, add a seam — don't reach around the broker.
- `ruff check .` clean (config in `pyproject.toml`); no formatter — hand-formatted, line length 100.

## Reporting bugs

An offline reproduction is gold: a scripted-brain test (see `tests/test_web_reliability.py` for the
pattern) that fails on `main` says more than any log. For live-only issues, include the server log and
the run's event stream (`GET /api/runs/{id}/events`).

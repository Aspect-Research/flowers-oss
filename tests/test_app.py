"""Smoke test for the ASGI entrypoint — the single ``build_app`` factory builds and serves offline."""

from __future__ import annotations

from starlette.testclient import TestClient

from flowers.app import build_app


def client_ok(app) -> bool:
    return TestClient(app).get("/api/runs/nope").status_code == 404


def test_app_builds_and_serves_dashboard():
    app = build_app(db_path=":memory:", timers_path=":memory:")
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200 and "flowers" in r.text


def test_app_picks_fakes_offline():
    # Offline (no keys): the app wires the fakes, so the dashboard is inspectable without spending.
    app = build_app(db_path=":memory:", timers_path=":memory:")
    assert client_ok(app)


def test_health_and_ready_endpoints():
    # /health (liveness) + /ready (store reachable) — both 200 on a healthy app.
    client = TestClient(build_app(db_path=":memory:", timers_path=":memory:"))
    h = client.get("/health")
    assert h.status_code == 200 and h.json()["status"] == "ok"
    assert client.get("/ready").status_code == 200


def test_app_has_no_auth_gate():
    # Single-user local surface: the API is reachable with no token (unknown run -> 404, never 401).
    client = TestClient(build_app(db_path=":memory:", timers_path=":memory:"))
    assert client.get("/api/runs/nope").status_code == 404


def test_verify_polling_defaults_and_env_override(monkeypatch):
    from flowers.app import _verify_polling
    # offline (all fakes) stays a single instant check; live tolerates provider read-back lag.
    monkeypatch.delenv("FLOWERS_VERIFY_ATTEMPTS", raising=False)
    monkeypatch.delenv("FLOWERS_VERIFY_DELAY", raising=False)
    assert _verify_polling(live=False) == (1, 0.0)
    attempts, delay = _verify_polling(live=True)
    assert attempts >= 2 and delay > 0.0
    # explicit env overrides win, invalid values fall back rather than crash.
    monkeypatch.setenv("FLOWERS_VERIFY_ATTEMPTS", "6")
    monkeypatch.setenv("FLOWERS_VERIFY_DELAY", "0.5")
    assert _verify_polling(live=False) == (6, 0.5)
    monkeypatch.setenv("FLOWERS_VERIFY_ATTEMPTS", "not-a-number")
    assert _verify_polling(live=True)[0] >= 2   # falls back to the live default

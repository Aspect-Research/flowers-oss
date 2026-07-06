"""Offline tests for the runtime helpers — mainly the dependency-free `.env` loader that makes
`OPENROUTER_API_KEY=...` in a `.env` file actually reach the adapter availability gates."""

from __future__ import annotations

from flowers import runtime


def _write(tmp_path, body: str) -> str:
    p = tmp_path / ".env"
    p.write_text(body, encoding="utf-8")
    return str(p)


def test_load_dotenv_sets_missing_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    path = _write(tmp_path, "OPENROUTER_API_KEY=sk-or-v1-abc\nTAVILY_API_KEY=tvly-xyz\n")
    n = runtime.load_dotenv(path)
    assert n == 2
    assert runtime.env("OPENROUTER_API_KEY") == "sk-or-v1-abc"
    assert runtime.env("TAVILY_API_KEY") == "tvly-xyz"


def test_load_dotenv_does_not_override_real_env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "real-key-wins")
    path = _write(tmp_path, "OPENROUTER_API_KEY=from-dotenv\n")
    runtime.load_dotenv(path)
    assert runtime.env("OPENROUTER_API_KEY") == "real-key-wins"


def test_load_dotenv_ignores_comments_blanks_export_and_quotes(tmp_path, monkeypatch):
    for k in ("A_KEY", "B_KEY", "C_KEY", "D_KEY"):
        monkeypatch.delenv(k, raising=False)
    body = (
        "# a comment\n"
        "\n"
        "A_KEY=plain            # trailing comment stripped when unquoted\n"
        'B_KEY="quoted # kept"\n'
        "export C_KEY=exported\n"
        "not_a_pair_line\n"
        "D_KEY='single'\n"
    )
    path = _write(tmp_path, body)
    runtime.load_dotenv(path)
    assert runtime.env("A_KEY") == "plain"
    assert runtime.env("B_KEY") == "quoted # kept"
    assert runtime.env("C_KEY") == "exported"
    assert runtime.env("D_KEY") == "single"


def test_load_dotenv_missing_file_is_noop(tmp_path):
    assert runtime.load_dotenv(str(tmp_path / "does-not-exist.env")) == 0


def test_adapter_available_offline_and_key_gating(monkeypatch):
    monkeypatch.setenv("FLOWERS_FORCE_OFFLINE", "1")
    monkeypatch.setenv("SOME_KEY", "present")
    assert runtime.adapter_available(key_env="SOME_KEY") is False  # offline pin wins
    monkeypatch.delenv("FLOWERS_FORCE_OFFLINE", raising=False)
    assert runtime.adapter_available(key_env="SOME_KEY") is True
    monkeypatch.delenv("SOME_KEY", raising=False)
    assert runtime.adapter_available(key_env="SOME_KEY") is False


def test_local_user_default_and_override(monkeypatch):
    # Arcade dev mode rejects any user_id that isn't the signed-in Arcade account (user_mismatch),
    # so the per-user identity must be overridable without touching code.
    monkeypatch.delenv("FLOWERS_USER_ID", raising=False)
    assert runtime.local_user() == runtime.LOCAL_USER == "local"
    monkeypatch.setenv("FLOWERS_USER_ID", "owner@example.com")
    assert runtime.local_user() == "owner@example.com"

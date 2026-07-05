"""The ``flowers`` console command — offline, $0 (uvicorn is faked, never actually started)."""

from __future__ import annotations

import builtins
import types

import pytest

import flowers
from flowers.cli import main


def test_version_prints_and_exits_zero(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0
    assert f"flowers {flowers.__version__}" in capsys.readouterr().out


def test_no_command_prints_help_and_exits_2(capsys):
    assert main([]) == 2
    assert "serve" in capsys.readouterr().out


def test_serve_without_web_extra_is_actionable(monkeypatch, capsys):
    real_import = builtins.__import__

    def no_uvicorn(name, *a, **kw):
        if name == "uvicorn":
            raise ImportError("No module named 'uvicorn'")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", no_uvicorn)
    assert main(["serve"]) == 1
    assert 'pip install "flowers[web]"' in capsys.readouterr().err


def test_serve_passes_host_port_and_db_env(monkeypatch):
    recorded = {}
    fake = types.ModuleType("uvicorn")
    fake.run = lambda app, **kw: recorded.update(app=app, **kw)
    monkeypatch.setitem(__import__("sys").modules, "uvicorn", fake)
    monkeypatch.delenv("FLOWERS_DB", raising=False)
    monkeypatch.delenv("FLOWERS_TIMERS_DB", raising=False)

    assert main(["serve", "--host", "0.0.0.0", "--port", "9001",
                 "--db", "x.db", "--timers-db", "y.db"]) == 0
    assert recorded["app"] == "flowers.app:app"
    assert recorded["host"] == "0.0.0.0" and recorded["port"] == 9001
    assert recorded["workers"] == 1   # one process: the timer poller + recovery sweep must be singular
    import os
    assert os.environ["FLOWERS_DB"] == "x.db" and os.environ["FLOWERS_TIMERS_DB"] == "y.db"


def test_version_matches_pyproject():
    # One version, two declarations (pyproject + __init__): pin them together so a release bump
    # can't ship them diverged.
    import tomllib
    from pathlib import Path
    py = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())
    assert py["project"]["version"] == flowers.__version__

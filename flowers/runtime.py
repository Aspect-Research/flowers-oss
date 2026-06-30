"""Tiny runtime helpers shared across seams — mainly the offline switch.

A real adapter is "available" only when (a) we are not forced offline AND (b) its credential is
present. The test suite sets ``FLOWERS_FORCE_OFFLINE=1`` (see the root ``conftest.py``), so every
real adapter reports unavailable and the engine uses the injected fakes. This is the single place
that decides "are we allowed to touch the network," so the contract is impossible to get subtly
wrong per-adapter.
"""

from __future__ import annotations

import os


def force_offline() -> bool:
    """True iff the suite/process has been pinned offline (no live model/tool/network calls)."""
    return bool(os.environ.get("FLOWERS_FORCE_OFFLINE"))


def adapter_available(*, key_env: str) -> bool:
    """Standard availability rule for a live adapter keyed by an env var.

    False when forced offline or when the credential is absent/blank. Adapters call this so the
    "offline by contract" guarantee lives in exactly one place.
    """
    if force_offline():
        return False
    return bool((os.environ.get(key_env) or "").strip())


def env(name: str, default: str = "") -> str:
    """Read a (stripped) environment string, with a default. Centralized for testability."""
    return (os.environ.get(name) or default).strip()


def load_dotenv(path: str = ".env") -> int:
    """Populate ``os.environ`` from a ``.env`` file of ``KEY=VALUE`` lines, if present.

    Dependency-free so a single-user local run "just works" after copying ``.env.example`` to
    ``.env`` (see the README). A variable already set in the real environment always wins — this
    only fills in what's missing — so it never overrides an explicit ``export``. Blank lines,
    ``#`` comments, an optional ``export`` prefix, and surrounding quotes are handled. Returns the
    number of keys set; a missing/unreadable file is a no-op (returns 0), never an error.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return 0
    set_count = 0
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        # Strip a trailing inline comment only when the value is unquoted.
        val = val.strip()
        if val[:1] in {'"', "'"} and val[-1:] == val[:1] and len(val) >= 2:
            val = val[1:-1]
        else:
            val = val.split("#", 1)[0].strip()
        if key and key not in os.environ:
            os.environ[key] = val
            set_count += 1
    return set_count

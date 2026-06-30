"""Regression: the fork-bomb hole in the inherited dangerous-shell guard is closed.

The ported ``_normalize_shell`` strips braces (to defang ``${HOME}``), which previously also hid the
fork bomb's trailing ``{`` from the catastrophic patterns. The brace-preserving fork-bomb check fixes
this without weakening the ``${VAR}`` defense.
"""

from __future__ import annotations

from flowers.seams.sandbox import LocalSubprocessSandbox, is_dangerous_shell


def test_canonical_fork_bomb_is_refused():
    assert is_dangerous_shell(":(){ :|:& };:") is True
    assert is_dangerous_shell("bomb() { bomb | bomb & }; bomb") is True


def test_var_evasion_still_defanged():
    # The brace-stripping defense for ${HOME} must still work (rm -rf ${HOME} is caught).
    assert is_dangerous_shell("rm -rf ${HOME}") is True


def test_legitimate_function_not_flagged():
    # A normal shell function with && (no pipe-to-background) must NOT be a false positive.
    assert is_dangerous_shell("build() { make && test; }; build") is False
    assert is_dangerous_shell("echo hello") is False


def test_sandbox_refuses_fork_bomb_without_executing(tmp_path):
    sb = LocalSubprocessSandbox(workdir=str(tmp_path))
    res = sb.run(":(){ :|:& };:")
    assert res.ok is False and "refused" in res.stderr.lower()
    sb.close()

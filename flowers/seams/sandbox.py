"""The ``Sandbox`` seam — isolated shell/file execution for a run.

The executor's shell and file tools go through THIS seam, never directly at the host: it runs in a
scoped workdir, under an environment stripped of every secret, behind a deterministic
dangerous-shell floor and a path-traversal guard. The model therefore never holds a platform
credential and cannot, by construction, ``rm -rf /`` the host or read a path outside the box.

``LocalSubprocessSandbox`` is the WIRED default (behind the ``flowers.seams.interfaces.Sandbox``
Protocol): it runs commands via ``subprocess`` in a temp workdir it owns. This is the executor's real
box in development; it is not a stub.

The optional ``E2BSandbox`` adapter (a Firecracker microVM whose only egress is the broker) now lives
in ``flowers/extras/sandbox.py`` (an optional template) — a config swap, not a code change.

The safety primitives (``is_dangerous_shell``, ``sanitized_executor_env`` + ``is_secret_env_name``,
``safe_workdir_path``) are ported faithfully from an earlier prototype's worker-confinement
helpers — copied here so the seam carries its own floor with no external dependency.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

from flowers import trustgate
from flowers.seams.interfaces import SandboxResult

# =========================================================================== #
# Executor confinement helpers (ported from an earlier prototype).
# These are the deterministic FLOOR the sandbox enforces: secret-env stripping,
# the catastrophic-shell / secret-exfil denylist, and path-traversal guarding.
# Pure + offline-testable; tighten-only (a True from a guard is a refusal).
# =========================================================================== #

# Platform secrets the EXECUTOR's processes must NEVER hold. The executor runs a shell tool the
# model controls; a prompt-injected ``curl $(env)`` must find no credential. These are explicit
# infrastructure keys (model/provider/integration/sandbox) — stripped from every subprocess env.
_STRIPPED_EXECUTOR_KEYS: tuple[str, ...] = (
    "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "TAVILY_API_KEY", "BRAVE_API_KEY", "SERPER_API_KEY",
    "ARCADE_API_KEY", "COMPOSIO_API_KEY", "STRIPE_API_KEY",
    "E2B_API_KEY", "BROWSERBASE_API_KEY",
    "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY",
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN",
)

# A fixed denylist names only the secrets we KNOW about — a future provider key, an AWS/GH/NPM
# token, or any third-party ``*_API_KEY`` the operator's env happens to hold would otherwise leak
# into the executor subprocess. So in addition to the explicit names above we strip anything whose
# NAME is shaped like a secret. Conservative-by-shape: over-stripping a benign ``*_KEY`` costs the
# executor nothing (tasks pass their own data via files/args), while UNDER-stripping leaks a
# credential — so the asymmetry favors stripping.
_SECRET_NAME_SUFFIXES: tuple[str, ...] = (
    "_API_KEY", "_APIKEY", "_KEY", "_KEY_ID", "_TOKEN", "_SECRET", "_ACCESS_KEY",
    "_PRIVATE_KEY", "_PASSWORD", "_PASSWD", "_CREDENTIALS", "_CREDS",
)
_SECRET_NAME_SUBSTRINGS: tuple[str, ...] = ("SECRET", "PASSWORD", "PASSWD", "PRIVATE_KEY")
_STRIPPED_SET = frozenset(k.upper() for k in _STRIPPED_EXECUTOR_KEYS)


def is_secret_env_name(name: str) -> bool:
    """True iff ``name`` looks like a platform/credential secret the executor must not hold — the
    explicit denylist, or any name shaped like an api-key/token/secret/password/private-key. Pure."""
    up = (name or "").upper()
    if up in _STRIPPED_SET:
        return True
    if any(s in up for s in _SECRET_NAME_SUBSTRINGS):
        return True
    return up.endswith(_SECRET_NAME_SUFFIXES)


def sanitized_executor_env(base: dict | None = None) -> dict:
    """A copy of ``base`` (or ``os.environ``) with EVERY secret-shaped variable stripped — the env
    every executor subprocess runs under, so the model cannot exfiltrate a credential via the shell.
    Strips the explicit platform keys AND any name ``is_secret_env_name`` flags (so a new/unknown
    provider key, AWS/GH/NPM token, etc. can't ride through). Pure; returns a plain dict for
    ``subprocess``."""
    env = dict(os.environ if base is None else base)
    for k in list(env.keys()):
        if is_secret_env_name(k):
            env.pop(k, None)
    return env


# --------------------------------------------------------------------------- #
# The deterministic catastrophic-shell / secret-exfiltration floor.
# The LocalProcess box is not a hard boundary, so this denylist is the FLOOR. Tighten-only and
# conservative — it refuses real damage / secret-exfil, not style. A loopback target (curl/wget
# pointed at the LOCAL box) is exempt, but ONLY when EVERY destination is loopback.
# --------------------------------------------------------------------------- #
_LOOPBACK = (r"(?:https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0)"
             r"|(?:localhost|127\.0\.0\.1|0\.0\.0\.0):\d)")

# Catastrophic + secret-READ patterns matched against the NORMALIZED command (quoting / ${} / a
# trailing ``# localhost`` comment can't slip a target or a decoy past these). The curl/wget
# data-POST exfil case is handled separately by ``_has_remote_exfil`` (needs multi-target reasoning).
_CATASTROPHIC_BASH = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\brm\s+-[a-z]*[rf][a-z]*\s+(/|~|\*|\$home|\.\.)",   # rm -rf / | ~ | * | $HOME | ..
    r"\b(mkfs|fdisk|format)\b",
    r"\bdd\b[^\n]*\bof=/dev/",
    r":\s*\(\s*\)\s*\{",                                   # fork bomb :(){
    r">\s*/dev/sd[a-z]",
    r"\bchmod\s+-[a-z]*R[a-z]*\s+777\s+/",
    r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba)?sh\b",     # curl … | sh  (remote exec)
    r"\bgit\s+push\b[^\n]*--force",
    # Reading / remote-copying a known on-disk secret (the env-strip covers ENV, not DISK).
    r"\b(cat|type|less|more|head|tail|nl|xxd|od|strings|base64|scp|rsync)\b"
    r"[^\n]*(\.env\b(?!\.(?:example|sample|template|dist))|broker_tokens\.json|opencode\.json"
    r"|id_rsa\b|id_ed25519\b|\.pem\b|/\.ssh/|/\.aws/|\.npmrc\b|\.netrc\b)",
))

_CURL_WGET = re.compile(r"\b(curl|wget)\b", re.IGNORECASE)
_EXFIL_FLAG = re.compile(
    r"(\s-d\b|\s-F\b|\s-T\b|--data(?:-[a-z]+)?\b|--form\b|--upload-file\b|--post-data\b|--post-file\b)",
    re.IGNORECASE)
# Tokens curl/wget treat as a DATA file, not a destination (so a ``*.json``/``.env`` payload isn't
# mistaken for a remote host below).
_DATA_FILE_TOKEN = re.compile(r"(@\S+|--(?:post-file|upload-file)=\S+"
                              r"|(?:-T|--upload-file|--post-file)\s+\S+)", re.IGNORECASE)
# A NON-loopback destination: a scheme URL whose host isn't loopback, or a bare dotted host(:port).
_REMOTE_TARGET = re.compile(
    r"https?://(?!(?:localhost|127\.0\.0\.1|0\.0\.0\.0)\b)[^\s/]+"
    r"|(?<![\w@.])(?!localhost\b)(?!127\.0\.0\.1)(?!0\.0\.0\.0)[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::\d+)?",
    re.IGNORECASE)
_LOOPBACK_RE = re.compile(_LOOPBACK, re.IGNORECASE)


def _normalize_shell(text: str) -> str:
    """Defang denylist evasion for the MATCH ONLY (never executed): drop a trailing shell comment
    (a decoy ``# localhost`` must not exempt an exfil), and strip quotes + ``${...}`` braces so
    ``rm -rf "/"``, ``cat '.env'``, ``${HOME}`` can't slip a dangerous target past the patterns."""
    text = re.sub(r"\s#.*$", "", text or "")
    return text.replace('"', "").replace("'", "").replace("{", "").replace("}", "")


def _has_remote_exfil(norm: str) -> bool:
    """True iff ``norm`` is a curl/wget that SENDS data (a -d/--data/upload flag, or a ``$(...)`` /
    backtick that builds its target) to a NON-loopback destination. Exempts the legitimate
    "exercise my own server" case where every destination is loopback — but a remote target present
    ALONGSIDE a loopback decoy is still refused."""
    if not _CURL_WGET.search(norm):
        return False
    has_subst = ("$(" in norm) or ("`" in norm)
    if not (_EXFIL_FLAG.search(norm) or has_subst):
        return False
    # Remove curl/wget DATA-FILE tokens so a ``@payload.json`` / ``@.env`` isn't read as a host.
    cleaned = _DATA_FILE_TOKEN.sub(" ", norm)
    if _REMOTE_TARGET.search(cleaned):
        return True                       # a non-loopback destination + a data send -> exfil
    if has_subst and not _LOOPBACK_RE.search(norm):
        return True                       # ``curl -d @.env $(...)`` building an unproven target
    return False                          # only loopback target(s) -> exempt (own-server testing)


# A fork bomb defines a function whose body pipes into a backgrounded recursion (`:(){ :|:& };:`).
# It must be matched with BRACES PRESERVED — `_normalize_shell` strips braces (to defang `${HOME}`),
# which would also delete the `{` this pattern needs. So we check it on a brace-preserving form.
_FORK_BOMB = re.compile(r"\(\s*\)\s*\{[^}]*\|[^}]*&", re.IGNORECASE)


def _brace_preserving(text: str) -> str:
    """Comment/quote-stripped but BRACE-preserving normalization (for the fork-bomb check only)."""
    text = re.sub(r"\s#.*$", "", text or "")
    return text.replace('"', "").replace("'", "")


def is_dangerous_shell(command: str) -> bool:
    """The deterministic shell FLOOR: True iff ``command`` is a catastrophic action (rm -rf /, fork
    bomb, mkfs, disk-overwrite, ...), a known on-disk-secret read, or a remote data-exfil. Matched
    against the normalized command. Pure + offline-testable; tighten-only (a True is a refusal,
    never an authorization)."""
    norm = _normalize_shell(command)
    if any(p.search(norm) for p in _CATASTROPHIC_BASH):
        return True
    if _FORK_BOMB.search(_brace_preserving(command)):
        return True
    return _has_remote_exfil(norm)


def safe_workdir_path(workdir: str | None, path: str) -> str | None:
    """Resolve ``path`` (relative to ``workdir``, or absolute) and return its realpath IFF it stays
    INSIDE ``workdir``; else None — the file-tool path-traversal guard.

    Closes ``read_file('../../other-user/secret')`` and absolute-escape in the file tools. Uses
    realpath + commonpath for containment; a different-drive path (Windows) is refused."""
    if not workdir:
        return None
    base = os.path.realpath(workdir)
    candidate = path if os.path.isabs(path) else os.path.join(base, path)
    real = os.path.realpath(candidate)
    try:
        if os.path.commonpath([base, real]) == base:
            return real
    except ValueError:  # different drive / mixed forms on Windows -> not under workdir
        return None
    return None


# =========================================================================== #
# LocalSubprocessSandbox — the wired dev default.
# =========================================================================== #

class LocalSubprocessSandbox:
    """The executor's real box in development: a scoped temp workdir, a secret-stripped subprocess
    env, the dangerous-shell floor, and a path-traversal guard. Conforms to the ``Sandbox`` Protocol.

    ``available()`` is always True — it needs no credential. For hard microVM isolation, swap in the
    optional ``E2BSandbox`` adapter (``flowers/extras/sandbox.py``); the local box is fully wired, not a stub.
    """

    def __init__(self, workdir: str | None = None) -> None:
        """Use ``workdir`` if given (caller-owned), else create a fresh temp dir this sandbox OWNS
        and removes on ``close()``."""
        if workdir:
            self._workdir = os.path.realpath(workdir)
            os.makedirs(self._workdir, exist_ok=True)
            self._owns_workdir = False
        else:
            self._workdir = os.path.realpath(tempfile.mkdtemp(prefix="flowers-sbx-"))
            self._owns_workdir = True

    def available(self) -> bool:
        """Always available — the local box needs no credential."""
        return True

    def workdir(self) -> str:
        """The absolute path of this sandbox's scoped working directory."""
        return self._workdir

    def run(self, command: str, *, timeout: float = 60.0) -> SandboxResult:
        """Execute ``command`` via the shell in the workdir, under a secret-stripped env.

        Refuses (without executing) any command the dangerous-shell floor flags, returning
        ``ok=False`` with a ``refused: ...`` stderr. Captures stdout/stderr/exit_code; a timeout
        returns ``ok=False``. ``ok`` is True iff the process ran to completion with exit code 0.
        """
        if is_dangerous_shell(command):
            return SandboxResult(
                ok=False,
                stderr=f"refused: dangerous shell command blocked by the sandbox floor: {command!r}",
                exit_code=1,
            )
        env = sanitized_executor_env()
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=self._workdir,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
            err = exc.stderr or ""
            if isinstance(out, bytes):
                out = out.decode("utf-8", errors="replace")
            if isinstance(err, bytes):
                err = err.decode("utf-8", errors="replace")
            return SandboxResult(
                ok=False,
                stdout=out,
                stderr=(err + f"\nrefused: command timed out after {timeout}s").strip(),
                exit_code=124,
            )
        return SandboxResult(
            ok=(proc.returncode == 0),
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            exit_code=proc.returncode,
        )

    def write_file(self, relpath: str, content: str) -> None:
        """Write ``content`` to ``relpath`` within the workdir. Rejects any path that escapes the
        workdir (path traversal / absolute-escape) with a ValueError."""
        target = safe_workdir_path(self._workdir, relpath)
        if target is None:
            raise ValueError(f"refused: path escapes the sandbox workdir: {relpath!r}")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(content)

    def read_file(self, relpath: str) -> str:
        """Read and return the text of ``relpath`` within the workdir. Rejects path traversal /
        absolute-escape with a ValueError; a missing file raises FileNotFoundError."""
        target = safe_workdir_path(self._workdir, relpath)
        if target is None:
            raise ValueError(f"refused: path escapes the sandbox workdir: {relpath!r}")
        with open(target, encoding="utf-8") as f:
            return f.read()

    def list_files(self) -> list[str]:
        """Every regular file under the workdir, as sorted workdir-relative POSIX-style paths."""
        out: list[str] = []
        for dirpath, _dirnames, filenames in os.walk(self._workdir):
            for fname in filenames:
                rel = os.path.relpath(os.path.join(dirpath, fname), self._workdir)
                out.append(rel.replace(os.sep, "/"))
        return sorted(out)

    def snapshot(self) -> dict:
        """A ``{relpath: hash}`` snapshot of the workdir — the box-observation baseline used to
        detect staleness/drift. Delegates to ``flowers.trustgate.snapshot_dir``."""
        return trustgate.snapshot_dir(self._workdir)

    def close(self) -> None:
        """Remove the temp workdir IFF this sandbox created (owns) it; a caller-supplied workdir is
        left untouched. Idempotent."""
        if self._owns_workdir and os.path.isdir(self._workdir):
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._owns_workdir = False

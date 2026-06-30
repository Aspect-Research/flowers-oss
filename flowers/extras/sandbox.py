"""Optional sandbox adapter — E2B Firecracker microVM.

``E2BSandbox`` is an optional adapter template (not wired into the default ``build_app``; the wired
default is ``LocalSubprocessSandbox`` in ``flowers/seams/sandbox.py``). The executor's shell/file tools
run INSIDE an E2B microVM (one per run), so a prompt-injected command is isolated from the host (the
microVM is the strong boundary; its only intended egress is the broker). Same ``Sandbox`` Protocol as the
local default — a config swap — and the deterministic dangerous-shell floor still applies (defense in
depth). Gated on ``E2B_API_KEY`` (unavailable offline); ``e2b`` is imported lazily so the core imports
without it. To use this, pass an ``E2BSandbox`` factory to the Operator's ``sandbox_factory`` and install
the ``e2b`` SDK.
"""

from __future__ import annotations

from flowers import runtime
from flowers.seams.interfaces import SandboxResult
from flowers.seams.sandbox import is_dangerous_shell


class E2BSandbox:
    """Stronger-isolation sandbox — an E2B Firecracker microVM (one per run). The executor's shell/file tools
    run INSIDE the VM, so a prompt-injected command is isolated from the host (the microVM is the strong
    boundary; its only intended egress is the broker). Same ``Sandbox`` Protocol as
    :class:`LocalSubprocessSandbox` — a config swap — and the deterministic dangerous-shell floor +
    path-traversal guard still apply (defense in depth). Gated on ``E2B_API_KEY`` (unavailable offline);
    ``e2b`` is imported lazily so the stdlib core imports without it. The VM is created lazily on first
    use and KILLED in ``close()`` so a per-run VM is never leaked (nor the budget drained).
    """

    _KEY_ENV = "E2B_API_KEY"
    _WORKDIR = "/home/user/flowers"

    def __init__(self, api_key: str | None = None, *, vm_timeout: int = 600) -> None:
        self._api_key = api_key or runtime.env(self._KEY_ENV)
        self._vm_timeout = vm_timeout
        self._sb = None

    def available(self) -> bool:
        """True only when not forced offline AND ``E2B_API_KEY`` is present (False offline)."""
        return runtime.adapter_available(key_env=self._KEY_ENV)

    def _require_available(self) -> None:
        if not self.available():
            raise RuntimeError("E2BSandbox unavailable (offline or E2B_API_KEY missing); "
                               "use LocalSubprocessSandbox in dev/tests")

    def _sandbox(self):
        if self._sb is None:
            self._require_available()
            from e2b import Sandbox
            self._sb = Sandbox.create(api_key=self._api_key, timeout=self._vm_timeout)
            self._sb.commands.run(f"mkdir -p {self._WORKDIR}", timeout=30)
        return self._sb

    def workdir(self) -> str:
        return self._WORKDIR

    def _remote(self, relpath: str) -> str:
        rel = str(relpath).replace("\\", "/").lstrip("/")
        if not rel or ".." in rel.split("/"):
            raise ValueError(f"refused: path escapes the sandbox workdir: {relpath!r}")
        return f"{self._WORKDIR}/{rel}"

    def run(self, command: str, *, timeout: float = 60.0) -> SandboxResult:
        """Run a MODEL command in the VM under the dangerous-shell floor. ``commands.run`` raises a
        ``CommandExitException`` on a nonzero exit (it carries exit_code/stdout/stderr) and raises on a
        timeout / dead VM — both map to an honest non-ok SandboxResult, never a crash."""
        if is_dangerous_shell(command):
            return SandboxResult(
                ok=False,
                stderr=f"refused: dangerous shell command blocked by the sandbox floor: {command!r}",
                exit_code=1)
        sb = self._sandbox()   # raises offline (unavailable) BEFORE the try -> an offline call refuses
        try:
            r = sb.commands.run(command, cwd=self._WORKDIR, timeout=int(timeout))
            return SandboxResult(ok=(r.exit_code == 0), stdout=r.stdout or "",
                                 stderr=r.stderr or "", exit_code=r.exit_code)
        except Exception as e:  # noqa: BLE001 - a command/VM failure is a result, not a crash
            ec = getattr(e, "exit_code", None)
            return SandboxResult(
                ok=False, stdout=getattr(e, "stdout", "") or "",
                stderr=(getattr(e, "stderr", "") or f"{type(e).__name__}: {e}"),
                exit_code=ec if isinstance(ec, int) else 124)

    def write_file(self, relpath: str, content: str) -> None:
        self._sandbox().files.write(self._remote(relpath), content)

    def read_file(self, relpath: str) -> str:
        return self._sandbox().files.read(self._remote(relpath))

    def _exec(self, command: str, timeout: int = 60):
        """An INTERNAL command (find/sha256sum) that bypasses the model-facing dangerous-shell floor."""
        try:
            return self._sandbox().commands.run(command, cwd=self._WORKDIR, timeout=timeout)
        except Exception:
            return None

    def list_files(self) -> list[str]:
        self._require_available()
        r = self._exec(f"find {self._WORKDIR} -type f")
        base = self._WORKDIR.rstrip("/") + "/"
        out = [ln[len(base):] for ln in ((r.stdout if r else "") or "").splitlines() if ln.startswith(base)]
        return sorted(p for p in out if p)

    def snapshot(self) -> dict:
        """A ``{relpath: sha256}`` snapshot computed INSIDE the VM (sha256sum). Self-consistent: the
        operator compares a baseline snapshot to a later one — both from this sandbox — for box drift."""
        self._require_available()
        r = self._exec(f"find {self._WORKDIR} -type f -exec sha256sum {{}} +", timeout=120)
        base = self._WORKDIR.rstrip("/") + "/"
        out: dict[str, str] = {}
        for ln in ((r.stdout if r else "") or "").splitlines():
            parts = ln.split(None, 1)
            if len(parts) == 2 and parts[1].startswith(base):
                out[parts[1][len(base):]] = parts[0]
        return out

    def close(self) -> None:
        if self._sb is not None:
            try:
                self._sb.kill()
            except Exception:
                pass
            self._sb = None

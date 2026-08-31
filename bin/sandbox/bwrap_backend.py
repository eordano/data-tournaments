"""bwrap sandbox backend — local jail for trusted-tier, offline workloads.

Maps a SandboxRunRequest onto scripts/lint/lint-jail.sh ``exec``: each
command runs inside the proven bubblewrap jail (read-only repo and /nix,
no network namespace, loopback up, toolchain pinned by a flake) with a
per-run writable workspace under .lint-jail-cache/runs/. This is the fast
inner-loop substrate (~seconds vs microVM boot); its isolation is weaker
than e2b/microvm (shared kernel, host /nix and /etc visible read-only), so
profiles for UNTRUSTED code must keep using those substrates.

Environment mapping:
* profile.flake_ref, when set, is built with ``nix build`` and passed to
  the jail as TOOLCHAIN — the ref must evaluate to an env with bin/ (e.g.
  ``path:scripts/lint#toolchain``). Empty means the jail resolves its own
  default toolchain flake.
* The jail is ALWAYS offline; a profile asking for egress (deny_all=False)
  is refused rather than silently degraded, mirroring the e2b secrets rule.
* Secrets: only exposure='env' refs are resolved (from the worker's
  environment); 'egress-proxy' exposure needs the microvm runner's proxy
  and raises here.
* profile.repo + base_commit pin the workspace via a host-side
  ``git clone`` + ``checkout`` before the jail runs (the jail itself has
  no network); local paths and URLs both work as clone sources.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from bin.sandbox.backend import (
    CommandResult,
    SandboxBackend,
    SandboxRunRequest,
    SandboxRunResult,
)

_OUTPUT_TAIL = 4000
_REPO_ROOT = Path(__file__).resolve().parents[2]
_JAIL = _REPO_ROOT / "scripts" / "lint" / "lint-jail.sh"

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _subprocess_runner(argv, env, timeout, cwd=None):
    proc = subprocess.run(
        argv,
        env=env,
        timeout=timeout,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    return proc.returncode, proc.stdout or ""

class BwrapSandboxBackend(SandboxBackend):
    name = "bwrap"

    def __init__(self, runner=None, repo_root=None):
        """``runner`` is injectable for tests: callable
        ``(argv, env, timeout, cwd) -> (exit_code, output)``. Production
        validates the platform and the jail script up front."""
        self._root = Path(repo_root) if repo_root else _REPO_ROOT
        self._jail = self._root / "scripts" / "lint" / "lint-jail.sh"
        if runner is not None:
            self._runner = runner
            return
        if not sys.platform.startswith("linux"):
            raise RuntimeError(
                "bwrap backend requires Linux user namespaces; use the "
                "e2b or microvm backend on this platform"
            )
        if not self._jail.is_file():
            raise RuntimeError(f"bwrap backend requires {self._jail}")
        self._runner = _subprocess_runner

    def _toolchain_env(self, flake_ref: str) -> dict:
        env = dict(os.environ)
        if flake_ref:
            code, out = self._runner(
                ["nix", "build", "--no-link", "--print-out-paths", flake_ref],
                env,
                600,
                None,
            )
            if code != 0 or not out.strip():
                raise RuntimeError(
                    f"bwrap backend: nix build of flake_ref {flake_ref!r} "
                    f"failed: {out[-_OUTPUT_TAIL:]}"
                )
            env["TOOLCHAIN"] = out.strip().splitlines()[-1]
        return env

    def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        profile = request.profile
        if not profile.egress.deny_all:
            raise RuntimeError(
                "bwrap backend is offline-only (unshared netns); profile "
                f"{profile.name!r} requests egress — use the e2b or microvm "
                "backend instead of degrading the policy"
            )
        for secret in profile.secrets:
            if secret.exposure != "env":
                raise RuntimeError(
                    f"secret {secret.ref} requires exposure="
                    f"{secret.exposure!r}, which needs the self-hosted "
                    "egress proxy (microvm backend); refusing to degrade"
                )

        env = self._toolchain_env(profile.flake_ref)
        for secret in profile.secrets:
            env_name = secret.ref.rsplit("/", 1)[-1].upper().replace("-", "_")
            value = os.environ.get(env_name)
            if value is None:
                raise RuntimeError(
                    f"secret {secret.ref}: {env_name} not present in the "
                    "worker environment at run time"
                )
            env[env_name] = value

        runs_dir = self._root / ".lint-jail-cache" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        workspace = tempfile.mkdtemp(prefix="run.", dir=runs_dir)

        started = _now()
        results: list[CommandResult] = []
        error = ""
        try:
            setup = []
            if profile.repo and profile.base_commit:
                url = profile.repo
                if url.startswith("github.com:"):
                    url = "https://github.com/" + url.split(":", 1)[1]
                setup = [
                    ["git", "clone", "--quiet", url, workspace],
                    ["git", "-C", workspace, "checkout", "--quiet",
                     profile.base_commit],
                ]
            for argv in setup:
                code, out = self._runner(
                    argv, env, profile.timeout_seconds, None
                )
                results.append(
                    CommandResult(
                        command=" ".join(argv),
                        exit_code=code,
                        output_tail=out[-_OUTPUT_TAIL:],
                    )
                )
                if code != 0:
                    error = f"workspace pin failed: {' '.join(argv)}"
                    break
            if not error:
                for cmd in request.commands:
                    code, out = self._runner(
                        [str(self._jail), "exec", workspace, "bash", "-c", cmd],
                        env,
                        profile.timeout_seconds,
                        str(self._root),
                    )
                    results.append(
                        CommandResult(
                            command=cmd,
                            exit_code=code,
                            output_tail=out[-_OUTPUT_TAIL:],
                        )
                    )
        except Exception as e:
            error = error or f"{type(e).__name__}: {e}"
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

        return SandboxRunResult(
            backend=self.name,
            profile_digest=profile.digest,
            workorder_id=request.workorder_id,
            pack_digest=request.pack_digest,
            started_at=started,
            finished_at=_now(),
            commands=tuple(results),
            artifacts={},
            ok=(not error) and all(r.exit_code == 0 for r in results),
            error=error,
        )

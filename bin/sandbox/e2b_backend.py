"""E2B sandbox backend — managed Firecracker microVMs (pilot substrate).

Chosen per docs/research/sandbox-execution-options-2026.md as the managed
fallback/pilot: real deny-all + domain-allowlist egress, workload identity,
pause/fork, Apache-2.0 infra. This backend is deliberately THIN: it maps a
SandboxRunRequest onto an E2B sandbox pinned to the profile's base commit
and returns the typed result.

Gating: requires the ``e2b_code_interpreter`` (or ``e2b``) package AND
E2B_API_KEY in the environment. Absent either, get_backend('e2b') raises a
clear RuntimeError — a weaker substrate is never silently substituted.

Egress mapping: profile.egress.deny_all -> E2B internet access disabled;
allow_domains -> E2B allowed-domains list. Secrets: only exposure='env'
SecretRefs are resolved (from the worker's environment, per-step) and
injected as env vars; 'egress-proxy' exposure requires the self-hosted
proxy (microvm runner) and raises here rather than degrade.
"""
from __future__ import annotations

import os
import shlex
from datetime import datetime, timezone

from bin.sandbox.backend import (
    CommandResult,
    SandboxBackend,
    SandboxRunRequest,
    SandboxRunResult,
)

_OUTPUT_TAIL = 4000


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class E2BSandboxBackend(SandboxBackend):
    name = "e2b"

    def __init__(self, sandbox_factory=None):
        """``sandbox_factory`` is injectable for tests; production resolves
        the real E2B SDK lazily and validates credentials."""
        if sandbox_factory is not None:
            self._factory = sandbox_factory
            return
        if not os.environ.get("E2B_API_KEY"):
            raise RuntimeError(
                "e2b backend requires E2B_API_KEY in the environment "
                "(never store it in profiles or work orders)"
            )
        try:
            from e2b_code_interpreter import Sandbox  # type: ignore
        except ImportError:
            try:
                from e2b import Sandbox  # type: ignore
            except ImportError as e:
                raise RuntimeError(
                    "e2b backend requires the 'e2b-code-interpreter' (or "
                    "'e2b') package; install it in the worker environment"
                ) from e
        self._factory = Sandbox

    def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        profile = request.profile
        for secret in profile.secrets:
            if secret.exposure != "env":
                raise RuntimeError(
                    f"secret {secret.ref} requires exposure="
                    f"{secret.exposure!r}, which needs the self-hosted "
                    "egress proxy (microvm backend); refusing to degrade"
                )

        envs = {}
        for secret in profile.secrets:
            env_name = secret.ref.rsplit("/", 1)[-1].upper().replace("-", "_")
            value = os.environ.get(env_name)
            if value is None:
                raise RuntimeError(
                    f"secret {secret.ref}: {env_name} not present in the "
                    "worker environment at run time"
                )
            envs[env_name] = value

        kwargs: dict = {"envs": envs} if envs else {}
        if profile.egress.deny_all:
            kwargs["allow_internet_access"] = False
        elif profile.egress.allow_domains:
            kwargs["allowed_domains"] = list(profile.egress.allow_domains)

        started = _now()
        results: list[CommandResult] = []
        error = ""
        try:
            with self._factory.create(
                timeout=profile.timeout_seconds, **kwargs
            ) as sandbox:
                workdir = "/workspace"
                setup = []
                if profile.repo and profile.base_commit:
                    # Pin the workspace to the profile's base commit.
                    url = profile.repo
                    if url.startswith("github.com:"):
                        url = "https://github.com/" + url.split(":", 1)[1]
                    setup = [
                        f"git clone --quiet {shlex.quote(url)} {workdir}",
                        f"git -C {workdir} checkout --quiet "
                        f"{shlex.quote(profile.base_commit)}",
                    ]
                for cmd in (*setup, *request.commands):
                    proc = sandbox.commands.run(
                        cmd, cwd=workdir, timeout=profile.timeout_seconds
                    )
                    out = (proc.stdout or "") + (proc.stderr or "")
                    results.append(
                        CommandResult(
                            command=cmd,
                            exit_code=proc.exit_code,
                            output_tail=out[-_OUTPUT_TAIL:],
                        )
                    )
                    if proc.exit_code != 0 and cmd in setup:
                        error = f"workspace pin failed: {cmd}"
                        break
        except Exception as e:  # infrastructure failure, not command failure
            error = error or f"{type(e).__name__}: {e}"

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

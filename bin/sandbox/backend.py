"""Sandbox backends: pluggable execution substrates behind one interface.

The interface is deliberately tiny: submit a run request, get a typed
result. Real backends (e2b, microvm) launch isolated environments pinned to
the profile's (flake_ref, base_commit); the fake backend is deterministic
and network-free so workflow/activity tests and dry-runs exercise the exact
same code path as production.
"""
from __future__ import annotations

import abc
from datetime import datetime, timezone

import pydantic

from bin.sandbox.profile import SandboxProfile

class SandboxRunRequest(pydantic.BaseModel):
    """One bounded execution inside a sandbox."""

    model_config = pydantic.ConfigDict(frozen=True)

    profile: SandboxProfile
    workorder_id: str = ""
    pack_digest: str = ""
    commands: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()

class CommandResult(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True)

    command: str
    exit_code: int
    output_tail: str = ""

class SandboxRunResult(pydantic.BaseModel):
    """Typed outcome. Failure is a VALID outcome — never massaged."""

    model_config = pydantic.ConfigDict(frozen=True)

    backend: str
    profile_digest: str
    workorder_id: str = ""
    pack_digest: str = ""
    started_at: str = ""
    finished_at: str = ""
    commands: tuple[CommandResult, ...] = ()
    artifacts: dict[str, str] = {}
    ok: bool = False
    error: str = ""

class SandboxBackend(abc.ABC):
    """Interface every execution substrate implements."""

    name: str = "abstract"

    @abc.abstractmethod
    def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        """Execute the request; must never raise for command failures
        (those are encoded in the result), only for infrastructure
        errors (cannot launch, cannot pin commit...)."""

class FakeSandboxBackend(SandboxBackend):
    """Deterministic, network-free backend for tests and dry-runs.

    Behavior contract:
    * commands containing the substring ``FAIL`` exit 1, others exit 0
    * output is derived only from the command text and profile digest, so
      identical requests yield identical results (modulo timestamps, which
      callers may pin via ``clock``)
    """

    name = "fake"

    def __init__(self, clock=None):
        self._clock = clock or (
            lambda: datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )

    def run(self, request: SandboxRunRequest) -> SandboxRunResult:
        started = self._clock()
        results = []
        for cmd in request.commands:
            code = 1 if "FAIL" in cmd else 0
            results.append(
                CommandResult(
                    command=cmd,
                    exit_code=code,
                    output_tail=f"[fake:{request.profile.digest[:12]}] {cmd} -> {code}",
                )
            )
        finished = self._clock()
        return SandboxRunResult(
            backend=self.name,
            profile_digest=request.profile.digest,
            workorder_id=request.workorder_id,
            pack_digest=request.pack_digest,
            started_at=started,
            finished_at=finished,
            commands=tuple(results),
            artifacts={},
            ok=all(r.exit_code == 0 for r in results),
            error="",
        )

_BACKENDS: dict[str, type[SandboxBackend]] = {"fake": FakeSandboxBackend}

def register_backend(name: str, cls: type[SandboxBackend]) -> None:
    _BACKENDS[name] = cls

def get_backend(name: str) -> SandboxBackend:
    """Instantiate a backend by name.

    'e2b' and 'microvm' register lazily when their modules are importable
    and configured; unknown/unconfigured names raise with a clear message
    instead of silently falling back to a weaker substrate.
    """
    if name == "e2b" and name not in _BACKENDS:
        try:  # pragma: no cover - exercised only with e2b installed
            from bin.sandbox.e2b_backend import E2BSandboxBackend

            register_backend("e2b", E2BSandboxBackend)
        except ImportError as e:
            raise RuntimeError(
                "e2b backend requested but not available: install the 'e2b' "
                "extra and set E2B_API_KEY"
            ) from e
    if name == "bwrap" and name not in _BACKENDS:
        from bin.sandbox.bwrap_backend import BwrapSandboxBackend

        register_backend("bwrap", BwrapSandboxBackend)
    if name not in _BACKENDS:
        raise RuntimeError(
            f"unknown sandbox backend {name!r}; available: {sorted(_BACKENDS)}"
        )
    return _BACKENDS[name]()

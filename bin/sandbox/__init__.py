"""Sandbox execution plane: profiles, backends, and preflight reports.

Wave 5 of docs/plans/unity-explorer-release-platform.md. The design rules
come from docs/research/sandbox-execution-options-2026.md:

* Sandbox identity is reproducible: (flake_ref/lock digest, repo commit).
* Egress is deny-by-default; allowlists are explicit and auditable.
* Secrets are referenced by NAME only (``secret://scope/name``) and resolved
  at the execution boundary (activity / egress proxy) — plaintext never
  enters profiles, work orders, packs, logs, or Temporal history.
* Backends are pluggable: a deterministic fake for tests/dry-runs, E2B as
  the managed pilot (env-gated), microvm.nix Linux runners later.
"""
from bin.sandbox.backend import (
    FakeSandboxBackend,
    SandboxBackend,
    SandboxRunRequest,
    SandboxRunResult,
    get_backend,
)
from bin.sandbox.profile import EgressPolicy, SandboxProfile, SecretRef
from bin.sandbox.report import preflight_evidence

__all__ = [
    "EgressPolicy",
    "FakeSandboxBackend",
    "SandboxBackend",
    "SandboxProfile",
    "SandboxRunRequest",
    "SandboxRunResult",
    "SecretRef",
    "get_backend",
    "preflight_evidence",
]

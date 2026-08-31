"""Sandbox profiles: reproducible identity + deny-by-default policy.

A profile is a frozen, content-addressed description of WHERE and UNDER WHAT
RULES a work order may execute. It carries no runtime state and no secret
values (names only).
"""
from __future__ import annotations

import re

import pydantic

from bin.landscape.canonical import content_digest

_SECRET_REF_RE = re.compile(r"^secret://[a-z0-9][a-z0-9\-]*/[a-z0-9][a-z0-9\-_.]*$")

class SecretRef(pydantic.BaseModel):
    """A named secret, resolved only at the execution boundary.

    ``ref`` looks like ``secret://unity-cloud/api-key``. The value is NEVER
    part of this model; anything that looks like a literal secret is
    rejected so a profile cannot smuggle plaintext.
    """

    model_config = pydantic.ConfigDict(frozen=True)

    ref: str
    exposure: str = "egress-proxy"

    @pydantic.field_validator("ref")
    @classmethod
    def _ref_shape(cls, v: str) -> str:
        if not _SECRET_REF_RE.match(v):
            raise ValueError(
                "secret ref must look like secret://<scope>/<name> "
                "(lowercase slug segments); literal secret values are not "
                "allowed here"
            )
        return v

    @pydantic.field_validator("exposure")
    @classmethod
    def _exposure_known(cls, v: str) -> str:
        if v not in ("egress-proxy", "env"):
            raise ValueError("exposure must be 'egress-proxy' or 'env'")
        return v

class EgressPolicy(pydantic.BaseModel):
    """Deny-by-default network policy. Only named destinations are reachable."""

    model_config = pydantic.ConfigDict(frozen=True)

    allow_domains: tuple[str, ...] = ()
    allow_cidrs: tuple[str, ...] = ()
    deny_all: bool = False

    @pydantic.field_validator("allow_domains")
    @classmethod
    def _domains_are_bare(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for d in v:
            if "://" in d or "/" in d:
                raise ValueError(
                    f"allow_domains entries are bare hostnames, got {d!r}"
                )
        return tuple(sorted(set(v)))

    @pydantic.model_validator(mode="after")
    def _deny_all_means_empty(self) -> "EgressPolicy":
        if self.deny_all and (self.allow_domains or self.allow_cidrs):
            raise ValueError("deny_all=True cannot combine with allowlists")
        return self

class SandboxProfile(pydantic.BaseModel):
    """Reproducible sandbox identity + resource budget + policy.

    Identity = (flake_ref, repo, base_commit): two runs with the same
    profile digest see byte-identical inputs.
    """

    model_config = pydantic.ConfigDict(frozen=True)

    name: str
    backend: str
    flake_ref: str = ""
    repo: str = ""
    base_commit: str = ""
    read_only: bool = True
    egress: EgressPolicy = EgressPolicy(deny_all=True)
    secrets: tuple[SecretRef, ...] = ()
    cpu_cores: int = 2
    memory_mib: int = 2048
    timeout_seconds: int = 1800

    @pydantic.field_validator("backend")
    @classmethod
    def _backend_known(cls, v: str) -> str:
        if v not in ("fake", "e2b", "microvm", "bwrap"):
            raise ValueError("backend must be one of: fake, e2b, microvm, bwrap")
        return v

    @pydantic.model_validator(mode="after")
    def _write_needs_pinned_commit(self) -> "SandboxProfile":
        if not self.read_only and not self.base_commit:
            raise ValueError("write-capable profiles require base_commit")
        return self

    @property
    def digest(self) -> str:
        return content_digest(self.model_dump())

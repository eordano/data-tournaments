"""Evidence-source adapters: system code that turns raw sources into frozen
``EvidenceRef`` contracts.

Each adapter is a module exposing::

    collect(config: dict, *, why: str, limits: dict | None = None)
        -> list[EvidenceRef]

plus adapter-specific helpers. Trust tiers are assigned HERE, by system code
— callers never pass tier strings. Adapters import the frozen contracts from
``bin.landscape`` and never mutate them.
"""
from __future__ import annotations

from types import ModuleType

from bin.landscape.adapters import (
    bugsweep_corpus,
    dedup_lists,
    git_local,
    github_api,
    github_autoclosed,
    sentry_csv,
    slack_csv,
    unity_cloud,
)
from bin.landscape.adapters.build_snapshot import assemble_snapshot

_REGISTRY: dict[str, ModuleType] = {
    "bugsweep_corpus": bugsweep_corpus,
    "dedup_lists": dedup_lists,
    "git_local": git_local,
    "github_api": github_api,
    "github_autoclosed": github_autoclosed,
    "sentry_csv": sentry_csv,
    "slack_csv": slack_csv,
    "unity_cloud": unity_cloud,
}


def adapter_kinds() -> tuple[str, ...]:
    """Registered adapter kinds, sorted for stable display."""
    return tuple(sorted(_REGISTRY))


def get_adapter(kind: str) -> ModuleType:
    """Return the adapter module for ``kind``.

    Raises ``KeyError`` with the known kinds listed — a typo should fail
    loudly, not fall back to some default source.
    """
    try:
        return _REGISTRY[kind]
    except KeyError:
        raise KeyError(
            f"unknown adapter kind {kind!r}; known kinds: "
            + ", ".join(adapter_kinds())
        ) from None


__all__ = [
    "adapter_kinds",
    "assemble_snapshot",
    "bugsweep_corpus",
    "dedup_lists",
    "get_adapter",
    "git_local",
    "github_api",
    "github_autoclosed",
    "sentry_csv",
    "slack_csv",
]

"""LandscapeSnapshot: an immutable, content-addressed selection of evidence
for a project at a point in time.

The snapshot is the "run manifest" for context assembly: everything a
ContextPack is built from must appear here first. Evidence is normalized to a
deterministic order (sorted by content digest) at validation time, so two
snapshots built from the same refs in different insertion orders are
identical objects with identical digests.
"""
from __future__ import annotations

from typing import Any

import pydantic

from bin.landscape.canonical import content_digest
from bin.landscape.evidence import EvidenceRef
from bin.workorder import RepoSnapshot

class FrozenRepoSnapshot(RepoSnapshot):
    """RepoSnapshot (reused from bin.workorder) frozen for use inside
    immutable landscape artifacts."""

    model_config = pydantic.ConfigDict(frozen=True)

class LandscapeSnapshot(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True)

    project: str
    created_at: str = ""
    evidence: tuple[EvidenceRef, ...] = ()
    repos: tuple[FrozenRepoSnapshot, ...] = ()

    @pydantic.field_validator("project")
    @classmethod
    def _nonempty_project(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("project must be non-empty")
        return v

    @pydantic.field_validator("evidence")
    @classmethod
    def _normalize_evidence(
        cls, v: tuple[EvidenceRef, ...]
    ) -> tuple[EvidenceRef, ...]:
        """Deterministic order + de-duplication by content digest, so
        construction order can never leak into the snapshot identity."""
        unique = {ref.digest: ref for ref in v}
        return tuple(unique[d] for d in sorted(unique))

    def _content_payload(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "created_at": self.created_at,
            "evidence": [ref.digest for ref in self.evidence],
            "repos": sorted(
                (repo.model_dump() for repo in self.repos),
                key=lambda r: (r["root"], r["remote"], r["base_commit"]),
            ),
        }

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def digest(self) -> str:
        """Content digest over canonical JSON. Evidence contributes via its
        own content digests, so any excerpt/uri/tier change propagates."""
        return content_digest(self._content_payload())

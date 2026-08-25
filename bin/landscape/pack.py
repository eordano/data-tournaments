"""Role-shaped ContextPacks.

A ContextPack is the immutable projection of a LandscapeSnapshot for one
role. The role-shaping policy is the security boundary of the platform:

* EXECUTOR — runs tools. Tier-3 (external, untrusted) evidence is EXCLUDED
  entirely: untrusted text must never sit in the context of the role that
  can act (prompt-injection containment).
* JUDGE — compares artifacts. Sees tier-3 evidence but every tier-3 ref is
  listed in ``flagged_evidence_ids`` so the judge prompt can mark it as
  untrusted.
* CREATOR — drafts work orders. Sees everything; tier labels ride along on
  each EvidenceRef (``trust_tier``), and ``tier_labels`` gives an id → tier
  index for prompt rendering.

Packs carry NO approval or authorization state — approvals belong to the
workflow runtime (Temporal signals), never to the context artifact.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

import pydantic

from bin.landscape.canonical import content_digest
from bin.landscape.evidence import EvidenceRef, TrustTier
from bin.landscape.snapshot import LandscapeSnapshot


class Role(str, Enum):
    CREATOR = "creator"
    JUDGE = "judge"
    EXECUTOR = "executor"


class ContextPack(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True)

    project: str
    role: Role
    snapshot_digest: str  # the snapshot this pack was projected from
    created_at: str = ""
    evidence: tuple[EvidenceRef, ...] = ()
    # Tier-3 refs the consumer must present as untrusted (JUDGE packs).
    flagged_evidence_ids: tuple[str, ...] = ()

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def tier_labels(self) -> dict[str, str]:
        """Evidence id → trust tier, for prompt rendering."""
        return {ref.id: ref.trust_tier.value for ref in self.evidence}

    def _content_payload(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "role": self.role.value,
            "snapshot_digest": self.snapshot_digest,
            "created_at": self.created_at,
            "evidence": [ref.digest for ref in self.evidence],
            "flagged_evidence_ids": sorted(self.flagged_evidence_ids),
        }

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def digest(self) -> str:
        return content_digest(self._content_payload())


def build_pack(
    snapshot: LandscapeSnapshot, role: Role, *, created_at: str = ""
) -> ContextPack:
    """Project ``snapshot`` into an immutable pack for ``role``.

    This factory is the ONLY sanctioned way to build a pack from a snapshot;
    it enforces the role-shaping policy described in the module docstring.
    """
    role = Role(role)
    if role is Role.EXECUTOR:
        evidence = tuple(
            ref
            for ref in snapshot.evidence
            if ref.trust_tier is not TrustTier.TIER3_EXTERNAL
        )
        flagged: tuple[str, ...] = ()
    elif role is Role.JUDGE:
        evidence = snapshot.evidence
        flagged = tuple(
            ref.id
            for ref in snapshot.evidence
            if ref.trust_tier is TrustTier.TIER3_EXTERNAL
        )
    else:  # CREATOR: everything, tiers visible via tier_labels / trust_tier
        evidence = snapshot.evidence
        flagged = ()
    return ContextPack(
        project=snapshot.project,
        role=role,
        snapshot_digest=snapshot.digest,
        created_at=created_at or snapshot.created_at,
        evidence=evidence,
        flagged_evidence_ids=flagged,
    )

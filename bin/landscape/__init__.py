"""Typed context contracts for the project-landscape platform (plan phase 0b).

Pure data contracts: no network, no persistence, no LM coupling. Everything
here is frozen (immutable) and content-addressed — identical content yields
identical digests regardless of construction order.

Import direction rule: ``bin.landscape`` may import from ``bin.workorder``
(RepoSnapshot, WorkOrderLink), never the other way around.
"""
from bin.landscape.canonical import canonical_json, content_digest
from bin.landscape.evidence import (
    MAX_EXCERPT_CHARS,
    BrowsableLink,
    EvidenceRef,
    SourceType,
    TrustTier,
)
from bin.landscape.pack import ContextPack, Role, build_pack
from bin.landscape.snapshot import LandscapeSnapshot
from bin.landscape.workflow_spec import (
    APPROVAL_REQUIRED_KINDS,
    StepKind,
    WorkflowSpec,
    WorkflowStep,
)

__all__ = [
    "APPROVAL_REQUIRED_KINDS",
    "BrowsableLink",
    "ContextPack",
    "EvidenceRef",
    "LandscapeSnapshot",
    "MAX_EXCERPT_CHARS",
    "Role",
    "SourceType",
    "StepKind",
    "TrustTier",
    "WorkflowSpec",
    "WorkflowStep",
    "build_pack",
    "canonical_json",
    "content_digest",
]

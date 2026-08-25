"""EvidenceRef: one normalized, trust-tiered piece of evidence.

An EvidenceRef is the atom of a project landscape: a pointer to something a
system adapter retrieved (git state, a GitHub issue, a CI build, a doc, a
chat excerpt, an MCP resource) plus a bounded excerpt and the reason it was
selected. It is frozen and content-addressed: ``digest`` (and the
digest-derived ``id``) are computed from the content fields, so any change to
the content is a different EvidenceRef.

Trust tiers (drive role-shaping in bin.landscape.pack):

* TIER1_SYSTEM   — system-captured facts (git commits, CI results). Cannot be
                   forged by a model or an external author.
* TIER2_INTERNAL — team-authored content (internal docs, our issue text).
* TIER3_EXTERNAL — untrusted external text (third-party issues, chat from
                   outside, scraped pages). Must never reach the executor
                   role and never selects tools or approves anything.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

import pydantic

from bin.landscape.canonical import content_digest
from bin.workorder import WorkOrderLink

MAX_EXCERPT_CHARS = 2000


class SourceType(str, Enum):
    GIT_REPO = "git_repo"
    GITHUB_ISSUE = "github_issue"
    GITHUB_PR = "github_pr"
    GITHUB_RELEASE = "github_release"
    CI_BUILD = "ci_build"
    DOC = "doc"
    CHAT = "chat"
    API = "api"
    MCP_RESOURCE = "mcp_resource"


class TrustTier(str, Enum):
    TIER1_SYSTEM = "tier1_system"
    TIER2_INTERNAL = "tier2_internal"
    TIER3_EXTERNAL = "tier3_external"


class BrowsableLink(WorkOrderLink):
    """WorkOrderLink (https-only validation inherited) frozen for use inside
    immutable landscape artifacts."""

    model_config = pydantic.ConfigDict(frozen=True)


class EvidenceRef(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(frozen=True)

    source_type: SourceType
    canonical_uri: str
    revision: str = ""  # commit sha, issue updated_at etag, build number, …
    retrieved_at: str = ""  # ISO-8601, stamped by the retrieving system
    trust_tier: TrustTier
    excerpt: str = ""  # bounded; the digest covers it
    browsable_link: Optional[BrowsableLink] = None  # https-only, human-facing
    why_selected: str = ""

    @pydantic.field_validator("canonical_uri")
    @classmethod
    def _nonempty_uri(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("canonical_uri must be non-empty")
        return v

    @pydantic.field_validator("excerpt", "why_selected")
    @classmethod
    def _bound_text(cls, v: str) -> str:
        return v[:MAX_EXCERPT_CHARS]

    def _content_payload(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type.value,
            "canonical_uri": self.canonical_uri,
            "revision": self.revision,
            "retrieved_at": self.retrieved_at,
            "trust_tier": self.trust_tier.value,
            "excerpt": self.excerpt,
            "browsable_link": (
                self.browsable_link.model_dump() if self.browsable_link else None
            ),
            "why_selected": self.why_selected,
        }

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def digest(self) -> str:
        """Content digest — the identity of this evidence."""
        return content_digest(self._content_payload())

    @pydantic.computed_field  # type: ignore[prop-decorator]
    @property
    def id(self) -> str:
        """Short content-derived id for citation (stable, collision-unlikely
        at landscape scale; the full digest disambiguates if ever needed)."""
        return "ev-" + self.digest[:16]

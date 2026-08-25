"""Sandbox run -> EvidenceRef: preflight reports become citable evidence.

The sandbox verification report is itself evidence (plan Phase 4: "report
becomes an EvidenceRef"), so judging and release decisions can cite it by
digest like any other landscape evidence.
"""
from __future__ import annotations

from bin.landscape.evidence import EvidenceRef, SourceType, TrustTier
from bin.sandbox.backend import SandboxRunResult

_EXCERPT_LIMIT = 2000


def preflight_evidence(
    result: SandboxRunResult, *, retrieved_at: str, why: str
) -> EvidenceRef:
    """Wrap a sandbox run result as TIER1_SYSTEM evidence.

    Tier rationale: the report is produced by system-controlled
    infrastructure (our backend, our profile pins), not by external text.
    The excerpt is a bounded human-readable summary; the full result is
    persisted separately (CAS) by the caller.
    """
    lines = [
        f"sandbox={result.backend} profile={result.profile_digest[:16]} "
        f"ok={result.ok}",
    ]
    if result.workorder_id:
        lines.append(f"workorder={result.workorder_id}")
    for cr in result.commands:
        lines.append(f"$ {cr.command} -> exit {cr.exit_code}")
    if result.error:
        lines.append(f"error: {result.error}")
    excerpt = "\n".join(lines)
    if len(excerpt) > _EXCERPT_LIMIT:
        excerpt = excerpt[: _EXCERPT_LIMIT - 15] + "\n...[truncated]"

    return EvidenceRef(
        source_type=SourceType.CI_BUILD,
        canonical_uri=(
            f"sandbox:{result.backend}:{result.profile_digest}"
            + (f":{result.workorder_id}" if result.workorder_id else "")
        ),
        revision=result.finished_at or result.started_at,
        retrieved_at=retrieved_at,
        trust_tier=TrustTier.TIER1_SYSTEM,
        excerpt=excerpt,
        why_selected=why,
    )

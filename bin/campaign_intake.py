"""Campaign intake: signal adapters -> dedup gate -> findings.

The pipeline middle from the bugsweep product model §1: collect signals
from a campaign project's sources, persist them as evidence, dedup against
open PRs / in-flight work / prior campaign slugs, and mint candidate
findings in the campaign ledger.

Trust discipline: adapters assign tiers (never callers); TIER3 signal
bodies are evidence for HUMANS and the judge — the intake never sends them
to an LM. Dedup evidence (TIER1 lists) is linked to each finding it
suppresses, so "why was this dropped?" is always answerable.

Slugging: deterministic from the signal's canonical URI (stable across
re-runs) — re-ingesting the same signals is idempotent: existing slugs are
skipped, never duplicated.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bin import campaigns, catalog  # noqa: E402
from bin.landscape.adapters import get_adapter  # noqa: E402

SIGNAL_KINDS = {
    "sentry-csv": "sentry_csv",
    "slack-csv": "slack_csv",
    "github-autoclosed": "github_autoclosed",
    "foundry-stories": "foundry_stories",
}

class IntakeError(RuntimeError):
    pass

def _uri_digest(uri: str) -> str:
    return hashlib.sha256(uri.encode("utf-8")).hexdigest()[:8]


def _slug_for(uri: str, title_hint: str = "") -> str:
    """Deterministic finding slug from the signal's canonical URI. The
    8-hex suffix is the identity (URI-derived); the stem is display sugar
    from the title and may drift between runs — dedup keys on the suffix,
    never the whole slug."""
    words = re.findall(r"[a-z0-9]+", title_hint.lower())[:5]
    stem = "-".join(words) if words else "signal"
    return f"{stem}-{_uri_digest(uri)}"

def _title_from_excerpt(excerpt: str) -> str:
    first = (excerpt or "").strip().splitlines()[0] if excerpt else ""
    return first[:180] or "untitled signal"

_DEDUP_KINDS = {
    "open_prs_tsv": "open_prs",
    "inflight_tsv": "inflight",
    "prior_slugs_text": "prior_slugs",
}

_MIN_PLAIN_TOKEN_LEN = 6

def _keep_token(tok: str) -> bool:
    return len(tok) >= _MIN_PLAIN_TOKEN_LEN or bool(re.search(r"[-_0-9]", tok))

def _identifier_tokens(kind: str, content: str):
    """Yield structured identifier tokens from ONE dedup list's raw content.

    prior_slugs: each non-empty line IS the identifier (whole slug).
    open_prs / inflight (TSV): only single-token cells that look like
    identifiers — pure digits (PR numbers) or branch/slug shapes containing
    '/', '-' or '_' ('feature/ban-dialog-copy' -> 'ban-dialog-copy').
    Free-text cells (titles — anything with whitespace, or plain prose
    words) are NEVER tokenized: title words like 'does'/'timestamp' must
    not suppress findings.
    """
    for line in (content or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if kind == "prior_slugs":
            yield line.lower()
            continue
        for cell in line.split("\t"):
            cell = cell.strip().lower()
            if not cell or re.search(r"\s", cell):
                continue
            if re.fullmatch(r"\d+", cell):
                yield cell
            elif re.fullmatch(r"[a-z0-9][a-z0-9/_.-]*", cell) and re.search(
                r"[/_-]", cell
            ):
                yield cell.rsplit("/", 1)[-1]

def _dedup_tokens(campaign_name: str, config: dict) -> tuple[dict[str, str], list]:
    """Collect dedup-list evidence and extract match tokens.

    Returns ({token -> which list it came from}, dedup EvidenceRefs).
    Tokens come ONLY from structured identifier fields (branch names, PR
    numbers, whole prior-campaign slugs) — never from free-text titles.
    The EvidenceRefs still freeze the FULL lists for audit.
    """
    dedup_cfg = {
        k: config[k]
        for k in ("open_prs_tsv", "inflight_tsv", "prior_slugs_text")
        if config.get(k)
    }
    if not dedup_cfg:
        return {}, []
    refs = get_adapter("dedup_lists").collect(
        dedup_cfg, why=f"dedup gate for campaign {campaign_name}"
    )
    tokens: dict[str, str] = {}
    for key, kind in _DEDUP_KINDS.items():
        if key not in dedup_cfg:
            continue
        for tok in _identifier_tokens(kind, str(dedup_cfg[key])):
            if _keep_token(tok):
                tokens.setdefault(tok, kind)
    return tokens, list(refs)

def ingest(
    campaign_name: str,
    *,
    signals: dict[str, dict],
    dedup: Optional[dict] = None,
    limits: Optional[dict] = None,
    max_new: Optional[int] = None,
) -> dict[str, Any]:
    """Run intake for a campaign.

    signals: {source_name: {kind: sentry-csv|slack-csv|github-autoclosed|
              foundry-stories, config: adapter config dict}}
    dedup:   optional {open_prs_tsv, inflight_tsv, prior_slugs_text}
    max_new: candidate budget — stop minting findings once this many were
             created this run (evidence for the remainder is still frozen,
             so 'what was dropped?' stays answerable via truncated=true).

    Returns {created: [slugs], deduped: [{slug, reason}], skipped_existing:
    [slugs], evidence_count, per_source: {name: count}, truncated}.
    """
    camp = campaigns.get_campaign(campaign_name)
    project_id = camp["project_id"]
    projects = {p["id"]: p["name"] for p in catalog.list_projects()}
    project_name = projects.get(project_id)
    if project_name is None:
        raise IntakeError(f"campaign {campaign_name!r} has no active project")

    tokens, dedup_refs = _dedup_tokens(campaign_name, dedup or {})

    existing = {f["slug"] for f in campaigns.list_findings(campaign_name)}
    existing_ids = {s.rsplit("-", 1)[-1] for s in existing}
    created: list[str] = []
    deduped: list[dict] = []
    skipped: list[str] = []
    per_source: dict[str, int] = {}
    evidence_count = 0
    truncated = False

    for source_name, spec in signals.items():
        kind = spec.get("kind", "")
        adapter_kind = SIGNAL_KINDS.get(kind)
        if adapter_kind is None:
            raise IntakeError(
                f"source {source_name!r}: unknown signal kind {kind!r}; "
                f"known: {', '.join(sorted(SIGNAL_KINDS))}"
            )
        try:
            src = catalog.get_source(project_name, source_name)
        except LookupError:
            catalog.create_source(
                project=project_name,
                name=source_name,
                kind=kind,
                locator=spec.get("config", {}).get("locator", kind),
                trust_tier=3,
            )
            src = catalog.get_source(project_name, source_name)

        refs = get_adapter(adapter_kind).collect(
            spec.get("config", {}),
            why=f"campaign {campaign_name} intake from {source_name}",
            limits=limits,
        )
        per_source[source_name] = len(refs)

        for ref in refs:
            digest = catalog.insert_evidence_ref(ref, source_id=src["id"])
            evidence_count += 1
            if max_new is not None and len(created) >= max_new:
                truncated = True
                continue
            title = _title_from_excerpt(ref.excerpt)
            slug = _slug_for(ref.canonical_uri, title)
            if slug in existing or _uri_digest(ref.canonical_uri) in existing_ids:
                skipped.append(slug)
                continue
            hit = next(
                (
                    (tok, which)
                    for tok, which in tokens.items()
                    if tok and (tok in slug or tok in title.lower())
                ),
                None,
            )
            if hit is not None:
                deduped.append(
                    {"slug": slug, "reason": f"matched {hit[1]} entry {hit[0]!r}"}
                )
                continue
            campaigns.create_finding(
                campaign=campaign_name,
                slug=slug,
                title=title,
                source_kind=kind,
                dedup_notes="passed dedup gate"
                + (f" ({len(tokens)} tokens checked)" if tokens else " (no lists)"),
            )
            campaigns.link_finding_evidence(
                campaign=campaign_name,
                slug=slug,
                evidence_digest=digest,
                role="signal",
            )
            for dref in dedup_refs:
                campaigns.link_finding_evidence(
                    campaign=campaign_name,
                    slug=slug,
                    evidence_digest=catalog.insert_evidence_ref(
                        dref, source_id=src["id"]
                    ),
                    role="dedup",
                )
            existing.add(slug)
            existing_ids.add(_uri_digest(ref.canonical_uri))
            created.append(slug)

    return {
        "created": created,
        "deduped": deduped,
        "skipped_existing": skipped,
        "evidence_count": evidence_count,
        "per_source": per_source,
        "truncated": truncated,
    }


def ingest_from_spec(
    campaign_name: str,
    *,
    dedup: Optional[dict] = None,
    limits: Optional[dict] = None,
) -> dict[str, Any]:
    """Run intake from the campaign's SweepSpec: each ``corpus`` entry maps
    to one signal source, and ``intake.max_candidates`` becomes the
    candidate budget. Raises IntakeError when the campaign has no spec or a
    corpus entry names an adapter this intake doesn't serve."""
    spec = campaigns.get_sweep_spec(campaign_name)
    if spec is None:
        raise IntakeError(
            f"campaign {campaign_name!r} has no sweep spec; use ingest() "
            "with explicit signals"
        )
    if not spec.corpus:
        raise IntakeError(f"sweep spec for {campaign_name!r} declares no corpus")
    kind_by_adapter = {v: k for k, v in SIGNAL_KINDS.items()}
    signals: dict[str, dict] = {}
    for i, src in enumerate(spec.corpus):
        kind = kind_by_adapter.get(src.adapter)
        if kind is None:
            raise IntakeError(
                f"corpus[{i}]: adapter {src.adapter!r} is not an intake "
                f"signal adapter; known: {', '.join(sorted(SIGNAL_KINDS.values()))}"
            )
        signals[f"{src.adapter}-{i}"] = {"kind": kind, "config": dict(src.config)}
    return ingest(
        campaign_name,
        signals=signals,
        dedup=dedup,
        limits=limits,
        max_new=spec.intake.max_candidates,
    )

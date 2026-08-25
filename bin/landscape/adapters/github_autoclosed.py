"""github_autoclosed adapter: parse the campaign's autoclosed.csv (stale-bot
auto-closed GitHub issues) into frozen EvidenceRefs.

CSV header: ``issue,created,auto_closed,title,body``. This is the
"auto-closed without fix" recovery signal class from the aug16 bugsweep:
issues the stale bot closed that may still reproduce at the pin. The dossier
rule ("still occurring" requires cross-evidence) lives downstream — this
adapter only freezes the raw signal.

Trust rule: issue titles/bodies are user-authored external text —
TIER3_EXTERNAL, always. Bodies are html-unescaped, token-redacted (shared
helper from slack_csv), and bounded before freezing.

Digest determinism: ``retrieved_at`` is stamped from the row's
``auto_closed`` date (export data), never the wall clock.
"""
from __future__ import annotations

import csv
import html
import io
from typing import Optional

from bin.landscape.adapters._text import bounded_excerpt
from bin.landscape.adapters.slack_csv import redact_text
from bin.landscape.evidence import (
    MAX_EXCERPT_CHARS,
    BrowsableLink,
    EvidenceRef,
    SourceType,
    TrustTier,
)


class GitHubAutoclosedPayloadError(ValueError):
    """Malformed autoclosed.csv row. Raised loudly — silently skipping
    malformed evidence would hide data loss."""


def _require(row: dict, key: str) -> str:
    value = (row.get(key) or "").strip()
    if not value:
        raise GitHubAutoclosedPayloadError(
            f"autoclosed row missing required field {key!r}"
        )
    return value


def parse_row(
    repo: str, row: dict, *, why: str, max_chars: int = MAX_EXCERPT_CHARS
) -> EvidenceRef:
    """One autoclosed.csv row -> TIER3_EXTERNAL EvidenceRef for
    ``github:<org/repo>#<issue>``."""
    if not isinstance(row, dict):
        raise GitHubAutoclosedPayloadError("autoclosed row must be a dict")
    number = _require(row, "issue")
    title = _require(row, "title")
    created = (row.get("created") or "").strip()
    auto_closed = (row.get("auto_closed") or "").strip()

    header = (
        f"issue #{number} auto-closed without fix: "
        f"{redact_text(html.unescape(title))}\n"
        f"created: {created or '?'}  auto_closed: {auto_closed or '?'}"
    )
    body = redact_text(html.unescape(row.get("body") or "")).strip()
    excerpt = header + ("\n\n" + body if body else "")

    return EvidenceRef(
        source_type=SourceType.GITHUB_ISSUE,
        canonical_uri=f"github:{repo}#{number}",
        revision=auto_closed,
        retrieved_at=auto_closed,
        trust_tier=TrustTier.TIER3_EXTERNAL,
        excerpt=bounded_excerpt(excerpt, max_chars),
        browsable_link=BrowsableLink(
            label=f"Issue #{number}",
            url=f"https://github.com/{repo}/issues/{number}",
            kind="issue",
        ),
        why_selected=why,
    )


def parse_rows(
    repo: str, csv_text: str, *, why: str, limits: Optional[dict] = None
) -> list[EvidenceRef]:
    """Parse autoclosed.csv text. Empty input -> empty list; malformed rows
    raise, never skip."""
    if not (csv_text or "").strip():
        return []
    limits = limits or {}
    max_items = max(1, int(limits.get("max_items", 50)))
    max_chars = int(limits.get("max_chars", MAX_EXCERPT_CHARS))
    rows = list(csv.DictReader(io.StringIO(csv_text)))[:max_items]
    return [parse_row(repo, r, why=why, max_chars=max_chars) for r in rows]


def collect(
    config: dict, *, why: str, limits: Optional[dict] = None
) -> list[EvidenceRef]:
    """Adapter entrypoint for ALREADY-EXPORTED data.

    config: ``repo`` ('org/name', required) + ``csv_text``.
    """
    repo = config.get("repo", "")
    if not repo or "/" not in repo:
        raise GitHubAutoclosedPayloadError(
            "github_autoclosed config requires repo='org/name'"
        )
    csv_text = config.get("csv_text")
    if csv_text is None:
        raise GitHubAutoclosedPayloadError(
            "github_autoclosed config requires csv_text"
        )
    return parse_rows(repo, csv_text, why=why, limits=limits)

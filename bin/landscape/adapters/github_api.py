"""github_api adapter: parse ALREADY-FETCHED GitHub REST v3 JSON into
frozen EvidenceRefs.

Trust rule: issue / PR / release BODIES are user-authored external text —
TIER3_EXTERNAL, always. Factual metadata (number, state, dates, head/base
sha, tag name) appears only in the excerpt HEADER line; it does not upgrade
the tier, because the ref carries the untrusted body text.

Parsing functions take plain dicts (fixture-tested, no network). ``fetch``
uses urllib against api.github.com and is exercised only by
RUN_LIVE_TESTS-gated tests.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Optional

from bin.landscape.adapters._text import bounded_excerpt, now_iso
from bin.landscape.evidence import (
    MAX_EXCERPT_CHARS,
    BrowsableLink,
    EvidenceRef,
    SourceType,
    TrustTier,
)

API_ROOT = "https://api.github.com"

class GitHubPayloadError(ValueError):
    """A payload dict is not the GitHub REST v3 shape we expect. Raised
    loudly — silently skipping malformed evidence would hide data loss."""

def _require(payload: dict, key: str, kind: str):
    """Fetch a required field or raise a clear GitHubPayloadError."""
    if not isinstance(payload, dict):
        raise GitHubPayloadError(f"{kind} payload must be a dict, got {type(payload).__name__}")
    if key not in payload or payload[key] is None:
        raise GitHubPayloadError(f"{kind} payload missing required field {key!r}")
    return payload[key]

def _browsable(url: str, label: str, kind: str) -> Optional[BrowsableLink]:
    if isinstance(url, str) and url.startswith("https://"):
        return BrowsableLink(label=label, url=url, kind=kind)
    return None

def _ref(
    *,
    source_type: SourceType,
    canonical_uri: str,
    revision: str,
    header: str,
    body: str,
    link: Optional[BrowsableLink],
    why: str,
    max_chars: int,
) -> EvidenceRef:
    body = (body or "").strip()
    excerpt = header + ("\n\n" + body if body else "")
    return EvidenceRef(
        source_type=source_type,
        canonical_uri=canonical_uri,
        revision=revision,
        retrieved_at=now_iso(),
        trust_tier=TrustTier.TIER3_EXTERNAL,
        excerpt=bounded_excerpt(excerpt, max_chars),
        browsable_link=link,
        why_selected=why,
    )

def issue_ref(
    repo: str, payload: dict, *, why: str, max_chars: int = MAX_EXCERPT_CHARS
) -> EvidenceRef:
    """``repo`` is ``owner/name``; ``payload`` a REST v3 issue dict."""
    number = _require(payload, "number", "issue")
    title = _require(payload, "title", "issue")
    state = _require(payload, "state", "issue")
    updated = _require(payload, "updated_at", "issue")
    header = f"issue #{number} [{state}] {title} (updated {updated})"
    return _ref(
        source_type=SourceType.GITHUB_ISSUE,
        canonical_uri=f"https://github.com/{repo}/issues/{number}",
        revision=str(updated),
        header=header,
        body=payload.get("body") or "",
        link=_browsable(
            payload.get("html_url", ""), f"Issue #{number}", "issue"
        ),
        why=why,
        max_chars=max_chars,
    )

def pr_ref(
    repo: str, payload: dict, *, why: str, max_chars: int = MAX_EXCERPT_CHARS
) -> EvidenceRef:
    """``payload`` is a REST v3 pull-request dict (has head/base)."""
    number = _require(payload, "number", "pull request")
    title = _require(payload, "title", "pull request")
    state = _require(payload, "state", "pull request")
    updated = _require(payload, "updated_at", "pull request")
    head = _require(payload, "head", "pull request")
    base = _require(payload, "base", "pull request")
    head_sha = _require(head, "sha", "pull request head")
    base_sha = _require(base, "sha", "pull request base")
    header = (
        f"pr #{number} [{state}] {title} "
        f"(head {head_sha[:12]} -> base {base_sha[:12]}, updated {updated})"
    )
    return _ref(
        source_type=SourceType.GITHUB_PR,
        canonical_uri=f"https://github.com/{repo}/pull/{number}",
        revision=str(head_sha),
        header=header,
        body=payload.get("body") or "",
        link=_browsable(payload.get("html_url", ""), f"PR #{number}", "pr"),
        why=why,
        max_chars=max_chars,
    )

def release_ref(
    repo: str, payload: dict, *, why: str, max_chars: int = MAX_EXCERPT_CHARS
) -> EvidenceRef:
    """``payload`` is a REST v3 release dict."""
    tag = _require(payload, "tag_name", "release")
    name = payload.get("name") or tag
    published = _require(payload, "published_at", "release")
    draft = payload.get("draft", False)
    prerelease = payload.get("prerelease", False)
    flags = "".join(
        [" [draft]" if draft else "", " [prerelease]" if prerelease else ""]
    )
    header = f"release {tag} — {name}{flags} (published {published})"
    return _ref(
        source_type=SourceType.GITHUB_RELEASE,
        canonical_uri=f"https://github.com/{repo}/releases/tag/{tag}",
        revision=str(published),
        header=header,
        body=payload.get("body") or "",
        link=_browsable(payload.get("html_url", ""), f"Release {tag}", "other"),
        why=why,
        max_chars=max_chars,
    )

_PARSERS = {
    "issues": issue_ref,
    "pulls": pr_ref,
    "releases": release_ref,
}

def parse(
    repo: str,
    kind: str,
    payloads: list[dict],
    *,
    why: str,
    max_chars: int = MAX_EXCERPT_CHARS,
) -> list[EvidenceRef]:
    """Parse a list of already-fetched payload dicts of ``kind``
    (issues | pulls | releases). Malformed payloads raise, never skip."""
    try:
        parser = _PARSERS[kind]
    except KeyError:
        raise GitHubPayloadError(
            f"unknown github payload kind {kind!r}; known: "
            + ", ".join(sorted(_PARSERS))
        ) from None
    return [parser(repo, p, why=why, max_chars=max_chars) for p in payloads]

def collect(
    config: dict, *, why: str, limits: Optional[dict] = None
) -> list[EvidenceRef]:
    """Adapter entrypoint for ALREADY-FETCHED data.

    config: ``repo`` ('owner/name', required) plus any of ``issues`` /
    ``pulls`` / ``releases`` — lists of REST v3 dicts.
    limits: ``max_items`` per kind (default 30), ``max_chars`` per excerpt.
    """
    repo = config.get("repo", "")
    if not repo or "/" not in repo:
        raise GitHubPayloadError("github_api config requires repo='owner/name'")
    limits = limits or {}
    max_items = max(1, int(limits.get("max_items", 30)))
    max_chars = int(limits.get("max_chars", MAX_EXCERPT_CHARS))
    refs: list[EvidenceRef] = []
    for kind in ("issues", "pulls", "releases"):
        payloads = config.get(kind)
        if payloads:
            refs.extend(
                parse(repo, kind, list(payloads)[:max_items], why=why, max_chars=max_chars)
            )
    return refs

def fetch(config: dict, *, timeout: float = 20.0) -> dict:
    """LIVE fetch of issues / pulls / releases for ``config['repo']`` from
    api.github.com. Network code — exercised only by RUN_LIVE_TESTS-gated
    tests. Returns a dict suitable to pass straight to ``collect``.
    """
    repo = config.get("repo", "")
    if not repo or "/" not in repo:
        raise GitHubPayloadError("fetch config requires repo='owner/name'")
    per_page = int(config.get("per_page", 10))
    token = config.get("token", "")
    out: dict = {"repo": repo}
    for kind in config.get("kinds", ("issues", "pulls", "releases")):
        if kind not in _PARSERS:
            raise GitHubPayloadError(f"unknown fetch kind {kind!r}")
        url = f"{API_ROOT}/repos/{repo}/{kind}?per_page={per_page}&state=all"
        if kind == "releases":
            url = f"{API_ROOT}/repos/{repo}/releases?per_page={per_page}"
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "data-tournaments-landscape",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out[kind] = json.loads(resp.read().decode("utf-8"))
    return out

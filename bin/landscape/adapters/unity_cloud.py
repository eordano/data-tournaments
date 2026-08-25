"""Unity Cloud Build adapter: CI build payloads -> frozen EvidenceRefs.

Parses ALREADY-FETCHED Unity Cloud Build REST payloads (the API that
unity-explorer's scripts/cloudbuild/build.py drives) into TIER1_SYSTEM
evidence: build status is system-captured CI state, not human prose.

Same conventions as github_api: parse functions take dicts and raise
``UnityCloudPayloadError`` on malformed payloads (never silently skip);
``collect`` is the adapter entrypoint for already-fetched data; ``fetch``
does live HTTP and is exercised only by RUN_LIVE_TESTS-gated tests.

config (collect): org, project, builds=[payload dicts]
config (fetch):   org, project, buildtarget, api_key_env (name of the env
                  var holding the key — the VALUE never appears in configs,
                  per the landscape secret rules), per_page
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Optional

from bin.landscape.evidence import (
    BrowsableLink,
    EvidenceRef,
    SourceType,
    TrustTier,
)

API_ROOT = "https://build-api.cloud.unity3d.com/api/v1"
MAX_EXCERPT_CHARS = 1600


class UnityCloudPayloadError(ValueError):
    """Malformed/incomplete Unity Cloud Build payload."""


def _require(payload: dict, key: str):
    if key not in payload or payload[key] in (None, ""):
        raise UnityCloudPayloadError(
            f"unity cloud build payload missing required field {key!r}"
        )
    return payload[key]


def parse_build(
    org: str,
    project: str,
    payload: dict,
    *,
    why: str,
    max_chars: int = MAX_EXCERPT_CHARS,
) -> EvidenceRef:
    """One build payload -> TIER1_SYSTEM EvidenceRef.

    Required fields: build (number), buildtargetid, buildStatus. Optional
    context fields (scmBranch, lastBuiltRevision, totalTimeInSeconds,
    created, finished) enrich the excerpt when present.
    """
    if not isinstance(payload, dict):
        raise UnityCloudPayloadError("build payload must be a dict")
    number = _require(payload, "build")
    target = _require(payload, "buildtargetid")
    status = _require(payload, "buildStatus")

    lines = [f"build #{number} target={target} status={status}"]
    if payload.get("scmBranch"):
        lines.append(f"branch: {payload['scmBranch']}")
    if payload.get("lastBuiltRevision"):
        lines.append(f"revision: {payload['lastBuiltRevision']}")
    if payload.get("totalTimeInSeconds") is not None:
        lines.append(f"duration: {payload['totalTimeInSeconds']}s")
    for key in ("created", "finished"):
        if payload.get(key):
            lines.append(f"{key}: {payload[key]}")
    excerpt = "\n".join(lines)
    if len(excerpt) > max_chars:
        excerpt = excerpt[: max_chars - 15] + "\n...[truncated]"

    browsable = None
    if org and project:
        browsable = BrowsableLink(
            label=f"UCB build #{number}",
            url=(
                "https://developer.cloud.unity3d.com/build/orgs/"
                f"{org}/projects/{project}/buildtargets/{target}/builds/{number}/"
            ),
            kind="ci",
        )

    return EvidenceRef(
        source_type=SourceType.CI_BUILD,
        canonical_uri=f"ucb:{org}/{project}/{target}#{number}",
        revision=str(payload.get("lastBuiltRevision") or number),
        retrieved_at=str(payload.get("finished") or payload.get("created") or ""),
        trust_tier=TrustTier.TIER1_SYSTEM,
        excerpt=excerpt,
        browsable_link=browsable,
        why_selected=why,
    )


def collect(
    config: dict, *, why: str, limits: Optional[dict] = None
) -> list[EvidenceRef]:
    """Adapter entrypoint for ALREADY-FETCHED build payloads."""
    org = config.get("org", "")
    project = config.get("project", "")
    if not org or not project:
        raise UnityCloudPayloadError(
            "unity_cloud config requires org and project"
        )
    limits = limits or {}
    max_items = max(1, int(limits.get("max_items", 20)))
    max_chars = int(limits.get("max_chars", MAX_EXCERPT_CHARS))
    builds = list(config.get("builds") or [])[:max_items]
    return [
        parse_build(org, project, b, why=why, max_chars=max_chars)
        for b in builds
    ]


def fetch(config: dict, *, timeout: float = 30.0) -> dict:
    """LIVE fetch of recent builds. Network code — RUN_LIVE_TESTS only.

    The API key is read from the env var NAMED in config['api_key_env']
    (default UNITY_CLOUD_BUILD_API_KEY); the value never lives in configs.
    Returns a dict suitable to pass straight to ``collect``.
    """
    org = config.get("org", "")
    project = config.get("project", "")
    target = config.get("buildtarget", "_all")
    if not org or not project:
        raise UnityCloudPayloadError("fetch config requires org and project")
    key_env = config.get("api_key_env", "UNITY_CLOUD_BUILD_API_KEY")
    api_key = os.environ.get(key_env, "")
    if not api_key:
        raise UnityCloudPayloadError(
            f"fetch requires the {key_env} env var (value is never stored "
            "in source configs)"
        )
    per_page = int(config.get("per_page", 10))
    url = (
        f"{API_ROOT}/orgs/{org}/projects/{project}/buildtargets/{target}"
        f"/builds?per_page={per_page}"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Basic {api_key}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        builds = json.loads(resp.read().decode("utf-8"))
    return {"org": org, "project": project, "builds": builds}

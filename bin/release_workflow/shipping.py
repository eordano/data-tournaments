"""Shipping layer: PR creation, CI tracking, UCB build tracking, canary /
rollback contracts, and the release manifest (wave-8 B6).

Deliberately temporalio-FREE (same rule as generation_bridge.py) so the
root test suite exercises it; the Temporal activities in activities.py are
thin wrappers that call in here when the environment is configured.

TRANSPORT INJECTION (the e2b_backend pattern): all HTTP goes through an
injectable ``transport`` callable::

    transport(method: str, url: str, payload: dict | None) -> parsed JSON

Tests inject fakes fed from tests/fixtures/shipping/*.json; production
builds a urllib transport against api.github.com /
build-api.cloud.unity3d.com, gated on env vars. When the credential env
var is ABSENT, construction raises RuntimeError naming the variable —
success is never faked and a weaker path is never silently substituted
(failures the API reports are encoded in typed results/exceptions, not
swallowed).

SECRET RULE (landscape convention): configuration names the ENV VAR
(``token_env`` / ``api_key_env``); the secret VALUE never appears in
configs, results, manifests, or logs.

PUSH BOUNDARY: pushing branches is NOT implemented here. The git
worktree/branch/push discipline lives with sandbox execution (the sandbox
holds the working tree and the deploy key); this module starts at the
point where a branch already exists on the remote — PR creation, CI/build
tracking, canary observation, and the release manifest.

PER-ACTION APPROVAL SCOPES (product-model §B6): the real August campaign
user approved BRANCH PUSH but FORBADE PR creation ("push yes, NO PRs") —
so push, PR-create, and promote are three SEPARATE approvable actions,
never one blanket "ship" approval. The approvals layer consumes
``ACTION_SCOPES`` to gate each action independently:

    ACTION_SCOPES = {
        "push":    "ship:push:*",     # push branches to the remote
        "pr":      "ship:pr:*",       # open/update pull requests
        "promote": "ship:promote:*",  # flip production / tag release
    }

Holding one scope grants NOTHING about the others.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Optional

GITHUB_API_ROOT = "https://api.github.com"
UCB_API_ROOT = "https://build-api.cloud.unity3d.com/api/v1"

ACTION_SCOPES = {
    "push": "ship:push:*",
    "pr": "ship:pr:*",
    "promote": "ship:promote:*",
}

MANIFEST_SCHEMA_VERSION = 1

Transport = Callable[..., Any]

class ShippingPayloadError(ValueError):
    """A remote API payload is not the shape we expect. Raised loudly —
    a malformed response is never mapped to a fake success."""

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _urllib_transport(headers: dict) -> Transport:
    """Build the live urllib transport. ``headers`` carries the auth
    header; the transport never logs or returns it."""

    def transport(method: str, url: str, payload: Optional[dict] = None):
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", **headers},
        )
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            body = resp.read().decode("utf-8")
        return json.loads(body) if body.strip() else {}

    return transport

class GitHubShipper:
    """PR creation/update + CI status against the GitHub REST v3 API.

    ``transport=None`` (production) requires the env var named by
    ``token_env`` (default GITHUB_TOKEN) and raises RuntimeError when it
    is absent — never a fake success. Tests inject a fake transport.

    NOTE: pushing the branch is NOT this class's job (see module
    docstring — push lives with sandbox execution and is a separately
    approvable action, scope ``ship:push:*``; PR creation is scope
    ``ship:pr:*``).
    """

    def __init__(
        self,
        transport: Optional[Transport] = None,
        *,
        repo: str,
        token_env: str = "GITHUB_TOKEN",
    ):
        if not repo or "/" not in repo:
            raise ValueError("GitHubShipper requires repo='owner/name'")
        self.repo = repo
        self.token_env = token_env
        if transport is not None:
            self._transport = transport
            return
        token = os.environ.get(token_env, "")
        if not token:
            raise RuntimeError(
                f"GitHubShipper requires the {token_env} env var (the "
                "value is never stored in configs or results); refusing "
                "to construct an unauthenticated shipper"
            )
        self._transport = _urllib_transport(
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "data-tournaments-shipping",
            }
        )

    def create_or_update_pr(
        self, branch: str, base: str, title: str, body: str
    ) -> dict:
        """Idempotent PR upsert for ``head=branch`` -> ``base``.

        Searches open PRs with head=<owner>:<branch> first; PATCHes the
        existing one (title/body/base) or POSTs a new one. Returns the
        typed result ``{number, url, action: "created"|"updated"}``.
        """
        owner = self.repo.split("/", 1)[0]
        existing = self._transport(
            "GET",
            f"{GITHUB_API_ROOT}/repos/{self.repo}/pulls"
            f"?state=open&head={owner}:{branch}",
            None,
        )
        if not isinstance(existing, list):
            raise ShippingPayloadError(
                "PR search must return a list of pull-request dicts"
            )
        if existing:
            number = existing[0].get("number")
            if number is None:
                raise ShippingPayloadError("open PR payload missing 'number'")
            updated = self._transport(
                "PATCH",
                f"{GITHUB_API_ROOT}/repos/{self.repo}/pulls/{number}",
                {"title": title, "body": body, "base": base},
            )
            return {
                "number": number,
                "url": updated.get("html_url")
                or existing[0].get("html_url", ""),
                "action": "updated",
            }
        created = self._transport(
            "POST",
            f"{GITHUB_API_ROOT}/repos/{self.repo}/pulls",
            {"title": title, "head": branch, "base": base, "body": body},
        )
        if not isinstance(created, dict) or created.get("number") is None:
            raise ShippingPayloadError(
                "PR create response missing 'number' — not mapping to success"
            )
        return {
            "number": created["number"],
            "url": created.get("html_url", ""),
            "action": "created",
        }

    def get_ci_status(self, sha: str) -> dict:
        """Map the check-runs API shape to a coarse CI state.

        Returns ``{state: success|failure|pending, checks: [{name,
        conclusion}]}``. Rules: any non-completed run (or zero runs) ->
        pending; all conclusions in {success, neutral, skipped} ->
        success; anything else -> failure.
        """
        payload = self._transport(
            "GET",
            f"{GITHUB_API_ROOT}/repos/{self.repo}/commits/{sha}/check-runs",
            None,
        )
        if not isinstance(payload, dict) or "check_runs" not in payload:
            raise ShippingPayloadError(
                "check-runs response missing 'check_runs'"
            )
        runs = payload["check_runs"]
        checks = [
            {"name": r.get("name", ""), "conclusion": r.get("conclusion")}
            for r in runs
        ]
        if not runs or any(r.get("status") != "completed" for r in runs):
            state = "pending"
        elif all(
            r.get("conclusion") in ("success", "neutral", "skipped")
            for r in runs
        ):
            state = "success"
        else:
            state = "failure"
        return {"state": state, "checks": checks}

class UCBTracker:
    """Trigger + poll Unity Cloud Build builds (field names per
    bin/landscape/adapters/unity_cloud.py: build / buildStatus / links).

    ``transport=None`` (production) requires the env var named by
    ``api_key_env`` (default UNITY_CLOUD_BUILD_API_KEY) — RuntimeError
    when absent, per the landscape secret rule (config names the env
    var, never holds the value).
    """

    def __init__(
        self,
        transport: Optional[Transport] = None,
        *,
        org: str,
        project: str,
        api_key_env: str = "UNITY_CLOUD_BUILD_API_KEY",
    ):
        if not org or not project:
            raise ValueError("UCBTracker requires org and project")
        self.org = org
        self.project = project
        self.api_key_env = api_key_env
        if transport is not None:
            self._transport = transport
            return
        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            raise RuntimeError(
                f"UCBTracker requires the {api_key_env} env var (the value "
                "is never stored in configs or results); refusing to "
                "construct an unauthenticated tracker"
            )
        self._transport = _urllib_transport(
            {"Authorization": f"Basic {api_key}"}
        )

    def _target_root(self, target: str) -> str:
        return (
            f"{UCB_API_ROOT}/orgs/{self.org}/projects/{self.project}"
            f"/buildtargets/{target}"
        )

    def trigger_build(self, target: str, commit: str) -> dict:
        """POST a build for ``commit``; returns {build_number, status}.

        UCB responds with a list of created build payloads; the first is
        the one we asked for. Idempotency per (target, commit) is the
        caller's concern (Temporal activity retries re-POST — UCB dedups
        queued builds for the same commit on a clean=false target).
        """
        payload = self._transport(
            "POST",
            f"{self._target_root(target)}/builds",
            {"clean": False, "commit": commit},
        )
        build = payload[0] if isinstance(payload, list) and payload else payload
        if not isinstance(build, dict) or build.get("build") in (None, ""):
            raise ShippingPayloadError(
                "UCB trigger response missing 'build' number"
            )
        if build.get("buildStatus") in (None, ""):
            raise ShippingPayloadError(
                "UCB trigger response missing 'buildStatus'"
            )
        return {
            "build_number": build["build"],
            "status": build["buildStatus"],
        }

    def poll_build(self, target: str, number: int) -> dict:
        """GET one build; returns {status, artifact_url?}.

        ``artifact_url`` is present only when UCB exposes
        links.download_primary.href (i.e. the build produced an
        artifact) — its absence is honest, not padded.
        """
        payload = self._transport(
            "GET", f"{self._target_root(target)}/builds/{number}", None
        )
        if not isinstance(payload, dict) or payload.get("buildStatus") in (
            None,
            "",
        ):
            raise ShippingPayloadError(
                "UCB poll response missing 'buildStatus'"
            )
        out: dict = {"status": payload["buildStatus"]}
        href = (
            (payload.get("links") or {}).get("download_primary") or {}
        ).get("href")
        if href:
            out["artifact_url"] = href
        return out

def _urllib_probe(url: str, timeout: float = 10.0) -> tuple[int, str]:
    """Default canary probe: plain GET, returns (status_code, detail)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "data-tournaments-shipping-canary"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, f"HTTP {resp.status}"

class CanaryMonitor:
    """Point-in-time canary health + the typed rollback contract.

    ``probe`` is injectable for tests: callable(url) -> (status_code,
    detail). Default is a urllib GET with a 10s timeout. Probe failures
    (exceptions) are encoded as unhealthy results with the error detail —
    never raised past the boundary and never mapped to healthy.
    """

    def __init__(self, probe: Optional[Callable[[str], tuple]] = None):
        self._probe = probe or _urllib_probe

    def check(self, url: str) -> dict:
        """Returns ``{healthy: bool, detail: str}``. 2xx/3xx = healthy."""
        try:
            status, detail = self._probe(url)
        except Exception as exc:
            return {
                "healthy": False,
                "detail": f"probe error: {type(exc).__name__}: {exc}",
            }
        return {"healthy": 200 <= int(status) < 400, "detail": str(detail)}

    def rollback_plan(self, build: dict) -> dict:
        """Typed NO-OP rollback contract — documented, never fake-executed.

        Real rollback needs credentials and systems this module does not
        hold (canary/production deploy credentials, monitoring write key,
        WorkOrder store). The plan states exactly what a real executor
        must do; ``executed`` is always False here so no caller can
        mistake the contract for an action.
        """
        return {
            "action": "rollback",
            "build": dict(build or {}),
            "executed": False,
            "scope": ACTION_SCOPES["promote"],
            "requires": [
                "destroy the canary deployment for this build",
                "revert the production pointer if partially applied",
                "mark associated WorkOrders aborted",
                "notify release owners (webhook / channel)",
            ],
            "note": (
                "no-op contract: real rollback requires canary/production "
                "deploy credentials — this module documents the steps and "
                "never pretends to have run them"
            ),
        }

def build_release_manifest(
    *,
    repo: str,
    commit: str,
    snapshot_digest: str,
    work_order_ids,
    approval_event_ids,
    pr,
    builds,
    canary,
    outcome: str,
    written_at: str = "",
) -> dict:
    """Canonical release manifest + sha256 digest — the B6 audit artifact
    tying the release together (context snapshot -> work orders ->
    approvals -> PR -> builds -> canary -> outcome).

    The digest is sha256 over the canonical JSON (sorted keys, compact
    separators) of every field EXCEPT ``written_at`` and the digest
    itself, so identical release content always yields an identical
    digest regardless of when the manifest was written. Required fields
    (repo, commit, outcome) must be non-empty strings; id collections
    must be lists of strings. Secret VALUES must never enter a manifest —
    callers pass ids, digests, and URLs only.
    """
    for name, value in (("repo", repo), ("commit", commit), ("outcome", outcome)):
        if not value or not isinstance(value, str):
            raise ValueError(
                f"release manifest requires a non-empty string {name!r}"
            )
    for name, ids in (
        ("work_order_ids", work_order_ids),
        ("approval_event_ids", approval_event_ids),
    ):
        if not isinstance(ids, (list, tuple)) or not all(
            isinstance(i, str) for i in ids
        ):
            raise ValueError(f"{name} must be a list of strings")

    body = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "repo": repo,
        "commit": commit,
        "snapshot_digest": snapshot_digest or "",
        "work_order_ids": list(work_order_ids),
        "approval_event_ids": list(approval_event_ids),
        "pr": dict(pr) if pr else None,
        "builds": [dict(b) for b in (builds or [])],
        "canary": dict(canary) if canary else None,
        "outcome": outcome,
    }
    canonical = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    manifest = dict(body)
    manifest["written_at"] = written_at or _now_iso()
    manifest["manifest_digest"] = hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()
    return manifest

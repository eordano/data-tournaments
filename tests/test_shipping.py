"""Contract tests for the shipping layer (bin/release_workflow/shipping.py).

All HTTP via injected fake transports fed from tests/fixtures/shipping/*.json
— no network. Live paths are credential-gated (RuntimeError naming the env
var when unset); never a fake success.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bin.release_workflow.shipping import (
    ACTION_SCOPES,
    CanaryMonitor,
    GitHubShipper,
    ShippingPayloadError,
    UCBTracker,
    build_release_manifest,
)

FIXTURES = Path(__file__).parent / "fixtures" / "shipping"

# A canary secret VALUE that must never leak into results/manifests. The
# tests set env vars to this and scan every produced artifact for it.
SECRET_VALUE = "ghp_SUPERSECRET_do_not_leak_0123456789"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


class RecordingTransport:
    """Fake transport: records (method, url, payload) calls and replays
    canned responses in order."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, payload=None):
        self.calls.append((method, url, payload))
        return self.responses.pop(0)


def deep_scan(obj, needle: str) -> bool:
    """True when the needle string appears anywhere in the structure."""
    return needle in json.dumps(obj, default=str)


# ── GitHubShipper: PR upsert ─────────────────────────────────────────────


def test_pr_created_when_none_exists():
    transport = RecordingTransport([[], load("pr_created.json")])
    shipper = GitHubShipper(transport, repo="decentraland/unity-explorer")
    result = shipper.create_or_update_pr(
        "release/wave-8", "dev", "release: unity-explorer wave-8", "body text"
    )
    assert result == {
        "number": 4300,
        "url": "https://github.com/decentraland/unity-explorer/pull/4300",
        "action": "created",
    }
    # search first, then POST with the correct payload
    method, url, payload = transport.calls[0]
    assert method == "GET"
    assert (
        url
        == "https://api.github.com/repos/decentraland/unity-explorer/pulls"
        "?state=open&head=decentraland:release/wave-8"
    )
    assert payload is None
    method, url, payload = transport.calls[1]
    assert method == "POST"
    assert url == "https://api.github.com/repos/decentraland/unity-explorer/pulls"
    assert payload == {
        "title": "release: unity-explorer wave-8",
        "head": "release/wave-8",
        "base": "dev",
        "body": "body text",
    }


def test_pr_updated_when_open_pr_exists():
    transport = RecordingTransport(
        [load("pr_search_existing.json"), load("pr_updated.json")]
    )
    shipper = GitHubShipper(transport, repo="decentraland/unity-explorer")
    result = shipper.create_or_update_pr(
        "bugsweep/avatar-lod-bounds",
        "dev",
        "fix: bounds check in avatar LOD swap (v2)",
        "updated body",
    )
    assert result["action"] == "updated"
    assert result["number"] == 4211
    assert result["url"].endswith("/pull/4211")
    method, url, payload = transport.calls[1]
    assert method == "PATCH"
    assert url == (
        "https://api.github.com/repos/decentraland/unity-explorer/pulls/4211"
    )
    assert payload == {
        "title": "fix: bounds check in avatar LOD swap (v2)",
        "body": "updated body",
        "base": "dev",
    }
    assert len(transport.calls) == 2  # exactly search + patch, no create


def test_pr_search_bad_shape_raises():
    shipper = GitHubShipper(
        RecordingTransport([{"not": "a list"}]),
        repo="decentraland/unity-explorer",
    )
    with pytest.raises(ShippingPayloadError):
        shipper.create_or_update_pr("b", "dev", "t", "")


def test_pr_create_missing_number_never_maps_to_success():
    shipper = GitHubShipper(
        RecordingTransport([[], {"html_url": "https://x", "state": "open"}]),
        repo="decentraland/unity-explorer",
    )
    with pytest.raises(ShippingPayloadError):
        shipper.create_or_update_pr("b", "dev", "t", "")


# ── GitHubShipper: CI status mapping ─────────────────────────────────────


def test_ci_status_all_good_conclusions_is_success():
    shipper = GitHubShipper(
        RecordingTransport([load("check_runs_success.json")]),
        repo="decentraland/unity-explorer",
    )
    out = shipper.get_ci_status("c08a72ce5187")
    assert out["state"] == "success"
    assert {c["name"] for c in out["checks"]} == {
        "unit-tests", "lint", "asset-validation",
    }


def test_ci_status_mixed_conclusions_is_failure():
    shipper = GitHubShipper(
        RecordingTransport([load("check_runs_mixed.json")]),
        repo="decentraland/unity-explorer",
    )
    out = shipper.get_ci_status("c08a72ce5187")
    assert out["state"] == "failure"
    # every check surfaced, including the passing ones
    assert {(c["name"], c["conclusion"]) for c in out["checks"]} == {
        ("unit-tests", "success"),
        ("playmode-tests", "failure"),
        ("lint", "skipped"),
    }


def test_ci_status_incomplete_run_is_pending():
    shipper = GitHubShipper(
        RecordingTransport([load("check_runs_pending.json")]),
        repo="decentraland/unity-explorer",
    )
    assert shipper.get_ci_status("c08a72ce5187")["state"] == "pending"


def test_ci_status_zero_runs_is_pending():
    shipper = GitHubShipper(
        RecordingTransport([{"total_count": 0, "check_runs": []}]),
        repo="decentraland/unity-explorer",
    )
    assert shipper.get_ci_status("deadbeef")["state"] == "pending"


def test_ci_status_missing_check_runs_key_raises():
    shipper = GitHubShipper(
        RecordingTransport([{"weird": True}]),
        repo="decentraland/unity-explorer",
    )
    with pytest.raises(ShippingPayloadError):
        shipper.get_ci_status("deadbeef")


# ── UCBTracker ───────────────────────────────────────────────────────────


def test_ucb_trigger_maps_build_number_and_status():
    transport = RecordingTransport([load("ucb_trigger.json")])
    tracker = UCBTracker(transport, org="dcl", project="unity-explorer")
    out = tracker.trigger_build("windows-dev", "c08a72ce5187")
    assert out == {"build_number": 187, "status": "queued"}
    method, url, payload = transport.calls[0]
    assert method == "POST"
    assert url == (
        "https://build-api.cloud.unity3d.com/api/v1/orgs/dcl/projects/"
        "unity-explorer/buildtargets/windows-dev/builds"
    )
    assert payload == {"clean": False, "commit": "c08a72ce5187"}


def test_ucb_poll_success_includes_artifact_url():
    transport = RecordingTransport([load("ucb_poll_success.json")])
    tracker = UCBTracker(transport, org="dcl", project="unity-explorer")
    out = tracker.poll_build("windows-dev", 187)
    assert out["status"] == "success"
    assert out["artifact_url"].endswith("/windows-dev/187.zip")
    method, url, _ = transport.calls[0]
    assert method == "GET"
    assert url.endswith("/buildtargets/windows-dev/builds/187")


def test_ucb_poll_building_has_no_artifact_url():
    tracker = UCBTracker(
        RecordingTransport([load("ucb_poll_building.json")]),
        org="dcl",
        project="unity-explorer",
    )
    out = tracker.poll_build("windows-dev", 187)
    assert out == {"status": "building"}
    assert "artifact_url" not in out  # absence is honest, never padded


def test_ucb_trigger_missing_build_number_raises():
    tracker = UCBTracker(
        RecordingTransport([[{"buildStatus": "queued"}]]),
        org="dcl",
        project="unity-explorer",
    )
    with pytest.raises(ShippingPayloadError):
        tracker.trigger_build("windows-dev", "abc")


# ── CanaryMonitor ────────────────────────────────────────────────────────


def test_canary_probe_injection_healthy():
    monitor = CanaryMonitor(probe=lambda url: (200, "HTTP 200"))
    out = monitor.check("https://canary.example/unity")
    assert out == {"healthy": True, "detail": "HTTP 200"}


def test_canary_probe_injection_unhealthy_status():
    monitor = CanaryMonitor(probe=lambda url: (503, "HTTP 503"))
    assert monitor.check("https://canary.example/unity")["healthy"] is False


def test_canary_probe_exception_is_unhealthy_not_raised():
    def broken(url):
        raise TimeoutError("canary timed out")

    out = CanaryMonitor(probe=broken).check("https://canary.example/unity")
    assert out["healthy"] is False
    assert "TimeoutError" in out["detail"]
    assert "canary timed out" in out["detail"]


def test_rollback_plan_is_documented_noop_contract():
    plan = CanaryMonitor(probe=lambda u: (200, "ok")).rollback_plan(
        {"build_id": "ucb:windows-dev#187"}
    )
    assert plan["action"] == "rollback"
    assert plan["executed"] is False  # never fake-executed
    assert plan["build"] == {"build_id": "ucb:windows-dev#187"}
    assert plan["scope"] == ACTION_SCOPES["promote"]
    assert any("canary" in step for step in plan["requires"])
    assert any("production pointer" in step for step in plan["requires"])


# ── credential gating: unset env -> RuntimeError naming the var ─────────


def test_github_token_env_gating(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        GitHubShipper(repo="decentraland/unity-explorer")


def test_github_custom_token_env_gating(monkeypatch):
    monkeypatch.delenv("SHIP_GH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="SHIP_GH_TOKEN"):
        GitHubShipper(
            repo="decentraland/unity-explorer", token_env="SHIP_GH_TOKEN"
        )


def test_ucb_api_key_env_gating(monkeypatch):
    monkeypatch.delenv("UNITY_CLOUD_BUILD_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="UNITY_CLOUD_BUILD_API_KEY"):
        UCBTracker(org="dcl", project="unity-explorer")


def test_action_scopes_are_separate_per_action():
    """push / pr / promote are SEPARATE approvable actions (the real
    campaign allowed push but forbade PRs)."""
    assert ACTION_SCOPES == {
        "push": "ship:push:*",
        "pr": "ship:pr:*",
        "promote": "ship:promote:*",
    }
    assert len(set(ACTION_SCOPES.values())) == 3


# ── release manifest ─────────────────────────────────────────────────────


def _manifest(**overrides):
    kwargs = dict(
        repo="decentraland/unity-explorer",
        commit="c08a72ce5187",
        snapshot_digest="sha256:abc123",
        work_order_ids=["wo-1", "wo-2"],
        approval_event_ids=["appr-9"],
        pr={"number": 4300, "url": "https://github.com/x/pull/4300",
            "action": "created"},
        builds=[{"build_number": 187, "status": "success"}],
        canary={"healthy": True, "detail": "HTTP 200"},
        outcome="promoted",
    )
    kwargs.update(overrides)
    return build_release_manifest(**kwargs)


def test_manifest_digest_deterministic_across_written_at():
    a = _manifest(written_at="2026-08-17T10:00:00Z")
    b = _manifest(written_at="2026-08-18T23:59:59Z")
    assert a["manifest_digest"] == b["manifest_digest"]
    assert len(a["manifest_digest"]) == 64
    assert a["written_at"] != b["written_at"]
    assert a["schema_version"] == 1


def test_manifest_digest_changes_with_content():
    a = _manifest()
    b = _manifest(outcome="rolled_back")
    assert a["manifest_digest"] != b["manifest_digest"]


def test_manifest_required_field_validation():
    with pytest.raises(ValueError, match="repo"):
        _manifest(repo="")
    with pytest.raises(ValueError, match="commit"):
        _manifest(commit="")
    with pytest.raises(ValueError, match="outcome"):
        _manifest(outcome="")
    with pytest.raises(ValueError, match="work_order_ids"):
        _manifest(work_order_ids=[1, 2])
    with pytest.raises(ValueError, match="approval_event_ids"):
        _manifest(approval_event_ids="not-a-list")


def test_manifest_carries_all_audit_links():
    m = _manifest()
    assert m["work_order_ids"] == ["wo-1", "wo-2"]
    assert m["approval_event_ids"] == ["appr-9"]
    assert m["pr"]["number"] == 4300
    assert m["builds"][0]["build_number"] == 187
    assert m["canary"]["healthy"] is True
    assert m["snapshot_digest"] == "sha256:abc123"
    assert "written_at" in m


# ── secret hygiene: the secret VALUE never appears anywhere ─────────────


def test_no_secret_value_in_any_result_or_manifest(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", SECRET_VALUE)
    monkeypatch.setenv("UNITY_CLOUD_BUILD_API_KEY", SECRET_VALUE)

    transport = RecordingTransport(
        [
            [], load("pr_created.json"),          # PR upsert
            load("check_runs_mixed.json"),        # CI status
            load("ucb_trigger.json"),             # UCB trigger
            load("ucb_poll_success.json"),        # UCB poll
        ]
    )
    shipper = GitHubShipper(transport, repo="decentraland/unity-explorer")
    pr = shipper.create_or_update_pr("release/wave-8", "dev", "t", "b")
    ci = shipper.get_ci_status("c08a72ce5187")

    tracker = UCBTracker(transport, org="dcl", project="unity-explorer")
    trig = tracker.trigger_build("windows-dev", "c08a72ce5187")
    poll = tracker.poll_build("windows-dev", 187)

    monitor = CanaryMonitor(probe=lambda u: (200, "HTTP 200"))
    canary = monitor.check("https://canary.example/unity")
    plan = monitor.rollback_plan({"build_number": 187})

    manifest = build_release_manifest(
        repo="decentraland/unity-explorer",
        commit="c08a72ce5187",
        snapshot_digest="sha256:abc",
        work_order_ids=["wo-1"],
        approval_event_ids=["appr-1"],
        pr=pr,
        builds=[trig, poll],
        canary=canary,
        outcome="promoted",
    )
    for artifact in (pr, ci, trig, poll, canary, plan, manifest):
        assert not deep_scan(artifact, SECRET_VALUE)
    # transport never received the secret in any payload either
    assert not deep_scan(
        [(m, u, p) for (m, u, p) in transport.calls], SECRET_VALUE
    )

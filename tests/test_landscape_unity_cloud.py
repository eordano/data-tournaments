"""Tests for bin/landscape/adapters/unity_cloud.py — fixture-driven, no network."""
from __future__ import annotations

import pytest

from bin.landscape.adapters import adapter_kinds, get_adapter, unity_cloud
from bin.landscape.evidence import SourceType, TrustTier


def _build_payload(**over):
    base = {
        "build": 412,
        "buildtargetid": "t_macos",
        "buildStatus": "success",
        "scmBranch": "main",
        "lastBuiltRevision": "8be52b3847f7" + "0" * 28,
        "totalTimeInSeconds": 1841,
        "created": "2026-08-16T22:10:00Z",
        "finished": "2026-08-16T22:40:41Z",
    }
    base.update(over)
    return base


def test_registered_in_adapter_registry():
    assert "unity_cloud" in adapter_kinds()
    assert get_adapter("unity_cloud") is unity_cloud


def test_parse_build_is_tier1_system():
    ref = unity_cloud.parse_build("dcl", "explorer", _build_payload(), why="ci state")
    assert ref.trust_tier is TrustTier.TIER1_SYSTEM
    assert ref.source_type is SourceType.CI_BUILD
    assert ref.canonical_uri == "ucb:dcl/explorer/t_macos#412"
    assert ref.revision.startswith("8be52b3847f7")
    assert "status=success" in ref.excerpt
    assert "branch: main" in ref.excerpt


def test_browsable_link_is_https_ci_kind():
    ref = unity_cloud.parse_build("dcl", "explorer", _build_payload(), why="w")
    assert ref.browsable_link is not None
    assert ref.browsable_link.kind == "ci"
    assert ref.browsable_link.url.startswith(
        "https://developer.cloud.unity3d.com/build/orgs/dcl/projects/explorer/"
    )


def test_missing_required_fields_raise_never_skip():
    for missing in ("build", "buildtargetid", "buildStatus"):
        payload = _build_payload()
        del payload[missing]
        with pytest.raises(unity_cloud.UnityCloudPayloadError, match=missing):
            unity_cloud.parse_build("o", "p", payload, why="w")
    with pytest.raises(unity_cloud.UnityCloudPayloadError):
        unity_cloud.parse_build("o", "p", "not-a-dict", why="w")  # type: ignore[arg-type]


def test_excerpt_bounded():
    payload = _build_payload(scmBranch="b" * 5000)
    ref = unity_cloud.parse_build("o", "p", payload, why="w", max_chars=200)
    assert len(ref.excerpt) <= 200


def test_collect_limits_and_config_validation():
    builds = [_build_payload(build=n) for n in range(1, 6)]
    refs = unity_cloud.collect(
        {"org": "dcl", "project": "explorer", "builds": builds},
        why="recent ci",
        limits={"max_items": 3},
    )
    assert len(refs) == 3
    assert [r.canonical_uri.rsplit("#", 1)[1] for r in refs] == ["1", "2", "3"]
    with pytest.raises(unity_cloud.UnityCloudPayloadError, match="org and project"):
        unity_cloud.collect({"builds": builds}, why="w")


def test_fetch_requires_named_env_key(monkeypatch):
    monkeypatch.delenv("UNITY_CLOUD_BUILD_API_KEY", raising=False)
    with pytest.raises(unity_cloud.UnityCloudPayloadError, match="env var"):
        unity_cloud.fetch({"org": "dcl", "project": "explorer"})


def test_digest_deterministic():
    a = unity_cloud.parse_build("dcl", "explorer", _build_payload(), why="w")
    b = unity_cloud.parse_build("dcl", "explorer", _build_payload(), why="w")
    assert a.digest == b.digest
    c = unity_cloud.parse_build(
        "dcl", "explorer", _build_payload(buildStatus="failure"), why="w"
    )
    assert c.digest != a.digest

"""Tests for bin/sandbox — profiles, fake backend, preflight evidence."""
import pydantic
import pytest

from bin.sandbox import (
    EgressPolicy,
    FakeSandboxBackend,
    SandboxProfile,
    SandboxRunRequest,
    SecretRef,
    get_backend,
    preflight_evidence,
)
from bin.landscape.evidence import TrustTier

def _profile(**over):
    base = dict(
        name="preflight",
        backend="fake",
        flake_ref="github:decentraland/unity-explorer?rev=abc123",
        repo="github.com:decentraland/unity-explorer",
        base_commit="a" * 40,
        read_only=True,
    )
    base.update(over)
    return SandboxProfile(**base)

def test_secret_ref_accepts_named_refs_only():
    SecretRef(ref="secret://unity-cloud/api-key")
    with pytest.raises(pydantic.ValidationError):
        SecretRef(ref="sk-live-abcdef0123456789")
    with pytest.raises(pydantic.ValidationError):
        SecretRef(ref="https://example.com/key")
    with pytest.raises(pydantic.ValidationError):
        SecretRef(ref="secret://UPPER/Bad")

def test_secret_ref_exposure_validated():
    SecretRef(ref="secret://a/b", exposure="env")
    with pytest.raises(pydantic.ValidationError):
        SecretRef(ref="secret://a/b", exposure="plaintext")

def test_egress_deny_all_rejects_allowlists():
    with pytest.raises(pydantic.ValidationError):
        EgressPolicy(deny_all=True, allow_domains=("github.com",))

def test_egress_domains_must_be_bare_hostnames():
    with pytest.raises(pydantic.ValidationError):
        EgressPolicy(allow_domains=("https://github.com",))
    p = EgressPolicy(allow_domains=("b.com", "a.com", "b.com"))
    assert p.allow_domains == ("a.com", "b.com")

def test_profile_defaults_are_locked_down():
    p = _profile()
    assert p.read_only is True
    assert p.egress.deny_all is True
    assert p.secrets == ()

def test_profile_digest_deterministic_and_content_sensitive():
    assert _profile().digest == _profile().digest
    assert _profile().digest != _profile(base_commit="b" * 40).digest

def test_write_profile_requires_pinned_commit():
    with pytest.raises(pydantic.ValidationError):
        _profile(read_only=False, base_commit="")
    _profile(read_only=False)

def test_profile_frozen():
    with pytest.raises(pydantic.ValidationError):
        _profile().read_only = False

def test_unknown_backend_rejected():
    with pytest.raises(pydantic.ValidationError):
        _profile(backend="docker")

def test_fake_backend_success_and_failure_paths():
    be = FakeSandboxBackend(clock=lambda: "2026-08-17T00:00:00Z")
    req = SandboxRunRequest(
        profile=_profile(),
        workorder_id="wo-1",
        commands=("run tests", "FAIL: broken step", "package"),
    )
    result = be.run(req)
    assert result.ok is False
    codes = [c.exit_code for c in result.commands]
    assert codes == [0, 1, 0]
    assert result.profile_digest == _profile().digest

    ok_result = be.run(
        SandboxRunRequest(profile=_profile(), commands=("a", "b"))
    )
    assert ok_result.ok is True

def test_fake_backend_deterministic_output():
    be = FakeSandboxBackend(clock=lambda: "t")
    req = SandboxRunRequest(profile=_profile(), commands=("x",))
    assert be.run(req) == be.run(req)

def test_get_backend_fake_and_unknown():
    assert get_backend("fake").name == "fake"
    with pytest.raises(RuntimeError, match="unknown sandbox backend"):
        get_backend("bogus")

def test_preflight_evidence_is_tier1_and_bounded():
    be = FakeSandboxBackend(clock=lambda: "2026-08-17T00:00:00Z")
    result = be.run(
        SandboxRunRequest(
            profile=_profile(),
            workorder_id="wo-9",
            commands=tuple(f"step {i}" for i in range(200)),
        )
    )
    ref = preflight_evidence(
        result, retrieved_at="2026-08-17T00:00:01Z", why="preflight for wo-9"
    )
    assert ref.trust_tier is TrustTier.TIER1_SYSTEM
    assert ref.canonical_uri.startswith("sandbox:fake:")
    assert "wo-9" in ref.canonical_uri
    assert len(ref.excerpt) <= 2000

def test_preflight_evidence_mentions_failure():
    be = FakeSandboxBackend(clock=lambda: "t")
    result = be.run(
        SandboxRunRequest(profile=_profile(), commands=("FAIL: x",))
    )
    ref = preflight_evidence(result, retrieved_at="t", why="w")
    assert "ok=False" in ref.excerpt
    assert "exit 1" in ref.excerpt

def test_no_plaintext_secret_anywhere_in_profile_dump():
    p = _profile(secrets=(SecretRef(ref="secret://unity-cloud/api-key"),))
    dumped = str(p.model_dump())
    assert "secret://unity-cloud/api-key" in dumped
    assert "value" not in dumped

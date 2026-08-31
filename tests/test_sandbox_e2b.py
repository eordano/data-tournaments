"""Tests for bin/sandbox/e2b_backend.py — all offline via injected factory.

The real E2B SDK/network is never touched: a fake sandbox factory stands in,
so these tests validate the mapping logic (egress, secrets, pinning,
failure encoding) rather than the vendor API.
"""
from __future__ import annotations

import pytest

from bin.sandbox.backend import SandboxRunRequest, get_backend
from bin.sandbox.e2b_backend import E2BSandboxBackend
from bin.sandbox.profile import EgressPolicy, SandboxProfile, SecretRef

class _FakeProc:
    def __init__(self, exit_code=0, stdout="ok", stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr

class _FakeSandbox:
    """Records commands; scripted exit codes via 'FAIL' marker."""

    last_create_kwargs: dict = {}
    ran_commands: list = []

    def __init__(self):
        self.commands = self

    @classmethod
    def create(cls, timeout=None, **kwargs):
        cls.last_create_kwargs = {"timeout": timeout, **kwargs}
        cls.ran_commands = []
        return cls()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def run(self, cmd, cwd=None, timeout=None):
        type(self).ran_commands.append(cmd)
        return _FakeProc(exit_code=1 if "FAIL" in cmd else 0, stdout=f"ran {cmd}")

def _profile(**over):
    base = dict(
        name="preflight",
        backend="e2b",
        repo="github.com:decentraland/unity-explorer",
        base_commit="a" * 40,
        read_only=True,
        egress=EgressPolicy(deny_all=True),
    )
    base.update(over)
    return SandboxProfile(**base)

def _backend():
    return E2BSandboxBackend(sandbox_factory=_FakeSandbox)

def test_requires_api_key_without_injection(monkeypatch):
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="E2B_API_KEY"):
        E2BSandboxBackend()

def test_get_backend_e2b_raises_clearly_when_unconfigured(monkeypatch):
    monkeypatch.delenv("E2B_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="e2b backend"):
        get_backend("e2b")

def test_deny_all_maps_to_no_internet():
    be = _backend()
    be.run(SandboxRunRequest(profile=_profile(), commands=("echo hi",)))
    assert _FakeSandbox.last_create_kwargs["allow_internet_access"] is False

def test_allow_domains_map_to_allowlist():
    be = _backend()
    prof = _profile(
        egress=EgressPolicy(allow_domains=("github.com", "api.github.com"))
    )
    be.run(SandboxRunRequest(profile=prof, commands=("echo hi",)))
    assert _FakeSandbox.last_create_kwargs["allowed_domains"] == [
        "api.github.com",
        "github.com",
    ]

def test_workspace_pinned_to_base_commit():
    be = _backend()
    result = be.run(SandboxRunRequest(profile=_profile(), commands=("run tests",)))
    cmds = _FakeSandbox.ran_commands
    assert any("git clone" in c and "github.com/decentraland" in c for c in cmds)
    assert any(f"checkout --quiet {'a' * 40}" in c for c in cmds)
    assert cmds[-1] == "run tests"
    assert result.ok is True
    assert result.backend == "e2b"

def test_pin_failure_aborts_before_user_commands():
    class _PinFailSandbox(_FakeSandbox):
        def run(self, cmd, cwd=None, timeout=None):
            type(self).ran_commands.append(cmd)
            code = 1 if cmd.startswith("git clone") else 0
            return _FakeProc(exit_code=code)

    be = E2BSandboxBackend(sandbox_factory=_PinFailSandbox)
    result = be.run(
        SandboxRunRequest(profile=_profile(), commands=("must not run",))
    )
    assert result.ok is False
    assert "workspace pin failed" in result.error
    assert "must not run" not in _PinFailSandbox.ran_commands

def test_command_failure_is_encoded_not_raised():
    be = _backend()
    result = be.run(
        SandboxRunRequest(profile=_profile(), commands=("FAIL: boom",))
    )
    assert result.ok is False
    assert result.error == ""
    assert result.commands[-1].exit_code == 1

def test_proxy_exposure_secrets_refuse_to_degrade():
    be = _backend()
    prof = _profile(
        secrets=(SecretRef(ref="secret://unity-cloud/api-key"),)
    )
    with pytest.raises(RuntimeError, match="refusing to degrade"):
        be.run(SandboxRunRequest(profile=prof, commands=("x",)))

def test_env_secrets_resolved_from_worker_env(monkeypatch):
    monkeypatch.setenv("API_KEY", "resolved-at-runtime")
    be = _backend()
    prof = _profile(
        secrets=(SecretRef(ref="secret://unity-cloud/api-key", exposure="env"),)
    )
    be.run(SandboxRunRequest(profile=prof, commands=("x",)))
    assert _FakeSandbox.last_create_kwargs["envs"] == {
        "API_KEY": "resolved-at-runtime"
    }

def test_env_secret_missing_raises(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    be = _backend()
    prof = _profile(
        secrets=(SecretRef(ref="secret://unity-cloud/api-key", exposure="env"),)
    )
    with pytest.raises(RuntimeError, match="not present in the worker"):
        be.run(SandboxRunRequest(profile=prof, commands=("x",)))

def test_infra_exception_becomes_error_result():
    class _ExplodingSandbox(_FakeSandbox):
        @classmethod
        def create(cls, timeout=None, **kwargs):
            raise ConnectionError("e2b api unreachable")

    be = E2BSandboxBackend(sandbox_factory=_ExplodingSandbox)
    result = be.run(SandboxRunRequest(profile=_profile(), commands=("x",)))
    assert result.ok is False
    assert "e2b api unreachable" in result.error

def test_no_secret_value_in_result(monkeypatch):
    monkeypatch.setenv("API_KEY", "super-secret-value")
    be = _backend()
    prof = _profile(
        secrets=(SecretRef(ref="secret://unity-cloud/api-key", exposure="env"),)
    )
    result = be.run(SandboxRunRequest(profile=prof, commands=("echo hi",)))
    assert "super-secret-value" not in result.model_dump_json()

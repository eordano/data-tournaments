"""Tests for bin/sandbox/bwrap_backend.py — all offline via injected runner.

bwrap/nix are never invoked: a recording runner stands in, so these verify
the request->jail mapping (egress refusal, secret handling, flake_ref ->
TOOLCHAIN, workspace pinning, result encoding). The jail itself is verified
manually per scripts/lint/README.md.
"""
import pytest

from bin.sandbox import EgressPolicy, SandboxProfile, SandboxRunRequest, SecretRef
from bin.sandbox.bwrap_backend import BwrapSandboxBackend


class _RecordingRunner:
    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = tuple(fail_on)

    def __call__(self, argv, env, timeout, cwd=None):
        self.calls.append((list(argv), dict(env), timeout, cwd))
        joined = " ".join(argv)
        for frag in self.fail_on:
            if frag in joined:
                return 1, f"boom: {frag}"
        if argv[:2] == ["nix", "build"]:
            return 0, "/nix/store/fake-lint-jail-toolchain\n"
        return 0, f"ran: {joined}"


def _profile(**over):
    base = dict(
        name="bwrap-test",
        backend="bwrap",
        egress=EgressPolicy(deny_all=True),
        read_only=True,
    )
    base.update(over)
    return SandboxProfile(**base)


def _backend(tmp_path, runner=None):
    return BwrapSandboxBackend(runner=runner or _RecordingRunner(), repo_root=tmp_path)


def test_profile_accepts_bwrap_backend():
    assert _profile().backend == "bwrap"


def test_egress_profiles_refuse_to_degrade(tmp_path):
    be = _backend(tmp_path)
    req = SandboxRunRequest(
        profile=_profile(egress=EgressPolicy(allow_domains=("github.com",))),
        commands=("true",),
    )
    with pytest.raises(RuntimeError, match="offline-only"):
        be.run(req)


def test_proxy_exposure_secrets_refuse_to_degrade(tmp_path):
    be = _backend(tmp_path)
    req = SandboxRunRequest(
        profile=_profile(
            secrets=(SecretRef(ref="secret://scope/api-key"),)
        ),
        commands=("true",),
    )
    with pytest.raises(RuntimeError, match="refusing to degrade"):
        be.run(req)


def test_env_secrets_resolved_from_worker_env(tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEY", "resolved-value")
    runner = _RecordingRunner()
    be = _backend(tmp_path, runner)
    req = SandboxRunRequest(
        profile=_profile(
            secrets=(SecretRef(ref="secret://scope/api-key", exposure="env"),)
        ),
        commands=("echo hi",),
    )
    result = be.run(req)
    assert result.ok
    argv, env, _, _ = runner.calls[-1]
    assert env["API_KEY"] == "resolved-value"
    assert "resolved-value" not in result.model_dump_json()


def test_env_secret_missing_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    be = _backend(tmp_path)
    req = SandboxRunRequest(
        profile=_profile(
            secrets=(SecretRef(ref="secret://scope/api-key", exposure="env"),)
        ),
        commands=("true",),
    )
    with pytest.raises(RuntimeError, match="API_KEY not present"):
        be.run(req)


def test_flake_ref_built_and_passed_as_toolchain(tmp_path):
    runner = _RecordingRunner()
    be = _backend(tmp_path, runner)
    req = SandboxRunRequest(
        profile=_profile(flake_ref="path:scripts/lint#toolchain"),
        commands=("echo hi",),
    )
    result = be.run(req)
    assert result.ok
    nix_argv, _, _, _ = runner.calls[0]
    assert nix_argv[:2] == ["nix", "build"]
    assert nix_argv[-1] == "path:scripts/lint#toolchain"
    _, env, _, _ = runner.calls[-1]
    assert env["TOOLCHAIN"] == "/nix/store/fake-lint-jail-toolchain"


def test_flake_ref_build_failure_is_infrastructure_error(tmp_path):
    be = _backend(tmp_path, _RecordingRunner(fail_on=("nix build",)))
    req = SandboxRunRequest(
        profile=_profile(flake_ref="path:scripts/lint#toolchain"),
        commands=("true",),
    )
    with pytest.raises(RuntimeError, match="nix build"):
        be.run(req)


def test_commands_run_through_jail_exec(tmp_path):
    runner = _RecordingRunner()
    be = _backend(tmp_path, runner)
    req = SandboxRunRequest(profile=_profile(), commands=("dotnet test",))
    result = be.run(req)
    assert result.ok
    argv, _, timeout, cwd = runner.calls[-1]
    assert argv[0].endswith("scripts/lint/lint-jail.sh")
    assert argv[1] == "exec"
    assert argv[3:] == ["bash", "-c", "dotnet test"]
    assert timeout == req.profile.timeout_seconds
    assert cwd == str(tmp_path)


def test_workspace_pinned_to_base_commit(tmp_path):
    runner = _RecordingRunner()
    be = _backend(tmp_path, runner)
    req = SandboxRunRequest(
        profile=_profile(
            repo="github.com:decentraland/unity-explorer",
            base_commit="a" * 40,
        ),
        commands=("true",),
    )
    result = be.run(req)
    assert result.ok
    clone_argv = runner.calls[0][0]
    assert clone_argv[:3] == ["git", "clone", "--quiet"]
    assert clone_argv[3] == "https://github.com/decentraland/unity-explorer"
    checkout_argv = runner.calls[1][0]
    assert checkout_argv[3:] == ["checkout", "--quiet", "a" * 40]


def test_pin_failure_aborts_before_user_commands(tmp_path):
    runner = _RecordingRunner(fail_on=("git clone",))
    be = _backend(tmp_path, runner)
    req = SandboxRunRequest(
        profile=_profile(repo="/some/local/repo", base_commit="b" * 40),
        commands=("echo never",),
    )
    result = be.run(req)
    assert not result.ok
    assert result.error.startswith("workspace pin failed")
    assert all("echo never" not in c.command for c in result.commands)


def test_command_failure_is_encoded_not_raised(tmp_path):
    runner = _RecordingRunner(fail_on=("false-cmd",))
    be = _backend(tmp_path, runner)
    req = SandboxRunRequest(profile=_profile(), commands=("false-cmd", "echo after"))
    result = be.run(req)
    assert not result.ok
    assert result.error == ""
    assert [c.exit_code for c in result.commands] == [1, 0]


def test_infra_exception_becomes_error_result(tmp_path):
    def exploding(argv, env, timeout, cwd=None):
        raise OSError("cannot spawn")

    be = _backend(tmp_path, exploding)
    result = be.run(SandboxRunRequest(profile=_profile(), commands=("true",)))
    assert not result.ok
    assert "cannot spawn" in result.error


def test_workspace_cleaned_up(tmp_path):
    be = _backend(tmp_path)
    be.run(SandboxRunRequest(profile=_profile(), commands=("true",)))
    runs = tmp_path / ".lint-jail-cache" / "runs"
    assert list(runs.iterdir()) == []

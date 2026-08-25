"""Tests for bin/branch_validator.py — isolated per-branch validation
(wave-9 B2). End-to-end against real temp git repos whose red/green/guard
commands are tiny shell scripts printing the parseable conventions
('RED a/b', 'GREEN a/b', 'GUARD a/b').
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests.test_fix_branches import _commit, _env, _git, _make_repo


@pytest.fixture
def fb(tmp_data_home):
    from bin import fix_branches as mod

    mod.init()
    return mod


@pytest.fixture
def validator(fb):
    from bin import branch_validator as mod

    return mod


@pytest.fixture
def repo(tmp_path) -> Path:
    return _make_repo(tmp_path)


def _script(repo: Path, name: str, line: str) -> None:
    path = repo / name
    path.write_text(f"#!/bin/sh\necho '{line}'\n")
    path.chmod(0o755)
    _git(repo, "add", name)


def _fixture_branch(repo: Path, name: str, *, guard_line: str) -> str:
    """A fix branch carrying its own red/green/guard scripts."""
    _git(repo, "checkout", "-b", name)
    _script(repo, "red.sh", "RED 2/2")
    _script(repo, "green.sh", "GREEN 5/5")
    _script(repo, "guard.sh", guard_line)
    (repo / "fix.txt").write_text(f"fix on {name}\n")
    _git(repo, "add", "fix.txt")
    _git(repo, "commit", "-m", f"fix: {name}")
    sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "main")
    return sha


def _no_worktrees_left(repo: Path) -> bool:
    out = _git(repo, "worktree", "list", "--porcelain")
    # only the main worktree remains
    return out.count("worktree ") == 1


class TestValidateEndToEnd:
    def test_passing_branch(self, fb, validator, repo, tmp_path, tmp_data_home):
        sha = _fixture_branch(repo, "fix/good", guard_line="GUARD 3/3")
        bid = fb.register_branch(str(repo), "fix/good")
        res = validator.validate(
            bid,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            guard_cmd="./guard.sh",
            scratch_dir=str(tmp_path / "scratch"),
        )
        assert res["passed"] is True
        assert res["tested_sha"] == sha
        assert res["red"] == (2, 2)
        assert res["green"] == (5, 5)
        assert res["guard"] == (3, 3)
        b = fb.get_branch(bid)
        assert b["status"] == "validated"
        cv = fb.current_validation(bid)
        assert cv is not None
        assert cv["tested_sha"] == sha
        assert cv["passed"] == 1
        assert cv["red_observed"] == 2 and cv["red_intended"] == 2
        assert cv["green_passed"] == 5 and cv["green_total"] == 5
        assert cv["guard_passed"] == 3 and cv["guard_total"] == 3
        # log stored content-addressed; digest recorded
        from bin import catalog

        assert cv["log_digest"] == res["log_digest"]
        log = catalog.cas_read(cv["log_digest"])
        assert "RED 2/2" in log and "GREEN 5/5" in log and "GUARD 3/3" in log
        # worktree ALWAYS removed
        assert _no_worktrees_left(repo)

    def test_failing_guard_branch(self, fb, validator, repo, tmp_path):
        _fixture_branch(repo, "fix/bad-guard", guard_line="GUARD 2/3")
        bid = fb.register_branch(str(repo), "fix/bad-guard")
        res = validator.validate(
            bid,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            guard_cmd="./guard.sh",
            scratch_dir=str(tmp_path / "scratch"),
        )
        assert res["passed"] is False
        assert res["guard"] == (2, 3)
        b = fb.get_branch(bid)
        assert b["status"] == "failed"
        assert fb.current_validation(bid)["passed"] == 0
        assert _no_worktrees_left(repo)

    def test_guard_optional(self, fb, validator, repo, tmp_path):
        _fixture_branch(repo, "fix/no-guard", guard_line="GUARD 0/0")
        bid = fb.register_branch(str(repo), "fix/no-guard")
        res = validator.validate(
            bid,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            scratch_dir=str(tmp_path / "scratch"),
        )
        assert res["passed"] is True
        assert res["guard"] is None
        cv = fb.current_validation(bid)
        assert cv["guard_total"] is None and cv["guard_passed"] is None

    def test_unparseable_output_fails(self, fb, validator, repo, tmp_path):
        """A leg that never prints its convention line => not passed."""
        _fixture_branch(repo, "fix/silent", guard_line="GUARD 3/3")
        bid = fb.register_branch(str(repo), "fix/silent")
        res = validator.validate(
            bid,
            red_cmd="true",  # prints nothing
            green_cmd="./green.sh",
            guard_cmd="./guard.sh",
            scratch_dir=str(tmp_path / "scratch"),
        )
        assert res["passed"] is False
        assert res["red"] is None
        assert fb.get_branch(bid)["status"] == "failed"

    def test_worktree_removed_even_on_crash(self, fb, validator, repo, tmp_path,
                                             monkeypatch):
        _fixture_branch(repo, "fix/crash", guard_line="GUARD 3/3")
        bid = fb.register_branch(str(repo), "fix/crash")

        def boom(*a, **k):
            raise RuntimeError("validator crashed mid-run")

        monkeypatch.setattr(validator, "_run_leg", boom)
        with pytest.raises(RuntimeError, match="crashed"):
            validator.validate(
                bid,
                red_cmd="./red.sh",
                green_cmd="./green.sh",
                scratch_dir=str(tmp_path / "scratch"),
            )
        assert _no_worktrees_left(repo)

    def test_validator_never_touches_main_worktree(self, fb, validator, repo,
                                                   tmp_path):
        """The main worktree stays on main, clean, throughout validation."""
        _fixture_branch(repo, "fix/isolated", guard_line="GUARD 3/3")
        bid = fb.register_branch(str(repo), "fix/isolated")
        before = _git(repo, "rev-parse", "HEAD")
        validator.validate(
            bid,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            guard_cmd="./guard.sh",
            scratch_dir=str(tmp_path / "scratch"),
        )
        assert _git(repo, "rev-parse", "HEAD") == before
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"
        assert _git(repo, "status", "--porcelain") == ""

    def test_stale_branch_validation_refused_at_write(self, fb, validator, repo,
                                                      tmp_path, monkeypatch):
        """If the branch moves AFTER the DB read but BEFORE the row write,
        the staleness guard refuses the validation (tested_sha != head)."""
        _fixture_branch(repo, "fix/racy", guard_line="GUARD 3/3")
        bid = fb.register_branch(str(repo), "fix/racy")

        real_run_leg = validator._run_leg
        moved = {"done": False}

        def move_tip_then_run(cmd, cwd):
            if not moved["done"]:
                moved["done"] = True
                _git(repo, "checkout", "fix/racy")
                _commit(repo, "late.txt", "late\n", "late commit")
                _git(repo, "checkout", "main")
                fb.refresh_head(bid)  # DB head moves past the worktree SHA
            return real_run_leg(cmd, cwd)

        monkeypatch.setattr(validator, "_run_leg", move_tip_then_run)
        with pytest.raises(ValueError, match="does not match current head"):
            validator.validate(
                bid,
                red_cmd="./red.sh",
                green_cmd="./green.sh",
                scratch_dir=str(tmp_path / "scratch"),
            )
        assert _no_worktrees_left(repo)
        assert fb.current_validation(bid) is None

    def test_two_branches_validate_independently(self, fb, validator, repo,
                                                 tmp_path):
        """SHA-binding by construction: each branch's validation row binds
        to ITS OWN head; there is no aggregate tree anywhere."""
        sha_good = _fixture_branch(repo, "fix/one", guard_line="GUARD 3/3")
        sha_bad = _fixture_branch(repo, "fix/two", guard_line="GUARD 1/3")
        bid1 = fb.register_branch(str(repo), "fix/one")
        bid2 = fb.register_branch(str(repo), "fix/two")
        r1 = validator.validate(
            bid1, red_cmd="./red.sh", green_cmd="./green.sh",
            guard_cmd="./guard.sh", scratch_dir=str(tmp_path / "s1"),
        )
        r2 = validator.validate(
            bid2, red_cmd="./red.sh", green_cmd="./green.sh",
            guard_cmd="./guard.sh", scratch_dir=str(tmp_path / "s2"),
        )
        assert r1["tested_sha"] == sha_good and r1["passed"] is True
        assert r2["tested_sha"] == sha_bad and r2["passed"] is False
        assert fb.get_branch(bid1)["status"] == "validated"
        assert fb.get_branch(bid2)["status"] == "failed"


# ── Harness trust (wave-10 V2): the branch cannot grade itself ─────────────


def _script_on_main(repo: Path, name: str, body: str) -> None:
    """Commit a harness script to MAIN (the base the harness is pinned to)."""
    path = repo / name
    path.write_text(body)
    path.chmod(0o755)
    _git(repo, "add", name)


def _base_with_harness(repo: Path) -> None:
    """Commit honest red/green/guard scripts to main."""
    _script_on_main(repo, "red.sh", "#!/bin/sh\necho 'RED 2/2'\n")
    _script_on_main(repo, "green.sh", "#!/bin/sh\necho 'GREEN 5/5'\n")
    _script_on_main(repo, "guard.sh", "#!/bin/sh\necho 'GUARD 3/3'\n")
    _git(repo, "commit", "-m", "harness: base scripts")


class TestHarnessTrust:
    def test_tampered_guard_refused_before_execution(self, fb, validator,
                                                     repo, tmp_path):
        """A branch that rewrites guard.sh to fake passing counters is
        REFUSED before any candidate code runs: the tampered script would
        drop a sentinel marker if executed — the marker must be absent."""
        _base_with_harness(repo)
        marker = tmp_path / "tampered-guard-ran.marker"
        _git(repo, "checkout", "-b", "fix/evil")
        (repo / "guard.sh").write_text(
            f"#!/bin/sh\ntouch {marker}\necho 'GUARD 2/2'\n"
        )
        (repo / "fix.txt").write_text("evil fix\n")
        _git(repo, "add", "guard.sh", "fix.txt")
        _git(repo, "commit", "-m", "fix + tamper harness")
        _git(repo, "checkout", "main")

        bid = fb.register_branch(str(repo), "fix/evil")
        res = validator.validate(
            bid,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            guard_cmd="./guard.sh",
            scratch_dir=str(tmp_path / "scratch"),
        )
        assert res["passed"] is False
        assert res["refused"] == "harness-tampered"
        assert res["tampered_paths"] == ["guard.sh"]
        assert res["harness_digest"] is None
        # the tampered script NEVER executed
        assert not marker.exists()
        b = fb.get_branch(bid)
        assert b["status"] == "failed"
        cv = fb.current_validation(bid)
        assert cv["passed"] == 0
        from bin import catalog

        log = catalog.cas_read(cv["log_digest"])
        assert log.splitlines()[0] == "HARNESS-TAMPERED: guard.sh"

    def test_honest_branch_unaffected_and_digest_recorded(self, fb, validator,
                                                          repo, tmp_path):
        """An honest branch (doesn't touch the harness) still passes; the
        harness digest over the BASE scripts is recorded."""
        _base_with_harness(repo)
        _git(repo, "checkout", "-b", "fix/honest")
        (repo / "fix.txt").write_text("honest fix\n")
        _git(repo, "add", "fix.txt")
        _git(repo, "commit", "-m", "honest fix")
        _git(repo, "checkout", "main")

        bid = fb.register_branch(str(repo), "fix/honest")
        res = validator.validate(
            bid,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            guard_cmd="./guard.sh",
            scratch_dir=str(tmp_path / "scratch"),
        )
        assert res["passed"] is True
        assert res["harness_digest"] is not None
        assert len(res["harness_digest"]) == 64
        assert sorted(res["protected_paths"]) == [
            "green.sh", "guard.sh", "red.sh"
        ]
        assert fb.get_branch(bid)["status"] == "validated"
        from bin import catalog

        log = catalog.cas_read(res["log_digest"])
        assert f"HARNESS-DIGEST: sha256:{res['harness_digest']}" in log

    def test_scripts_run_from_base_even_without_diff_trick(self, fb, validator,
                                                           repo, tmp_path,
                                                           monkeypatch):
        """Belt-and-braces: even if the diff-based tamper check were
        somehow blind (monkeypatched to see nothing), the protected files
        are materialized FROM BASE inside the worktree — the branch's
        doctored guard (GUARD 3/3 + sentinel marker) never runs; the base
        guard (GUARD 2/3) does, so the branch fails on honest counters."""
        marker = tmp_path / "doctored-guard-ran.marker"
        _script_on_main(repo, "red.sh", "#!/bin/sh\necho 'RED 2/2'\n")
        _script_on_main(repo, "green.sh", "#!/bin/sh\necho 'GREEN 5/5'\n")
        _script_on_main(repo, "guard.sh", "#!/bin/sh\necho 'GUARD 2/3'\n")
        _git(repo, "commit", "-m", "harness: failing base guard")
        _git(repo, "checkout", "-b", "fix/doctored")
        (repo / "guard.sh").write_text(
            f"#!/bin/sh\ntouch {marker}\necho 'GUARD 3/3'\n"
        )
        _git(repo, "add", "guard.sh")
        _git(repo, "commit", "-m", "doctor the guard")
        _git(repo, "checkout", "main")
        bid = fb.register_branch(str(repo), "fix/doctored")
        # blind the diff-based check — materialization must still hold
        monkeypatch.setattr(validator, "_tampered_paths",
                            lambda *a, **k: [])
        res = validator.validate(
            bid, red_cmd="./red.sh", green_cmd="./green.sh",
            guard_cmd="./guard.sh", scratch_dir=str(tmp_path / "scratch"),
        )
        assert res["passed"] is False
        assert res["guard"] == (2, 3)  # BASE guard ran, not the doctored one
        assert not marker.exists()

    def test_explicit_protected_paths(self, fb, validator, repo, tmp_path):
        """An explicit protected list extends the default set: a branch
        touching a listed helper is refused even though no cmd names it."""
        _base_with_harness(repo)
        _script_on_main(repo, "helper.sh", "#!/bin/sh\ntrue\n")
        _git(repo, "commit", "-m", "harness helper")
        _git(repo, "checkout", "-b", "fix/helper-tamper")
        (repo / "helper.sh").write_text("#!/bin/sh\nexit 0 # doctored\n")
        _git(repo, "add", "helper.sh")
        _git(repo, "commit", "-m", "tamper helper")
        _git(repo, "checkout", "main")
        bid = fb.register_branch(str(repo), "fix/helper-tamper")
        res = validator.validate(
            bid, red_cmd="./red.sh", green_cmd="./green.sh",
            guard_cmd="./guard.sh",
            protected_paths=["./helper.sh"],
            scratch_dir=str(tmp_path / "scratch"),
        )
        assert res["refused"] == "harness-tampered"
        assert res["tampered_paths"] == ["helper.sh"]
        assert res["protected_source"]["helper.sh"] == "explicit"

    def test_non_relative_protected_path_rejected(self, fb, validator, repo,
                                                  tmp_path):
        _base_with_harness(repo)
        _git(repo, "checkout", "-b", "fix/ok")
        (repo / "fix.txt").write_text("ok\n")
        _git(repo, "add", "fix.txt")
        _git(repo, "commit", "-m", "ok")
        _git(repo, "checkout", "main")
        bid = fb.register_branch(str(repo), "fix/ok")
        with pytest.raises(ValueError, match="not worktree-relative"):
            validator.validate(
                bid, red_cmd="./red.sh", green_cmd="./green.sh",
                protected_paths=["/etc/passwd"],
                scratch_dir=str(tmp_path / "scratch"),
            )


# ── Widened harness trust (wave-11 B): transitive discovery, manifest
#    globs, expected-count pinning, cap ──────────────────────────────────


def _base_with_indirect_harness(repo: Path) -> None:
    """Base whose red leg is INDIRECT: red.sh invokes 'python3
    inner_test.py' — the harness definition lives partly in the inner test
    source. Also carries a pyproject.toml manifest."""
    _script_on_main(repo, "red.sh", "#!/bin/sh\npython3 inner_test.py\n")
    _script_on_main(repo, "green.sh", "#!/bin/sh\necho 'GREEN 5/5'\n")
    _script_on_main(repo, "guard.sh", "#!/bin/sh\necho 'GUARD 3/3'\n")
    (repo / "inner_test.py").write_text(
        "print('RED 2/2')  # honest inner test\n"
    )
    _git(repo, "add", "inner_test.py")
    (repo / "pyproject.toml").write_text("[project]\nname = 'fixture'\n")
    _git(repo, "add", "pyproject.toml")
    _git(repo, "commit", "-m", "harness: indirect scripts + manifest")


def _branch_touching(repo: Path, name: str, fname: str, content: str) -> None:
    _git(repo, "checkout", "-b", name)
    (repo / fname).write_text(content)
    _git(repo, "add", fname)
    _git(repo, "commit", "-m", f"{name}: edit {fname}")
    _git(repo, "checkout", "main")


class TestWidenedHarnessTrust:
    def test_tampered_inner_test_source_refused(self, fb, validator, repo,
                                                tmp_path):
        """The wave-10 hole: red.sh invokes 'python3 inner_test.py'; the
        branch edits inner_test.py to always-pass (with a sentinel). The
        transitive scan protects inner_test.py, so the branch is REFUSED
        pre-execution and the doctored inner test never runs."""
        _base_with_indirect_harness(repo)
        marker = tmp_path / "tampered-inner-ran.marker"
        _branch_touching(
            repo, "fix/inner-evil", "inner_test.py",
            f"import pathlib\n"
            f"pathlib.Path({str(marker)!r}).touch()\n"
            f"print('RED 2/2')  # forged\n",
        )
        bid = fb.register_branch(str(repo), "fix/inner-evil")
        res = validator.validate(
            bid, red_cmd="./red.sh", green_cmd="./green.sh",
            guard_cmd="./guard.sh", scratch_dir=str(tmp_path / "scratch"),
        )
        assert res["passed"] is False
        assert res["refused"] == "harness-tampered"
        assert res["tampered_paths"] == ["inner_test.py"]
        assert res["protected_source"]["inner_test.py"] == "transitive"
        assert not marker.exists()  # never executed
        assert fb.get_branch(bid)["status"] == "failed"
        from bin import catalog

        log = catalog.cas_read(res["log_digest"])
        assert log.splitlines()[0] == "HARNESS-TAMPERED: inner_test.py"

    def test_tampered_manifest_refused_via_glob(self, fb, validator, repo,
                                                tmp_path):
        """A branch editing pyproject.toml (the Cargo.toml-analog manifest
        that resolves what the harness runs) is refused via the manifest
        glob even though no command or script names it."""
        _base_with_indirect_harness(repo)
        _branch_touching(
            repo, "fix/manifest-evil", "pyproject.toml",
            "[project]\nname = 'fixture'\n[tool.evil]\nredirect = true\n",
        )
        bid = fb.register_branch(str(repo), "fix/manifest-evil")
        res = validator.validate(
            bid, red_cmd="./red.sh", green_cmd="./green.sh",
            guard_cmd="./guard.sh", scratch_dir=str(tmp_path / "scratch"),
        )
        assert res["refused"] == "harness-tampered"
        assert res["tampered_paths"] == ["pyproject.toml"]
        assert res["protected_source"]["pyproject.toml"] == "manifest-glob"

    def test_cargo_style_test_target_protected(self, fb, validator, repo,
                                                tmp_path):
        """A script mentioning '--test harness_red' protects every
        tests/harness_red.rs match in the BASE tree (top-level and
        nested), enumerated via git ls-tree — never the working tree."""
        _script_on_main(repo, "red.sh",
                        "#!/bin/sh\n# cargo test --test harness_red\n"
                        "echo 'RED 2/2'\n")
        _script_on_main(repo, "green.sh", "#!/bin/sh\necho 'GREEN 5/5'\n")
        (repo / "tests").mkdir()
        (repo / "tests" / "harness_red.rs").write_text("// red test\n")
        nested = repo / "crates" / "hashing" / "tests"
        nested.mkdir(parents=True)
        (nested / "harness_red.rs").write_text("// nested red test\n")
        _git(repo, "add", "tests", "crates")
        _git(repo, "commit", "-m", "harness: cargo-style test targets")
        _branch_touching(
            repo, "fix/rs-evil", "crates/hashing/tests/harness_red.rs",
            "// always pass\n",
        )
        bid = fb.register_branch(str(repo), "fix/rs-evil")
        res = validator.validate(
            bid, red_cmd="./red.sh", green_cmd="./green.sh",
            scratch_dir=str(tmp_path / "scratch"),
        )
        assert res["refused"] == "harness-tampered"
        assert res["tampered_paths"] == ["crates/hashing/tests/harness_red.rs"]
        src = res["protected_source"]
        assert src["tests/harness_red.rs"] == "manifest-glob"
        assert src["crates/hashing/tests/harness_red.rs"] == "manifest-glob"

    def test_honest_branch_widened_set_and_digest(self, fb, validator, repo,
                                                  tmp_path):
        """Honest branch still passes; protected_source shows WHY each path
        is protected; the digest covers the WIDENED set (provably different
        from a digest computed the old script-only way)."""
        _base_with_indirect_harness(repo)
        _git(repo, "checkout", "-b", "fix/honest-wide")
        (repo / "fix.txt").write_text("honest fix\n")
        _git(repo, "add", "fix.txt")
        _git(repo, "commit", "-m", "honest fix")
        _git(repo, "checkout", "main")
        bid = fb.register_branch(str(repo), "fix/honest-wide")
        res = validator.validate(
            bid, red_cmd="./red.sh", green_cmd="./green.sh",
            guard_cmd="./guard.sh", scratch_dir=str(tmp_path / "scratch"),
        )
        assert res["passed"] is True
        assert res["red"] == (2, 2)  # inner test actually ran
        assert res["protected_source"] == {
            "red.sh": "script",
            "green.sh": "script",
            "guard.sh": "script",
            "inner_test.py": "transitive",
            "pyproject.toml": "manifest-glob",
        }
        assert res["protected_paths"] == sorted(res["protected_source"])
        # digest covers the widened set, not just the scripts
        base_sha = fb.get_branch(bid)["base_sha"]
        old_way = validator._harness_digest(
            str(repo), base_sha, ["red.sh", "green.sh", "guard.sh"]
        )
        assert res["harness_digest"] != old_way
        assert res["harness_digest"] == validator._harness_digest(
            str(repo), base_sha, res["protected_paths"]
        )

    def test_expected_count_mismatch_fails_leg(self, fb, validator, repo,
                                               tmp_path):
        """Pinned counters that don't match the parsed output fail the leg
        with COUNTER-MISMATCH in the log, even though the leg reports full
        counts (2/2 looks 100%-passing but isn't what was pinned)."""
        _base_with_harness(repo)
        _git(repo, "checkout", "-b", "fix/pinned")
        (repo / "fix.txt").write_text("fix\n")
        _git(repo, "add", "fix.txt")
        _git(repo, "commit", "-m", "fix")
        _git(repo, "checkout", "main")
        bid = fb.register_branch(str(repo), "fix/pinned")
        res = validator.validate(
            bid, red_cmd="./red.sh", green_cmd="./green.sh",
            guard_cmd="./guard.sh",
            expected={"red": (3, 3)},  # base harness prints RED 2/2
            scratch_dir=str(tmp_path / "scratch"),
        )
        assert res["passed"] is False
        assert res["red"] == (2, 2)
        assert fb.get_branch(bid)["status"] == "failed"
        from bin import catalog

        log = catalog.cas_read(res["log_digest"])
        assert "COUNTER-MISMATCH: RED expected 3/3, got 2/2" in log

    def test_expected_counts_matching_passes(self, fb, validator, repo,
                                             tmp_path):
        _base_with_harness(repo)
        _git(repo, "checkout", "-b", "fix/pinned-ok")
        (repo / "fix.txt").write_text("fix\n")
        _git(repo, "add", "fix.txt")
        _git(repo, "commit", "-m", "fix")
        _git(repo, "checkout", "main")
        bid = fb.register_branch(str(repo), "fix/pinned-ok")
        res = validator.validate(
            bid, red_cmd="./red.sh", green_cmd="./green.sh",
            guard_cmd="./guard.sh",
            expected={"red": (2, 2), "green": (5, 5), "guard": (3, 3)},
            scratch_dir=str(tmp_path / "scratch"),
        )
        assert res["passed"] is True
        from bin import catalog

        log = catalog.cas_read(res["log_digest"])
        assert "COUNTER-MISMATCH" not in log

    def test_protected_cap_refuses_honestly(self, fb, validator, repo,
                                            tmp_path):
        """A widened set over MAX_PROTECTED_PATHS refuses to validate with
        an honest error — never a silent truncation. No row is written."""
        _base_with_harness(repo)
        _git(repo, "checkout", "-b", "fix/cap")
        (repo / "fix.txt").write_text("fix\n")
        _git(repo, "add", "fix.txt")
        _git(repo, "commit", "-m", "fix")
        _git(repo, "checkout", "main")
        bid = fb.register_branch(str(repo), "fix/cap")
        too_many = [f"gen/file{i}.txt" for i in range(
            validator.MAX_PROTECTED_PATHS + 1)]
        with pytest.raises(ValueError, match="exceeding the cap"):
            validator.validate(
                bid, red_cmd="./red.sh", green_cmd="./green.sh",
                protected_paths=too_many,
                scratch_dir=str(tmp_path / "scratch"),
            )
        assert fb.current_validation(bid) is None
        assert _no_worktrees_left(repo)

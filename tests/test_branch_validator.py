"""Tests for bin/branch_validator.py — isolated per-branch validation
(wave-9 B2). End-to-end against real temp git repos whose red/green/guard
commands are tiny shell scripts printing the parseable conventions
('RED a/b', 'GREEN a/b', 'GUARD a/b').
"""
from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

import pytest

from tests.test_fix_branches import _commit, _git, _make_repo

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
        from bin import catalog

        assert cv["log_digest"] == res["log_digest"]
        log = catalog.cas_read(cv["log_digest"])
        assert "RED 2/2" in log and "GREEN 5/5" in log and "GUARD 3/3" in log
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
            red_cmd="true",
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
                fb.refresh_head(bid)
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
        monkeypatch.setattr(validator, "_tampered_paths",
                            lambda *a, **k: [])
        res = validator.validate(
            bid, red_cmd="./red.sh", green_cmd="./green.sh",
            guard_cmd="./guard.sh", scratch_dir=str(tmp_path / "scratch"),
        )
        assert res["passed"] is False
        assert res["guard"] == (2, 3)
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
        assert not marker.exists()
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
        assert res["red"] == (2, 2)
        assert res["protected_source"] == {
            "red.sh": "script",
            "green.sh": "script",
            "guard.sh": "script",
            "inner_test.py": "transitive",
            "pyproject.toml": "manifest-glob",
        }
        assert res["protected_paths"] == sorted(res["protected_source"])
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
            expected={"red": (3, 3)},
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

def _seed_ref(fb, ref: str) -> None:
    with fb._connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO domain(name, generator_prompt, "
            "judge_prompt, corpus_source) VALUES (?, '', '', '{}')",
            (ref,),
        )
        conn.commit()

PAIR_A = "a" * 64
PAIR_B = "b" * 64
PAIR_C = "c" * 64
PAIR_D = "d" * 64
UNJUDGED_PAIR = "e" * 64

def _seed_pairs(fb, *keys: str) -> None:
    """Record ``keys`` where judged pairs are recorded.

    The validator refuses a pair key that names no judged pair, so a
    standing built out of thin air has to say where its keys came from. The
    dispatch ledger is one of the three places that counts: it is written at
    claim time from the pool's live results.
    """
    from bin import dispatch

    dispatch.init()
    with fb._connect() as conn:
        cur = conn.execute(
            "INSERT INTO work_dispatch(domain_id, dispatch_key, item_id, "
            "pool_id, destination, outcome) "
            "VALUES (0, ?, 'seeded-item', 'seeded-pool', 'branch-author', "
            "'claimed')",
            (uuid.uuid4().hex,),
        )
        conn.executemany(
            "INSERT OR IGNORE INTO work_dispatch_pair(dispatch_id, pair_key) "
            "VALUES (?, ?)",
            [(cur.lastrowid, key) for key in keys],
        )
        conn.commit()

def _standing(**overrides):
    from bin.workorder import TournamentStanding

    base = dict(
        points=7,
        played=3,
        rank=1,
        rounds=4,
        pool_id="wave-13",
        pair_keys=[PAIR_A, PAIR_B, PAIR_C],
    )
    base.update(overrides)
    return TournamentStanding(**base)

def _tournament_branch(fb, repo, name, ref, *, guard_line="GUARD 3/3"):
    _fixture_branch(repo, name, guard_line=guard_line)
    _seed_ref(fb, ref)
    _seed_pairs(fb, PAIR_A, PAIR_B, PAIR_C, PAIR_D)
    return fb.register_branch(str(repo), name, workorder_ref=ref)

class TestRankingEvidence:
    def test_ranking_evidence_records_outcome_against_pairs_and_standing(
        self, fb, validator, repo, tmp_path
    ):
        bid = _tournament_branch(fb, repo, "fix/won-and-compiles", "wo-top")
        res = validator.validate(
            bid,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            guard_cmd="./guard.sh",
            scratch_dir=str(tmp_path / "scratch"),
            standing=_standing(),
        )
        assert res["passed"] is True
        assert res["ranking_evidence_id"] is not None

        for pair_key in (PAIR_A, PAIR_B, PAIR_C):
            joined = validator.evidence_for_pair(pair_key)
            assert [row["id"] for row in joined] == [res["ranking_evidence_id"]]
            assert joined[0]["outcome"] == "passed"
            assert joined[0]["workorder_ref"] == "wo-top"
            assert joined[0]["validation_id"] == res["validation_id"]
            assert joined[0]["tested_sha"] == res["tested_sha"]

    def test_ranking_evidence_query_returns_pool_triples(
        self, fb, validator, repo, tmp_path
    ):
        winner = _tournament_branch(fb, repo, "fix/winner", "wo-winner")
        loser = _tournament_branch(
            fb, repo, "fix/loser", "wo-loser", guard_line="GUARD 1/3"
        )
        validator.validate(
            winner,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            guard_cmd="./guard.sh",
            scratch_dir=str(tmp_path / "scratch"),
            standing=_standing(rank=1, points=9, played=3),
        )
        validator.validate(
            loser,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            guard_cmd="./guard.sh",
            scratch_dir=str(tmp_path / "scratch"),
            standing=_standing(
                rank=2, points=6, played=3, pair_keys=[PAIR_A, PAIR_D]
            ),
        )

        triples = validator.ranking_evidence_for_pool("wave-13")
        assert [
            (t["workorder_ref"], t["standing"]["rank"], t["outcome"])
            for t in triples
        ] == [("wo-winner", 1, "passed"), ("wo-loser", 2, "failed")]
        assert triples[0]["standing"]["points"] == 9
        assert triples[0]["standing"]["pair_keys"] == sorted(
            [PAIR_A, PAIR_B, PAIR_C]
        )
        assert triples[1]["standing"]["pair_keys"] == sorted([PAIR_A, PAIR_D])
        assert validator.ranking_evidence_for_pool("other-pool") == []

        shared = validator.evidence_for_pair(PAIR_A)
        assert [row["workorder_ref"] for row in shared] == [
            "wo-winner", "wo-loser"
        ]

    def test_ranking_evidence_records_a_harness_tampered_refusal(
        self, fb, validator, repo, tmp_path
    ):
        _base_with_harness(repo)
        _git(repo, "checkout", "-b", "fix/tamper")
        (repo / "guard.sh").write_text("#!/bin/sh\necho 'GUARD 9/9'\n")
        (repo / "fix.txt").write_text("fix\n")
        _git(repo, "add", "guard.sh", "fix.txt")
        _git(repo, "commit", "-m", "fix + tamper")
        _git(repo, "checkout", "main")
        _seed_ref(fb, "wo-tamper")
        _seed_pairs(fb, PAIR_A, PAIR_B, PAIR_C)
        bid = fb.register_branch(str(repo), "fix/tamper", workorder_ref="wo-tamper")

        res = validator.validate(
            bid,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            guard_cmd="./guard.sh",
            scratch_dir=str(tmp_path / "scratch"),
            standing=_standing(pool_id="wave-14"),
        )
        assert res["refused"] == "harness-tampered"
        triples = validator.ranking_evidence_for_pool("wave-14")
        assert [t["outcome"] for t in triples] == ["refused"]

    def test_ranking_evidence_needs_a_joinable_tournament_item(
        self, fb, validator, repo, tmp_path
    ):
        _fixture_branch(repo, "fix/orphan", guard_line="GUARD 3/3")
        _seed_pairs(fb, PAIR_A, PAIR_B, PAIR_C)
        bid = fb.register_branch(str(repo), "fix/orphan")
        with pytest.raises(ValueError, match="no workorder_ref"):
            validator.validate(
                bid,
                red_cmd="./red.sh",
                green_cmd="./green.sh",
                scratch_dir=str(tmp_path / "scratch"),
                standing=_standing(),
            )
        assert fb.current_validation(bid) is None

        bid2 = _tournament_branch(fb, repo, "fix/no-pool", "wo-no-pool")
        with pytest.raises(ValueError, match="pool_id"):
            validator.validate(
                bid2,
                red_cmd="./red.sh",
                green_cmd="./green.sh",
                scratch_dir=str(tmp_path / "scratch"),
                standing=_standing(pool_id=""),
            )
        assert fb.current_validation(bid2) is None
        assert validator.ranking_evidence_for_pool("wave-13") == []

    def test_ranking_evidence_is_optional_and_append_only(
        self, fb, validator, repo, tmp_path
    ):
        bid = _tournament_branch(fb, repo, "fix/untracked", "wo-plain")
        res = validator.validate(
            bid,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            scratch_dir=str(tmp_path / "scratch"),
        )
        assert res["ranking_evidence_id"] is None
        assert validator.ranking_evidence_for_pool("wave-13") == []

        bid2 = _tournament_branch(fb, repo, "fix/tracked", "wo-tracked")
        validator.validate(
            bid2,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            scratch_dir=str(tmp_path / "scratch"),
            standing=_standing(),
        )
        with fb._connect() as conn:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute("UPDATE ranking_evidence SET outcome='failed'")
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute("DELETE FROM ranking_evidence")

    def test_ranking_evidence_promotes_nothing_on_its_own(
        self, fb, validator, repo, tmp_path
    ):
        bid = _tournament_branch(fb, repo, "fix/inert", "wo-inert")
        res = validator.validate(
            bid,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            guard_cmd="./guard.sh",
            scratch_dir=str(tmp_path / "scratch"),
            standing=_standing(),
        )
        assert res["passed"] is True
        with fb._connect() as conn:
            counts = {
                table: conn.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                for table in (
                    "review_rule",
                    "review_rule_proposal",
                    "approval_event",
                    "fix_branch_review",
                    "fix_branch_ship",
                )
            }
        assert counts == {
            "review_rule": 0,
            "review_rule_proposal": 0,
            "approval_event": 0,
            "fix_branch_review": 0,
            "fix_branch_ship": 0,
        }
        assert fb.get_branch(bid)["status"] == "validated"

class TestUnavailableReturnEdgeDegrades:
    """A byed item holds no pair key — a bye is not a result and awards
    none — so the join back to the judgements does not exist. That must
    cost one evidence row's join, never the whole validation."""

    def test_byed_item_with_no_pair_keys_is_still_validated(
        self, fb, validator, repo, tmp_path
    ):
        bid = _tournament_branch(fb, repo, "fix/byed", "wo-byed")
        res = validator.validate(
            bid,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            guard_cmd="./guard.sh",
            scratch_dir=str(tmp_path / "scratch"),
            standing=_standing(points=1, played=1, rank=4, pair_keys=[]),
        )
        assert res["passed"] is True
        assert res["red"] == (2, 2)
        assert res["green"] == (5, 5)
        assert res["guard"] == (3, 3)
        assert fb.get_branch(bid)["status"] == "validated"
        assert fb.current_validation(bid)["passed"] == 1
        assert _no_worktrees_left(repo)

    def test_byed_outcome_records_why_it_could_not_be_joined(
        self, fb, validator, repo, tmp_path
    ):
        bid = _tournament_branch(fb, repo, "fix/byed-detail", "wo-byed-detail")
        res = validator.validate(
            bid,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            scratch_dir=str(tmp_path / "scratch"),
            standing=_standing(points=1, played=1, rank=4, pair_keys=[]),
        )
        assert res["ranking_evidence_id"] is not None
        assert res["ranking_evidence_join"] == "no-pair-keys"

        with fb._connect() as conn:
            row = conn.execute(
                "SELECT outcome, join_status, join_detail, played, rank "
                "FROM ranking_evidence WHERE id=?",
                (res["ranking_evidence_id"],),
            ).fetchone()
            pair_rows = conn.execute(
                "SELECT COUNT(*) FROM ranking_evidence_pair WHERE evidence_id=?",
                (res["ranking_evidence_id"],),
            ).fetchone()[0]
        assert row["outcome"] == "passed"
        assert row["join_status"] == "no-pair-keys"
        assert row["played"] == 1 and row["rank"] == 4
        assert "bye" in row["join_detail"]
        assert pair_rows == 0

    def test_unplayed_item_says_it_was_never_compared(
        self, fb, validator, repo, tmp_path
    ):
        bid = _tournament_branch(fb, repo, "fix/unranked", "wo-unranked")
        res = validator.validate(
            bid,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            scratch_dir=str(tmp_path / "scratch"),
            standing=_standing(points=0, played=0, rank=0, pair_keys=[]),
        )
        assert res["passed"] is True
        record = validator.ranking_evidence(res["ranking_evidence_id"])
        assert record["join_status"] == "no-pair-keys"
        assert record["joinable"] is False
        assert record["pair_keys"] == []
        assert "played no match" in record["join_detail"]

    def test_degraded_and_joined_rows_are_distinguishable_in_one_pool(
        self, fb, validator, repo, tmp_path
    ):
        joined = _tournament_branch(fb, repo, "fix/joined", "wo-joined")
        byed = _tournament_branch(fb, repo, "fix/bye", "wo-bye")
        validator.validate(
            joined,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            scratch_dir=str(tmp_path / "scratch"),
            standing=_standing(rank=1, points=7, played=3),
        )
        validator.validate(
            byed,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            scratch_dir=str(tmp_path / "scratch"),
            standing=_standing(rank=2, points=1, played=1, pair_keys=[]),
        )

        triples = validator.ranking_evidence_for_pool("wave-13")
        assert [
            (t["workorder_ref"], t["join_status"], t["pair_keys"])
            for t in triples
        ] == [
            ("wo-joined", "joined", sorted([PAIR_A, PAIR_B, PAIR_C])),
            ("wo-bye", "no-pair-keys", []),
        ]
        assert [t["joinable"] for t in triples] == [True, False]
        assert [row["workorder_ref"] for row in validator.evidence_for_pair(
            PAIR_A
        )] == ["wo-joined"]

    def test_a_pair_key_naming_no_judged_pair_is_still_refused(
        self, fb, validator, repo, tmp_path
    ):
        """The case the guard was written for: evidence CLAIMED against a
        pair identity that cannot be a sha256 pair key is wrong, not
        merely absent, and must abort before any candidate code runs."""
        bid = _tournament_branch(fb, repo, "fix/fake-pair", "wo-fake-pair")
        for fake in ("z" * 64, "A" * 64, PAIR_A[:63], PAIR_A + "0", ""):
            with pytest.raises(ValueError, match="sha256 pair identity"):
                validator.validate(
                    bid,
                    red_cmd="./red.sh",
                    green_cmd="./green.sh",
                    scratch_dir=str(tmp_path / "scratch"),
                    standing=_standing(pair_keys=[PAIR_A, fake], played=3),
                )
        assert fb.current_validation(bid) is None
        assert validator.ranking_evidence_for_pool("wave-13") == []
        assert _no_worktrees_left(repo)

    def test_record_ranking_evidence_refuses_a_fake_pair_key_directly(
        self, fb, validator, repo, tmp_path
    ):
        bid = _tournament_branch(fb, repo, "fix/direct", "wo-direct")
        with pytest.raises(ValueError, match="sha256 pair identity"):
            validator.record_ranking_evidence(
                bid,
                validation_id=1,
                tested_sha="0" * 40,
                outcome="failed",
                standing=_standing(pair_keys=["not-a-pair-key"], played=3),
                workorder_ref="wo-direct",
            )
        assert validator.ranking_evidence_for_pool("wave-13") == []

    def test_join_status_is_derived_from_the_standing_not_asserted(
        self, fb, validator, repo, tmp_path
    ):
        bid = _tournament_branch(fb, repo, "fix/derived", "wo-derived")
        res = validator.validate(
            bid,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            scratch_dir=str(tmp_path / "scratch"),
            standing=_standing(),
        )
        with_keys = res["ranking_evidence_id"]
        without = validator.record_ranking_evidence(
            bid,
            validation_id=res["validation_id"],
            tested_sha=res["tested_sha"],
            outcome="passed",
            standing=_standing(points=1, played=1, rank=2, pair_keys=[]),
            workorder_ref="wo-derived",
        )
        assert validator.ranking_evidence(with_keys)["join_status"] == "joined"
        assert validator.ranking_evidence(without)["join_status"] == (
            "no-pair-keys"
        )

class TestBeatOutcomeIsUsableEvidence:
    def test_validate_hands_back_the_whole_labelled_example(
        self, fb, validator, repo, tmp_path
    ):
        """bin/optimize.py's consumer needs the pair keys to join on, the
        verdict, and the provenance — without a second query."""
        bid = _tournament_branch(
            fb, repo, "fix/overvalued", "wo-overvalued",
            guard_line="GUARD 1/3",
        )
        res = validator.validate(
            bid,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            guard_cmd="./guard.sh",
            scratch_dir=str(tmp_path / "scratch"),
            standing=_standing(),
        )
        assert res["passed"] is False
        example = res["ranking_evidence"]
        assert example["outcome"] == "failed"
        assert example["pair_keys"] == sorted([PAIR_A, PAIR_B, PAIR_C])
        assert example["join_status"] == "joined"
        assert example["joinable"] is True
        assert example["pool_id"] == "wave-13"
        assert example["workorder_ref"] == "wo-overvalued"
        assert example["standing"] == {
            "points": 7,
            "played": 3,
            "rank": 1,
            "rounds": 4,
            "pool_id": "wave-13",
            "pair_keys": sorted([PAIR_A, PAIR_B, PAIR_C]),
        }
        assert example["evidence_id"] == res["ranking_evidence_id"]
        assert example["validation_id"] == res["validation_id"]
        assert example["fix_branch_id"] == bid
        assert example["tested_sha"] == res["tested_sha"]
        assert example["created_at"]

    def test_no_standing_means_no_example(
        self, fb, validator, repo, tmp_path
    ):
        bid = _tournament_branch(fb, repo, "fix/no-standing", "wo-no-standing")
        res = validator.validate(
            bid,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            scratch_dir=str(tmp_path / "scratch"),
        )
        assert res["ranking_evidence"] is None
        assert res["ranking_evidence_join"] is None

    def test_ranking_evidence_lookup_misses_return_none(self, fb, validator):
        assert validator.ranking_evidence(4242) is None

    def test_db_predating_join_status_is_migrated_in_place(
        self, fb, validator, repo, tmp_path
    ):
        """A row written before join_status existed could only have had
        pair keys — an empty set aborted the whole validation then — so it
        backfills to 'joined'."""
        with fb._connect() as conn:
            conn.executescript(
                "CREATE TABLE ranking_evidence ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  pool_id TEXT NOT NULL, workorder_ref TEXT NOT NULL,"
                "  fix_branch_id INTEGER NOT NULL, validation_id INTEGER NOT NULL,"
                "  tested_sha TEXT NOT NULL, outcome TEXT NOT NULL,"
                "  points INTEGER NOT NULL, played INTEGER NOT NULL,"
                "  rank INTEGER NOT NULL, rounds INTEGER NOT NULL,"
                "  created_at TEXT NOT NULL DEFAULT (datetime('now')));"
                "CREATE TABLE ranking_evidence_pair ("
                "  evidence_id INTEGER NOT NULL, pair_key TEXT NOT NULL,"
                "  PRIMARY KEY (evidence_id, pair_key));"
            )
            conn.execute(
                "INSERT INTO ranking_evidence(id, pool_id, workorder_ref, "
                "fix_branch_id, validation_id, tested_sha, outcome, points, "
                "played, rank, rounds) VALUES "
                "(1,'wave-old','wo-old',1,1,'deadbeef','passed',9,3,1,4)"
            )
            conn.execute(
                "INSERT INTO ranking_evidence_pair(evidence_id, pair_key) "
                "VALUES (1, ?)",
                (PAIR_A,),
            )
            conn.commit()

        bid = _tournament_branch(fb, repo, "fix/after-migration", "wo-new")
        res = validator.validate(
            bid,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            scratch_dir=str(tmp_path / "scratch"),
            standing=_standing(
                pool_id="wave-old", points=1, played=1, rank=2, pair_keys=[]
            ),
        )
        assert res["passed"] is True

        old, new = validator.ranking_evidence_for_pool("wave-old")
        assert (old["workorder_ref"], old["join_status"], old["pair_keys"]) == (
            "wo-old", "joined", [PAIR_A]
        )
        assert (new["workorder_ref"], new["join_status"], new["pair_keys"]) == (
            "wo-new", "no-pair-keys", []
        )
        with fb._connect() as conn:
            columns = {
                r[1] for r in conn.execute("PRAGMA table_info(ranking_evidence)")
            }
        assert {"join_status", "join_detail"} <= columns

class TestAPairKeyMustBeWholeAndReal:
    def test_a_trailing_newline_is_not_a_pair_key(
        self, fb, validator, repo, tmp_path
    ):
        """'<64 hex>\\n' passed a '$'-anchored re.match and landed forever in
        a table whose UPDATE and DELETE triggers RAISE(ABORT)."""
        bid = _tournament_branch(fb, repo, "fix/newline", "wo-newline")
        with pytest.raises(ValueError, match="sha256 pair identity"):
            validator.validate(
                bid,
                red_cmd="./red.sh",
                green_cmd="./green.sh",
                scratch_dir=str(tmp_path / "scratch"),
                standing=_standing(pair_keys=[PAIR_A, PAIR_B + "\n"]),
            )
        assert fb.current_validation(bid) is None
        assert validator.ranking_evidence_for_pool("wave-13") == []

    def test_a_well_formed_key_that_names_no_judged_pair_is_refused(
        self, fb, validator, repo, tmp_path
    ):
        bid = _tournament_branch(fb, repo, "fix/unjudged", "wo-unjudged")
        with pytest.raises(ValueError, match="names no judged pair"):
            validator.validate(
                bid,
                red_cmd="./red.sh",
                green_cmd="./green.sh",
                scratch_dir=str(tmp_path / "scratch"),
                standing=_standing(pair_keys=[PAIR_A, UNJUDGED_PAIR]),
            )
        assert fb.current_validation(bid) is None
        assert validator.ranking_evidence_for_pool("wave-13") == [], (
            "shape is not existence: 64 hex characters are cheap, and "
            "'joined' evidence against a comparison nobody made is a lie "
            "bin/optimize.py would read as a labelled example"
        )
        assert _no_worktrees_left(repo)

    def test_record_ranking_evidence_refuses_an_unjudged_key_directly(
        self, fb, validator, repo
    ):
        bid = _tournament_branch(fb, repo, "fix/direct-unjudged", "wo-du")
        with pytest.raises(ValueError, match="names no judged pair"):
            validator.record_ranking_evidence(
                bid,
                validation_id=1,
                tested_sha="0" * 40,
                outcome="failed",
                standing=_standing(pair_keys=[UNJUDGED_PAIR], played=3),
                workorder_ref="wo-du",
            )
        assert validator.ranking_evidence_for_pool("wave-13") == []

    def test_a_key_recorded_on_a_score_row_counts_as_judged(
        self, fb, validator, repo, tmp_path
    ):
        """The dispatch ledger is not the only source: the judgement's own
        rows name the pair too."""
        bid = _tournament_branch(fb, repo, "fix/score-key", "wo-score-key")
        with fb._connect() as conn:
            conn.execute(
                "INSERT INTO eval_template(name, version, output_definition) "
                "VALUES ('probe-rubric', 1, '{}')"
            )
            template_id = conn.execute(
                "SELECT id FROM eval_template WHERE name='probe-rubric'"
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO score(rating_id, template_id, rubric_version, "
                "name, data_type, value, tournament_db_path, match_id, "
                "pair_key) VALUES ('r-1', ?, 1, 'judgement.verdict', "
                "'CATEGORICAL', 'a-wins', 'probe', 0, ?)",
                (template_id, UNJUDGED_PAIR),
            )
            conn.commit()
        res = validator.validate(
            bid,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            scratch_dir=str(tmp_path / "scratch"),
            standing=_standing(pair_keys=[UNJUDGED_PAIR], played=3),
        )
        assert res["ranking_evidence_join"] == "joined"
        assert res["ranking_evidence"]["pair_keys"] == [UNJUDGED_PAIR]

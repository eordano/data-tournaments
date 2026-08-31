"""Tests for bin/branch_author.py — WorkOrder -> branch authoring bridge
(wave-10 V1). Real temp git repos are built in-test (git init, commits);
no network. Backends under test: fixture (deterministic content) and
command (stub shell scripts).
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
from pathlib import Path

import pytest

def _env() -> dict:
    env = dict(os.environ)
    env.update(
        GIT_CONFIG_GLOBAL="/dev/null",
        GIT_CONFIG_SYSTEM="/dev/null",
        GIT_AUTHOR_NAME="test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="test",
        GIT_COMMITTER_EMAIL="test@example.invalid",
    )
    return env

def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_env(),
        check=True,
    )
    return proc.stdout.strip()

def _commit(repo: Path, fname: str, content: str, msg: str) -> str:
    (repo / fname).write_text(content)
    _git(repo, "add", fname)
    _git(repo, "commit", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")

def _make_repo(root: Path) -> Path:
    """The fixture-repo shape: main carries retry.py with the bug."""
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        capture_output=True,
        env=_env(),
        check=True,
    )
    _commit(repo, "retry.py", "BUGGY = True\n", "retry runner with the bug")
    return repo

def _branch_names(repo: Path) -> list[str]:
    out = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    return sorted(b for b in out.splitlines() if b)

def _stub_script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path

@pytest.fixture
def ba(tmp_data_home):
    from bin import branch_author as mod

    mod.init()
    return mod

@pytest.fixture
def fb(tmp_data_home):
    from bin import fix_branches as mod

    return mod

@pytest.fixture
def repo(tmp_path) -> Path:
    return _make_repo(tmp_path)

@pytest.fixture
def raw(tmp_data_home):
    conn = sqlite3.connect(str(tmp_data_home / "judgements.db"))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    yield conn
    conn.close()

FIX_A = {
    "files": {"retry.py": "BUGGY = False  # fresh per-attempt deadline\n"},
    "label": "fix-a-deadline-reset",
}
FIX_B = {
    "files": {"retry.py": "BUGGY = 'partially'  # carried budget pool\n"},
    "label": "fix-b-token-clone",
}

def _seed_ref(mod, ref: str) -> None:
    """Make ``ref`` resolvable as a domain name (strict lineage, wave-11
    W2): author_branch fail-closes on dangling workorder_refs."""
    with mod._connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO domain(name, generator_prompt, "
            "judge_prompt, corpus_source) VALUES (?, '', '', '{}')",
            (ref,),
        )
        conn.commit()

class TestFixtureBackend:
    def test_happy_path(self, ba, fb, repo, raw):
        base_sha = _git(repo, "rev-parse", "main")
        _seed_ref(ba, "wo-42")
        res = ba.author_branch(
            str(repo),
            base_ref="main",
            branch_name="fix/deadline-reset",
            backend="fixture",
            backend_config=FIX_A,
            workorder_ref="wo-42",
        )
        assert res["base_sha"] == base_sha
        assert res["backend"] == "fixture"
        head = _git(repo, "rev-parse", "fix/deadline-reset")
        assert res["head_sha"] == head
        assert _git(repo, "rev-parse", f"{head}^") == base_sha
        shown = _git(repo, "show", f"{head}:retry.py")
        assert "BUGGY = False" in shown
        msg = _git(repo, "log", "-1", "--format=%s", head)
        assert msg == "authored by fixture for wo-42"

        b = fb.get_branch(res["fix_branch_id"])
        assert b["branch_name"] == "fix/deadline-reset"
        assert b["base_sha"] == base_sha
        assert b["head_sha"] == head
        assert b["workorder_ref"] == "wo-42"
        assert b["patch_digest"] == res["patch_digest"]

        row = raw.execute(
            "SELECT * FROM branch_authoring WHERE id=?", (res["authoring_id"],)
        ).fetchone()
        assert row["fix_branch_id"] == res["fix_branch_id"]
        assert row["backend"] == "fixture"
        assert row["workorder_ref"] == "wo-42"
        assert row["base_sha"] == base_sha
        assert row["head_sha"] == head
        assert row["patch_digest"] == res["patch_digest"]

        prov = json.loads(row["provenance"])
        assert prov == {
            "backend": "fixture",
            "label": "fix-a-deadline-reset",
            "files": ["retry.py"],
        }
        assert ba.get_authoring(res["fix_branch_id"])[0]["provenance"] == prov

        assert _git(repo, "worktree", "list").count("\n") == 0

    def test_refuses_existing_branch(self, ba, repo, raw):
        _git(repo, "branch", "fix/taken")
        with pytest.raises(ba.AuthoringError, match="already exists"):
            ba.author_branch(
                str(repo),
                base_ref="main",
                branch_name="fix/taken",
                backend="fixture",
                backend_config=FIX_A,
            )
        assert raw.execute("SELECT COUNT(*) FROM branch_authoring").fetchone()[0] == 0

    def test_the_ref_is_taken_atomically_not_after_a_probe(
        self, ba, repo, raw, monkeypatch
    ):
        """The _branch_exists probe is a readable message, never the guard:
        with it blinded — the state a concurrent dispatcher leaves behind —
        the branch creation itself must still refuse."""
        _git(repo, "branch", "fix/raced")
        taken = _git(repo, "rev-parse", "fix/raced")
        monkeypatch.setattr(ba, "_branch_exists", lambda *a, **k: False)
        with pytest.raises(ba.AuthoringError, match="already exists"):
            ba.author_branch(
                str(repo),
                base_ref="main",
                branch_name="fix/raced",
                backend="fixture",
                backend_config=FIX_A,
            )
        assert _git(repo, "rev-parse", "fix/raced") == taken, (
            "the loser of the race never moved the winner's ref"
        )
        assert raw.execute("SELECT COUNT(*) FROM branch_authoring").fetchone()[0] == 0
        assert _git(repo, "worktree", "list").count("\n") == 0

    def test_unknown_backend_rejected(self, ba, repo):
        with pytest.raises(ba.AuthoringError, match="unknown backend"):
            ba.author_branch(
                str(repo),
                base_ref="main",
                branch_name="fix/x",
                backend="llm-magic",
                backend_config={},
            )

class TestAuthorCandidates:
    def test_two_candidates_same_base_different_heads(self, ba, fb, repo, raw):
        base_sha = _git(repo, "rev-parse", "main")
        _seed_ref(ba, "wo-7")
        results = ba.author_candidates(
            str(repo),
            base_ref="main",
            candidates=[
                {"branch_name": "fix/a", "backend": "fixture", "backend_config": FIX_A},
                {"branch_name": "fix/b", "backend": "fixture", "backend_config": FIX_B},
            ],
            workorder_ref="wo-7",
        )
        assert len(results) == 2
        assert {r["base_sha"] for r in results} == {base_sha}
        assert results[0]["head_sha"] != results[1]["head_sha"]
        assert results[0]["patch_digest"] != results[1]["patch_digest"]
        for r in results:
            assert _git(repo, "rev-parse", f"{r['head_sha']}^") == base_sha
        for r in results:
            b = fb.get_branch(r["fix_branch_id"])
            assert b["base_sha"] == base_sha
            assert b["workorder_ref"] == "wo-7"
            assert _git(repo, "rev-list", "--merges", "--all") == ""
        assert raw.execute("SELECT COUNT(*) FROM branch_authoring").fetchone()[0] == 2

    def test_empty_candidates_rejected(self, ba, repo):
        with pytest.raises(ba.AuthoringError, match="at least one"):
            ba.author_candidates(str(repo), base_ref="main", candidates=[])

class TestCommandBackend:
    def test_stub_script_writes_and_commits(self, ba, fb, repo, tmp_path, raw):
        script = _stub_script(
            tmp_path / "agent.sh",
            'printf "fixed by %s at %s on %s\\n" '
            '"$WORKORDER_REF" "$BASE_SHA" "$BRANCH_NAME" > agent-fix.txt\n',
        )
        base_sha = _git(repo, "rev-parse", "main")
        _seed_ref(ba, "wo-cmd")
        res = ba.author_branch(
            str(repo),
            base_ref="main",
            branch_name="fix/agent",
            backend="command",
            backend_config={"argv": [str(script)], "timeout_s": 30},
            workorder_ref="wo-cmd",
        )
        head = _git(repo, "rev-parse", "fix/agent")
        assert res["head_sha"] == head
        content = _git(repo, "show", f"{head}:agent-fix.txt")
        assert content == f"fixed by wo-cmd at {base_sha} on fix/agent"
        prov = json.loads(
            raw.execute(
                "SELECT provenance FROM branch_authoring WHERE id=?",
                (res["authoring_id"],),
            ).fetchone()[0]
        )
        assert prov["backend"] == "command"
        assert prov["argv"] == [str(script)]

    def test_nonzero_exit_is_honest_failure(self, ba, repo, tmp_path, raw):
        script = _stub_script(tmp_path / "boom.sh", "echo agent exploded >&2\nexit 3\n")
        _seed_ref(ba, "wo-boom")
        with pytest.raises(ba.AuthoringError, match="exited 3"):
            ba.author_branch(
                str(repo),
                base_ref="main",
                branch_name="fix/boom",
                backend="command",
                backend_config={"argv": [str(script)], "timeout_s": 30},
                workorder_ref="wo-boom",
            )
        assert raw.execute("SELECT COUNT(*) FROM fix_branch").fetchone()[0] == 0
        assert raw.execute("SELECT COUNT(*) FROM branch_authoring").fetchone()[0] == 0
        assert _git(repo, "worktree", "list").count("\n") == 0
        assert "fix/boom" not in _git(repo, "branch", "--list", "fix/boom")

    def test_empty_diff_is_honest_failure(self, ba, repo, tmp_path, raw):
        script = _stub_script(tmp_path / "noop.sh", "exit 0\n")
        with pytest.raises(ba.AuthoringError, match="EMPTY diff"):
            ba.author_branch(
                str(repo),
                base_ref="main",
                branch_name="fix/noop",
                backend="command",
                backend_config={"argv": [str(script)], "timeout_s": 30},
            )
        assert raw.execute("SELECT COUNT(*) FROM fix_branch").fetchone()[0] == 0
        assert _git(repo, "worktree", "list").count("\n") == 0
        assert "fix/noop" not in _git(repo, "branch", "--list", "fix/noop")

class TestLineage:
    """author_branch fail-closed workorder_ref resolution: a dangling ref
    is refused BEFORE any git mutation; the escape hatch stamps honestly."""

    def test_dangling_ref_refused_no_branch_created(self, ba, repo, raw):
        with pytest.raises(ValueError, match="workorder_ref does not resolve"):
            ba.author_branch(
                str(repo),
                base_ref="main",
                branch_name="fix/dangling",
                backend="fixture",
                backend_config=FIX_A,
                workorder_ref="wo-nowhere",
            )
        assert raw.execute("SELECT COUNT(*) FROM fix_branch").fetchone()[0] == 0
        assert raw.execute(
            "SELECT COUNT(*) FROM branch_authoring").fetchone()[0] == 0
        assert "fix/dangling" not in _git(repo, "branch", "--list",
                                          "fix/dangling")

    def test_dangling_ref_refused_on_candidates(self, ba, repo, raw):
        with pytest.raises(ValueError, match="workorder_ref does not resolve"):
            ba.author_candidates(
                str(repo),
                base_ref="main",
                candidates=[{"branch_name": "fix/c1", "backend": "fixture",
                             "backend_config": FIX_A}],
                workorder_ref="wo-nowhere",
            )
        assert raw.execute("SELECT COUNT(*) FROM fix_branch").fetchone()[0] == 0

    def test_resolving_ref_accepted(self, ba, fb, repo):
        _seed_ref(ba, "real-domain")
        res = ba.author_branch(
            str(repo),
            base_ref="main",
            branch_name="fix/resolved",
            backend="fixture",
            backend_config=FIX_A,
            workorder_ref="real-domain",
        )
        assert fb.get_branch(res["fix_branch_id"])["workorder_ref"] == \
            "real-domain"

    def test_escape_hatch_stamps_unresolved(self, ba, fb, repo, raw):
        res = ba.author_branch(
            str(repo),
            base_ref="main",
            branch_name="fix/exploratory",
            backend="fixture",
            backend_config=FIX_A,
            workorder_ref="wo-nowhere",
            allow_unresolved=True,
        )
        stamped = "unresolved-ref:wo-nowhere"
        assert fb.get_branch(res["fix_branch_id"])["workorder_ref"] == stamped
        row = raw.execute(
            "SELECT workorder_ref FROM branch_authoring WHERE id=?",
            (res["authoring_id"],),
        ).fetchone()
        assert row["workorder_ref"] == stamped
        msg = _git(repo, "log", "-1", "--format=%s", res["head_sha"])
        assert msg == f"authored by fixture for {stamped}"

    def test_cli_allow_unresolved_flag(self, ba, fb, repo, tmp_path, capsys):
        cfg = tmp_path / "explore.json"
        cfg.write_text(json.dumps({
            "base_ref": "main",
            "branch_name": "fix/cli-explore",
            "backend": "fixture",
            "backend_config": FIX_A,
            "workorder_ref": "wo-cli-dangling",
        }))
        assert ba.main(["author", "--repo", str(repo),
                        "--config", str(cfg)]) == 1
        assert "does not resolve" in capsys.readouterr().err
        assert ba.main(["author", "--repo", str(repo), "--config", str(cfg),
                        "--allow-unresolved"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert fb.get_branch(out["fix_branch_id"])["workorder_ref"] == \
            "unresolved-ref:wo-cli-dangling"

class TestSchema:
    def test_table_and_triggers_exist(self, ba, raw):
        tables = {
            r["name"]
            for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "branch_authoring" in tables
        triggers = {
            r["name"]
            for r in raw.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        assert "branch_authoring_immutable" in triggers
        assert "branch_authoring_no_delete" in triggers

    def test_rows_append_only(self, ba, repo, raw):
        res = ba.author_branch(
            str(repo),
            base_ref="main",
            branch_name="fix/immutable",
            backend="fixture",
            backend_config=FIX_A,
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            raw.execute(
                "UPDATE branch_authoring SET head_sha='deadbeef' WHERE id=?",
                (res["authoring_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute(
                "DELETE FROM branch_authoring WHERE id=?", (res["authoring_id"],)
            )

    def test_backend_check_constraint(self, ba, repo, raw):
        res = ba.author_branch(
            str(repo),
            base_ref="main",
            branch_name="fix/check",
            backend="fixture",
            backend_config=FIX_A,
        )
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "INSERT INTO branch_authoring(fix_branch_id, backend, base_sha, "
                "head_sha, patch_digest) VALUES (?, 'psychic', 'a', 'b', 'c')",
                (res["fix_branch_id"],),
            )

class TestCLI:
    def test_author_and_candidates(self, ba, repo, tmp_path, capsys):
        _seed_ref(ba, "wo-cli")
        _seed_ref(ba, "wo-cli-2")
        cfg = tmp_path / "single.json"
        cfg.write_text(json.dumps({
            "base_ref": "main",
            "branch_name": "fix/cli-one",
            "backend": "fixture",
            "backend_config": FIX_A,
            "workorder_ref": "wo-cli",
        }))
        assert ba.main(["author", "--repo", str(repo), "--config", str(cfg)]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["backend"] == "fixture"
        assert _git(repo, "rev-parse", "fix/cli-one") == out["head_sha"]

        cfg2 = tmp_path / "multi.json"
        cfg2.write_text(json.dumps({
            "base_ref": "main",
            "workorder_ref": "wo-cli-2",
            "candidates": [
                {"branch_name": "fix/cli-a", "backend": "fixture",
                 "backend_config": FIX_A},
                {"branch_name": "fix/cli-b", "backend": "fixture",
                 "backend_config": FIX_B},
            ],
        }))
        assert ba.main(
            ["author-candidates", "--repo", str(repo), "--config", str(cfg2)]
        ) == 0
        outs = json.loads(capsys.readouterr().out)
        assert len(outs) == 2
        assert outs[0]["base_sha"] == outs[1]["base_sha"]

    def test_author_error_exits_1(self, ba, repo, tmp_path, capsys):
        _git(repo, "branch", "fix/dupe")
        cfg = tmp_path / "dupe.json"
        cfg.write_text(json.dumps({
            "base_ref": "main",
            "branch_name": "fix/dupe",
            "backend": "fixture",
            "backend_config": FIX_A,
        }))
        assert ba.main(["author", "--repo", str(repo), "--config", str(cfg)]) == 1
        assert "already exists" in capsys.readouterr().err

class TestWorkTypeRouting:
    def test_investigation_work_type_is_refused_not_authored(self, ba, repo, raw):
        _seed_ref(ba, "wo-investigate")
        with pytest.raises(ba.NotAuthorable) as exc:
            ba.author_branch(
                str(repo),
                base_ref="main",
                branch_name="fix/should-never-exist",
                backend="fixture",
                backend_config=FIX_A,
                workorder_ref="wo-investigate",
                work_type="investigation",
            )
        assert exc.value.work_type == "investigation"
        assert "investigation" in str(exc.value)
        assert exc.value.workorder_ref == "wo-investigate"
        assert not isinstance(exc.value, ba.AuthoringError)
        assert not ba._branch_exists(str(repo), "fix/should-never-exist")
        assert raw.execute("SELECT COUNT(*) FROM fix_branch").fetchone()[0] == 0
        assert raw.execute(
            "SELECT COUNT(*) FROM branch_authoring"
        ).fetchone()[0] == 0

    def test_refusal_is_recorded_for_human_routing(self, ba, repo):
        _seed_ref(ba, "wo-investigate")
        with pytest.raises(ba.NotAuthorable) as exc:
            ba.author_branch(
                str(repo),
                base_ref="main",
                branch_name="fix/nope",
                backend="fixture",
                backend_config=FIX_A,
                workorder_ref="wo-investigate",
                work_type="investigation",
            )
        rows = ba.refusals("wo-investigate")
        assert len(rows) == 1
        assert rows[0]["id"] == exc.value.refusal_id
        assert rows[0]["work_type"] == "investigation"
        assert rows[0]["disposition"] == "route-to-human"
        assert rows[0]["branch_name"] == "fix/nope"
        assert ba.refusals() == rows

    def test_refusals_are_append_only(self, ba, repo, raw):
        ba._record_refusal("investigation", workorder_ref="wo-x")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            raw.execute("UPDATE work_type_refusal SET work_type='feature'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM work_type_refusal")

    @pytest.mark.parametrize(
        "work_type", ["bug-fix", "feature", "change-request", "refactor"]
    )
    def test_authorable_work_types_still_author(self, ba, fb, repo, work_type):
        _seed_ref(ba, "wo-42")
        res = ba.author_branch(
            str(repo),
            base_ref="main",
            branch_name=f"fix/{work_type}",
            backend="fixture",
            backend_config=FIX_A,
            workorder_ref="wo-42",
            work_type=work_type,
        )
        assert res["head_sha"] != res["base_sha"]
        assert fb.get_branch(res["fix_branch_id"])["branch_name"] == f"fix/{work_type}"
        assert ba.refusals() == []

    def test_absent_work_type_does_not_enforce_routing(self, ba, repo):
        _seed_ref(ba, "wo-42")
        res = ba.author_branch(
            str(repo),
            base_ref="main",
            branch_name="fix/no-work-type",
            backend="fixture",
            backend_config=FIX_A,
            workorder_ref="wo-42",
        )
        assert res["backend"] == "fixture"
        assert ba.refusals() == []

    def test_unknown_work_type_fails_closed_to_a_human(self, ba, repo):
        _seed_ref(ba, "wo-42")
        with pytest.raises(ba.NotAuthorable, match="not one of"):
            ba.author_branch(
                str(repo),
                base_ref="main",
                branch_name="fix/unknown-type",
                backend="fixture",
                backend_config=FIX_A,
                workorder_ref="wo-42",
                work_type="epic-saga",
            )
        assert ba.refusals()[0]["detail"] == "unknown work type"
        assert not ba._branch_exists(str(repo), "fix/unknown-type")

    def test_candidates_refuse_once_for_the_whole_batch(self, ba, repo, raw):
        _seed_ref(ba, "wo-investigate")
        with pytest.raises(ba.NotAuthorable):
            ba.author_candidates(
                str(repo),
                base_ref="main",
                candidates=[
                    {"branch_name": "fix/a", "backend": "fixture",
                     "backend_config": FIX_A},
                    {"branch_name": "fix/b", "backend": "fixture",
                     "backend_config": FIX_B},
                ],
                workorder_ref="wo-investigate",
                work_type="investigation",
            )
        assert len(ba.refusals()) == 1
        assert raw.execute("SELECT COUNT(*) FROM fix_branch").fetchone()[0] == 0

    def test_is_authorable_matches_the_work_type_vocabulary(self, ba):
        from bin.workorder import WORK_TYPES

        assert set(ba.AUTHORABLE_WORK_TYPES) | set(ba.HUMAN_WORK_TYPES) == set(
            WORK_TYPES
        )
        assert ba.is_authorable(None) is True
        assert ba.is_authorable("bug-fix") is True
        assert ba.is_authorable("investigation") is False

    def test_cli_exits_3_on_a_routed_work_order(self, ba, repo, tmp_path, capsys):
        _seed_ref(ba, "wo-investigate")
        cfg = tmp_path / "investigation.json"
        cfg.write_text(json.dumps({
            "base_ref": "main",
            "branch_name": "fix/cli-investigation",
            "backend": "fixture",
            "backend_config": FIX_A,
            "workorder_ref": "wo-investigate",
            "work_type": "investigation",
        }))
        assert ba.main(["author", "--repo", str(repo), "--config", str(cfg)]) == 3
        err = capsys.readouterr().err
        assert "routed-to-human" in err and "investigation" in err

class TestRouteToHuman:
    def test_records_the_same_ledger_row_the_refusal_does(self, ba, repo, raw):
        _seed_ref(ba, "wo-investigate")
        refusal_id = ba.route_to_human(
            "investigation",
            workorder_ref="wo-investigate",
            detail="rank 1, dispatched to a person",
        )
        rows = ba.refusals("wo-investigate")
        assert [r["id"] for r in rows] == [refusal_id]
        assert rows[0]["work_type"] == "investigation"
        assert rows[0]["disposition"] == "route-to-human"
        assert rows[0]["detail"] == "rank 1, dispatched to a person"
        assert raw.execute("SELECT COUNT(*) FROM fix_branch").fetchone()[0] == 0
        assert raw.execute(
            "SELECT COUNT(*) FROM branch_authoring"
        ).fetchone()[0] == 0
        assert _branch_names(repo) == ["main"], (
            "routing to a person touches git not at all"
        )

    def test_an_undeclared_work_type_can_be_routed_too(self, ba):
        first = ba.route_to_human("(undeclared)", workorder_ref="wo-x")
        second = ba.route_to_human("epic-saga", workorder_ref="wo-x")
        rows = ba.refusals("wo-x")
        assert [r["id"] for r in rows] == [first, second]
        assert [r["work_type"] for r in rows] == ["(undeclared)", "epic-saga"]
        assert all(r["detail"] for r in rows), (
            "a routed item always says why it is waiting for a person"
        )

    def test_routed_rows_are_append_only(self, ba, raw):
        ba.route_to_human("investigation", workorder_ref="wo-x")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            raw.execute("UPDATE work_type_refusal SET work_type='feature'")

class TestAuthorContext:
    def test_context_reaches_the_command_backend(self, ba, repo, tmp_path):
        script = _stub_script(
            tmp_path / "ctx.sh",
            'printf "%s %s %s\\n" "$WORKORDER_TITLE" "$WORKORDER_WORK_TYPE" '
            '"$WORKORDER_RANK" > ctx.txt\n',
        )
        _seed_ref(ba, "wo-ctx")
        ba.author_branch(
            str(repo),
            base_ref="main",
            branch_name="fix/ctx",
            backend="command",
            backend_config={"argv": [str(script)], "timeout_s": 30},
            workorder_ref="wo-ctx",
            work_type="bug-fix",
            author_context={
                "WORKORDER_TITLE": "retry deadline",
                "WORKORDER_WORK_TYPE": "bug-fix",
                "WORKORDER_RANK": 1,
            },
        )
        assert _git(repo, "show", "fix/ctx:ctx.txt") == "retry deadline bug-fix 1"

    def test_context_is_not_recorded_in_provenance(self, ba, repo, tmp_path, raw):
        script = _stub_script(tmp_path / "ctx2.sh", "echo hi > out.txt\n")
        _seed_ref(ba, "wo-ctx")
        res = ba.author_branch(
            str(repo),
            base_ref="main",
            branch_name="fix/ctx2",
            backend="command",
            backend_config={"argv": [str(script)], "timeout_s": 30},
            workorder_ref="wo-ctx",
            author_context={"WORKORDER_TITLE": "do-not-store-me"},
        )
        prov = raw.execute(
            "SELECT provenance FROM branch_authoring WHERE id=?",
            (res["authoring_id"],),
        ).fetchone()[0]
        assert "do-not-store-me" not in prov
        assert json.loads(prov)["backend"] == "command"

    @pytest.mark.parametrize(
        "context, match",
        [
            ({"lower_case": "x"}, "not one of"),
            ({"2BAD": "x"}, "not one of"),
            ({"BASE_SHA": "deadbeef"}, "set by the backend itself"),
            ({"BRANCH_NAME": "evil"}, "set by the backend itself"),
            ({"WORKORDER_REF": "evil"}, "set by the backend itself"),
            ({"PATH": "/tmp/evil"}, "not one of"),
            ({"LD_PRELOAD": "/tmp/evil.so"}, "not one of"),
            ({"GIT_CONFIG_GLOBAL": "/tmp/evil.gitconfig"}, "not one of"),
            ({"GIT_SSH_COMMAND": "ssh -o ProxyCommand=id"}, "not one of"),
            ({"GIT_AUTHOR_NAME": "somebody else"}, "not one of"),
            ({"WORKORDER_TITLE": "one\ntwo"}, "control character"),
            ({"WORKORDER_TITLE": "x" * 4097}, "over the 4096 cap"),
        ],
    )
    def test_a_bad_context_is_refused_before_any_git_mutation(
        self, ba, repo, tmp_path, context, match
    ):
        script = _stub_script(tmp_path / "ctx3.sh", "echo hi > out.txt\n")
        _seed_ref(ba, "wo-ctx")
        with pytest.raises(ba.AuthoringError, match=match):
            ba.author_branch(
                str(repo),
                base_ref="main",
                branch_name="fix/ctx3",
                backend="command",
                backend_config={"argv": [str(script)], "timeout_s": 30},
                workorder_ref="wo-ctx",
                author_context=context,
            )
        assert not ba._branch_exists(str(repo), "fix/ctx3")

    def test_every_allowlisted_key_reaches_the_backend(self, ba, repo, tmp_path):
        script = _stub_script(
            tmp_path / "ctxall.sh",
            'env | grep "^WORKORDER_" | sort > seen.txt\n',
        )
        _seed_ref(ba, "wo-ctx")
        context = {name: f"v-{name.lower()}"
                   for name in ba.AUTHOR_CONTEXT_ALLOWLIST}
        ba.author_branch(
            str(repo),
            base_ref="main",
            branch_name="fix/ctxall",
            backend="command",
            backend_config={"argv": [str(script)], "timeout_s": 30},
            workorder_ref="wo-ctx",
            work_type="bug-fix",
            author_context=context,
        )
        seen = dict(
            line.split("=", 1)
            for line in _git(repo, "show", "fix/ctxall:seen.txt").splitlines()
        )
        assert {k: seen[k] for k in context} == context

    def test_the_hermetic_git_environment_is_not_the_items_to_set(
        self, ba, repo, tmp_path
    ):
        """The denylist this replaced named three keys, so a caller could
        hand the backend its own PATH and a bare argv[0] would resolve
        through it."""
        script = _stub_script(tmp_path / "ctxenv.sh", "echo hi > out.txt\n")
        _seed_ref(ba, "wo-ctx")
        for name in ("PATH", "LD_PRELOAD", "GIT_CONFIG_GLOBAL"):
            with pytest.raises(ba.AuthoringError) as excinfo:
                ba.author_branch(
                    str(repo),
                    base_ref="main",
                    branch_name="fix/ctxenv",
                    backend="command",
                    backend_config={"argv": [str(script)], "timeout_s": 30},
                    workorder_ref="wo-ctx",
                    author_context={name: "/tmp/evil"},
                )
            assert name in str(excinfo.value)
        assert not ba._branch_exists(str(repo), "fix/ctxenv")

    def test_a_context_bigger_than_the_allowlist_is_refused(self, ba):
        extra = {name: "x" for name in ba.AUTHOR_CONTEXT_ALLOWLIST}
        extra["WORKORDER_EXTRA"] = "x"
        with pytest.raises(ba.AuthoringError, match="at most"):
            ba._validated_author_context(extra)

    def test_no_context_leaves_the_old_env_contract_alone(self, ba, repo, tmp_path):
        script = _stub_script(
            tmp_path / "ctx4.sh",
            'printf "%s|%s\\n" "$WORKORDER_REF" "$WORKORDER_TITLE" > env.txt\n',
        )
        _seed_ref(ba, "wo-ctx")
        ba.author_branch(
            str(repo),
            base_ref="main",
            branch_name="fix/ctx4",
            backend="command",
            backend_config={"argv": [str(script)], "timeout_s": 30},
            workorder_ref="wo-ctx",
        )
        assert _git(repo, "show", "fix/ctx4:env.txt") == "wo-ctx|"

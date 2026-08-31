"""Tests for bin/fix_branches.py — SHA-bound branch-fix persistence
(wave-9 B1). Real temp git repos are built in-test (git init, commits,
branches); no network.
"""
from __future__ import annotations

import os
import sqlite3
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
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main", str(repo)],
        capture_output=True,
        env=_env(),
        check=True,
    )
    _commit(repo, "README.md", "hello\n", "initial")
    return repo

def _make_fix_branch(repo: Path, name: str = "fix/widget") -> str:
    _git(repo, "checkout", "-b", name)
    sha = _commit(repo, "fix.txt", "the fix\n", "fix: widget")
    _git(repo, "checkout", "main")
    return sha

@pytest.fixture
def fb(tmp_data_home):
    from bin import fix_branches as mod

    mod.init()
    return mod

@pytest.fixture
def catalog(tmp_data_home):
    from bin import catalog as mod

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

def _policy(catalog, scope="branchfix:*", approvers=("changeme",)):
    return catalog.create_policy(
        name=f"approval-{scope}",
        kind="approval",
        rule={"approvers": list(approvers), "scope": scope},
    )

def _validate_current(fb, bid, **overrides):
    """Record a passing validation of the branch's current head."""
    b = fb.get_branch(bid)
    kwargs = dict(
        passed=True,
        red_cmd="./red.sh",
        red_intended=2,
        red_observed=2,
        green_cmd="./green.sh",
        green_total=5,
        green_passed=5,
        guard_total=3,
        guard_passed=3,
    )
    kwargs.update(overrides)
    return fb.record_validation(bid, b["head_sha"], **kwargs)

def _seed_workorder_ref(mod, ref: str) -> None:
    """Make ``ref`` resolvable as a domain name in the module's DB
    (strict-lineage tests, wave-11 W2)."""
    with mod._connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO domain(name, generator_prompt, "
            "judge_prompt, corpus_source) VALUES (?, '', '', '{}')",
            (ref,),
        )
        conn.commit()

class TestSchema:
    def test_tables_and_triggers_exist(self, fb, raw):
        tables = {
            r["name"]
            for r in raw.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for t in ("fix_branch", "fix_branch_validation", "fix_branch_review"):
            assert t in tables, f"missing table {t}"
        triggers = {
            r["name"]
            for r in raw.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        for tr in (
            "fix_branch_validation_immutable", "fix_branch_validation_no_delete",
            "fix_branch_review_immutable", "fix_branch_review_no_delete",
        ):
            assert tr in triggers, f"missing trigger {tr}"

    def test_validation_rows_append_only(self, fb, repo, raw):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        _validate_current(fb, bid)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            raw.execute("UPDATE fix_branch_validation SET passed=0")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM fix_branch_validation")

    def test_review_rows_append_only(self, fb, repo, raw):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        fb.record_review(bid, reviewer="changeme", decision="reject",
                         rationale="nope")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            raw.execute("UPDATE fix_branch_review SET decision='approve'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM fix_branch_review")

class TestRegister:
    def test_happy_path(self, fb, repo):
        fix_sha = _make_fix_branch(repo)
        main_sha = _git(repo, "rev-parse", "main")
        _seed_workorder_ref(fb, "wo-7")
        bid = fb.register_branch(str(repo), "fix/widget", workorder_ref="wo-7")
        b = fb.get_branch(bid)
        assert b["head_sha"] == fix_sha
        assert b["base_sha"] == main_sha
        assert b["status"] == "registered"
        assert b["workorder_ref"] == "wo-7"
        assert len(b["patch_digest"]) == 64

    def test_patch_digest_is_stable(self, fb, repo, tmp_path):
        """Same patch content => same digest, even from a distinct clone."""
        _make_fix_branch(repo)
        bid1 = fb.register_branch(str(repo), "fix/widget")
        d1 = fb.get_branch(bid1)["patch_digest"]
        clone = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", "--branch", "fix/widget", str(repo), str(clone)],
            capture_output=True, env=_env(), check=True,
        )
        bid2 = fb.register_branch(str(clone), "fix/widget", base="origin/main")
        assert fb.get_branch(bid2)["patch_digest"] == d1

    def test_duplicate_registration_raises(self, fb, repo):
        _make_fix_branch(repo)
        fb.register_branch(str(repo), "fix/widget")
        with pytest.raises(ValueError, match="already registered"):
            fb.register_branch(str(repo), "fix/widget")

    def test_unknown_branch_raises(self, fb, repo):
        with pytest.raises(ValueError, match="rev-parse"):
            fb.register_branch(str(repo), "no-such-branch")

    def test_merge_commits_rejected(self, fb, repo):
        """A merge commit between base and head fails registration."""
        _git(repo, "checkout", "-b", "side")
        _commit(repo, "side.txt", "side\n", "side work")
        _git(repo, "checkout", "main")
        _git(repo, "checkout", "-b", "fix/merged")
        _commit(repo, "fix.txt", "fix\n", "fix work")
        _git(repo, "merge", "--no-ff", "-m", "merge side", "side")
        _git(repo, "checkout", "main")
        with pytest.raises(ValueError, match="merge commits"):
            fb.register_branch(str(repo), "fix/merged")

def _seed_finding(mod, slug="widget-crash") -> int:
    """Create project -> campaign -> finding; returns the finding id."""
    from bin import campaigns, catalog

    catalog.create_project(name="proj-lineage")
    campaigns.create_campaign(project="proj-lineage", name="camp-lineage",
                              kind="bugsweep")
    return campaigns.create_finding(campaign="camp-lineage", slug=slug)

def _seed_pending(mod) -> int:
    """Insert eval_template -> job_configuration -> pending_judgement;
    returns the pending_judgement id."""
    with mod._connect() as conn:
        cur = conn.execute(
            "INSERT INTO eval_template(name, version, output_definition) "
            "VALUES ('t-lineage', 1, '{}')"
        )
        cur = conn.execute(
            "INSERT INTO job_configuration(template_id, rater_type) "
            "VALUES (?, 'llm')",
            (cur.lastrowid,),
        )
        cur = conn.execute(
            "INSERT INTO pending_judgement(config_id, tournament_db_path, "
            "match_id, trace_payload) VALUES (?, '/tmp/t.db', 1, '{}')",
            (cur.lastrowid,),
        )
        conn.commit()
        return cur.lastrowid

class TestLineage:
    """register_branch fail-closed workorder_ref/finding resolution."""

    def test_dangling_workorder_ref_refused(self, fb, repo):
        _make_fix_branch(repo)
        with pytest.raises(ValueError, match="workorder_ref does not resolve"):
            fb.register_branch(str(repo), "fix/widget",
                               workorder_ref="wo-nowhere")
        assert fb.list_branches() == []

    def test_finding_slug_resolves(self, fb, repo):
        _make_fix_branch(repo)
        _seed_finding(fb, slug="widget-crash")
        bid = fb.register_branch(str(repo), "fix/widget",
                                 workorder_ref="widget-crash")
        assert fb.get_branch(bid)["workorder_ref"] == "widget-crash"

    def test_domain_name_resolves(self, fb, repo):
        _make_fix_branch(repo)
        _seed_workorder_ref(fb, "unity-crashes")
        bid = fb.register_branch(str(repo), "fix/widget",
                                 workorder_ref="unity-crashes")
        assert fb.get_branch(bid)["workorder_ref"] == "unity-crashes"

    def test_pending_judgement_id_resolves(self, fb, repo):
        _make_fix_branch(repo)
        pid = _seed_pending(fb)
        bid = fb.register_branch(str(repo), "fix/widget",
                                 workorder_ref=str(pid))
        assert fb.get_branch(bid)["workorder_ref"] == str(pid)

    def test_intlike_ref_without_pending_row_refused(self, fb, repo):
        _make_fix_branch(repo)
        with pytest.raises(ValueError, match="workorder_ref does not resolve"):
            fb.register_branch(str(repo), "fix/widget", workorder_ref="99999")

    def test_unknown_finding_id_refused(self, fb, repo):
        _make_fix_branch(repo)
        with pytest.raises(ValueError, match="finding 424242 does not exist"):
            fb.register_branch(str(repo), "fix/widget", finding=424242)

    def test_existing_finding_id_accepted(self, fb, repo):
        _make_fix_branch(repo)
        fid = _seed_finding(fb)
        bid = fb.register_branch(str(repo), "fix/widget", finding=fid)
        assert fb.get_branch(bid)["finding_id"] == fid

    def test_escape_hatch_stamps_unresolved(self, fb, repo):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget",
                                 workorder_ref="wo-nowhere",
                                 allow_unresolved=True)
        assert fb.get_branch(bid)["workorder_ref"] == \
            "unresolved-ref:wo-nowhere"

    def test_escape_hatch_does_not_cover_finding(self, fb, repo):
        """allow_unresolved covers the workorder_ref only — a bogus
        finding id is refused regardless."""
        _make_fix_branch(repo)
        with pytest.raises(ValueError, match="finding 424242 does not exist"):
            fb.register_branch(str(repo), "fix/widget", finding=424242,
                               allow_unresolved=True)

    def test_cli_allow_unresolved_flag(self, fb, repo, capsys):
        _make_fix_branch(repo)
        assert fb.main(["register", "--repo", str(repo),
                        "--branch", "fix/widget",
                        "--workorder-ref", "wo-cli-dangling",
                        "--allow-unresolved"]) == 0
        import json as _json

        bid = _json.loads(capsys.readouterr().out)["id"]
        assert fb.get_branch(bid)["workorder_ref"] == \
            "unresolved-ref:wo-cli-dangling"

class TestRefresh:
    def test_unchanged_head_is_noop(self, fb, repo):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        b = fb.refresh_head(bid)
        assert b["status"] == "registered"

    def test_amend_marks_stale_and_strands_validation(self, fb, repo):
        old_sha = _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        _validate_current(fb, bid)
        assert fb.get_branch(bid)["status"] == "validated"
        assert fb.current_validation(bid) is not None
        _git(repo, "checkout", "fix/widget")
        (repo / "fix.txt").write_text("the better fix\n")
        _git(repo, "add", "fix.txt")
        _git(repo, "commit", "--amend", "-m", "fix: widget v2")
        _git(repo, "checkout", "main")
        b = fb.refresh_head(bid)
        assert b["head_sha"] != old_sha
        assert b["status"] == "stale"
        assert len(b["validations"]) == 1
        assert fb.current_validation(bid) is None

    def test_refresh_updates_patch_digest(self, fb, repo):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        d1 = fb.get_branch(bid)["patch_digest"]
        _git(repo, "checkout", "fix/widget")
        _commit(repo, "more.txt", "more\n", "fix: more")
        _git(repo, "checkout", "main")
        b = fb.refresh_head(bid)
        assert b["patch_digest"] != d1

    def test_refresh_rejects_new_merge_commit(self, fb, repo):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        _git(repo, "checkout", "-b", "side2")
        _commit(repo, "s2.txt", "s2\n", "side2")
        _git(repo, "checkout", "fix/widget")
        _git(repo, "merge", "--no-ff", "-m", "merge side2", "side2")
        _git(repo, "checkout", "main")
        with pytest.raises(ValueError, match="merge commits"):
            fb.refresh_head(bid)

class TestRecordValidation:
    def test_wrong_tested_sha_refused(self, fb, repo):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        with pytest.raises(ValueError, match="does not match current head"):
            fb.record_validation(bid, "0" * 40, passed=True)

    def test_failed_validation_sets_failed(self, fb, repo):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        _validate_current(fb, bid, passed=False, guard_passed=2)
        b = fb.get_branch(bid)
        assert b["status"] == "failed"
        cv = fb.current_validation(bid)
        assert cv is not None and cv["passed"] == 0

    def test_current_validation_is_latest_matching_row(self, fb, repo):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        _validate_current(fb, bid, passed=False)
        vid2 = _validate_current(fb, bid, passed=True)
        cv = fb.current_validation(bid)
        assert cv["id"] == vid2 and cv["passed"] == 1
        assert fb.get_branch(bid)["status"] == "validated"

class TestReview:
    def test_approve_without_current_passed_validation_raises(self, fb, repo, catalog):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        _policy(catalog)
        with pytest.raises(ValueError, match="approve requires a passed validation"):
            fb.record_review(bid, reviewer="changeme", decision="approve")

    def test_approve_with_failed_validation_raises(self, fb, repo, catalog):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        _validate_current(fb, bid, passed=False)
        _policy(catalog)
        with pytest.raises(ValueError, match="approve requires a passed validation"):
            fb.record_review(bid, reviewer="changeme", decision="approve")

    def test_approve_without_policy_fails_closed(self, fb, repo):
        from bin.approvals import ApprovalDenied

        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        _validate_current(fb, bid)
        with pytest.raises(ApprovalDenied):
            fb.record_review(bid, reviewer="changeme", decision="approve")
        b = fb.get_branch(bid)
        assert b["reviews"] == []
        assert b["status"] == "validated"

    def test_approve_writes_audit_row(self, fb, repo, catalog, raw):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        _validate_current(fb, bid)
        _policy(catalog, scope="branchfix:*")
        res = fb.record_review(
            bid, reviewer="changeme", decision="approve", rationale="lgtm"
        )
        assert res["approval_event_id"] is not None
        b = fb.get_branch(bid)
        assert b["status"] == "approved"
        assert b["reviews"][0]["approval_event_id"] == res["approval_event_id"]
        assert b["reviews"][0]["tested_sha"] == b["head_sha"]
        ev = raw.execute(
            "SELECT * FROM approval_event WHERE id=?",
            (res["approval_event_id"],),
        ).fetchone()
        assert ev is not None
        assert ev["decision"] == "approved"
        assert ev["approver"] == "changeme"
        wf = f"branchfix:fix/widget:{b['head_sha'][:12]}"
        assert ev["temporal_workflow_id"] == wf

    def test_approver_outside_scope_denied(self, fb, repo, catalog):
        from bin.approvals import ApprovalDenied

        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        _validate_current(fb, bid)
        _policy(catalog, scope="release:*")
        with pytest.raises(ApprovalDenied):
            fb.record_review(bid, reviewer="changeme", decision="approve")

    def test_reject_on_failed_branch_ok(self, fb, repo):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        _validate_current(fb, bid, passed=False)
        res = fb.record_review(
            bid, reviewer="changeme", decision="reject", rationale="wrong approach"
        )
        assert res["approval_event_id"] is None
        assert fb.get_branch(bid)["status"] == "rejected"

    def test_needs_changes_keeps_validated(self, fb, repo):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        _validate_current(fb, bid)
        fb.record_review(bid, reviewer="changeme", decision="needs-changes",
                         rationale="rename the helper")
        assert fb.get_branch(bid)["status"] == "validated"

    def test_head_change_after_approval_marks_stale(self, fb, repo, catalog):
        """Approval is retained in history but the branch is no longer
        approved-current after the head moves."""
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        _validate_current(fb, bid)
        _policy(catalog)
        res = fb.record_review(bid, reviewer="changeme", decision="approve")
        assert fb.get_branch(bid)["status"] == "approved"
        approved_sha = fb.get_branch(bid)["head_sha"]
        _git(repo, "checkout", "fix/widget")
        _commit(repo, "late.txt", "late change\n", "late tweak")
        _git(repo, "checkout", "main")
        b = fb.refresh_head(bid)
        assert b["status"] == "stale"
        assert b["head_sha"] != approved_sha
        assert len(b["reviews"]) == 1
        assert b["reviews"][0]["decision"] == "approve"
        assert b["reviews"][0]["tested_sha"] == approved_sha
        assert b["reviews"][0]["approval_event_id"] == res["approval_event_id"]
        assert fb.current_validation(bid) is None

class TestDiffs:
    """Content-addressed diffs (wave-10 V2): register/refresh store the
    unified diff at $DATA_TOURNAMENTS_HOME/branch-diffs/<patch_digest>.patch;
    get_branch carries 'diff' (text or None) + 'changed_files'."""

    def test_register_writes_content_addressed_diff(self, fb, repo,
                                                    tmp_data_home):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        b = fb.get_branch(bid)
        patch_file = tmp_data_home / "branch-diffs" / f"{b['patch_digest']}.patch"
        assert patch_file.exists()
        text = patch_file.read_text()
        expected = _git(repo, "diff", f"{b['base_sha']}..{b['head_sha']}")
        assert text.strip() == expected.strip()
        assert "fix.txt" in text
        import hashlib

        assert hashlib.sha256(patch_file.read_bytes()).hexdigest() == \
            b["patch_digest"]

    def test_get_branch_carries_diff_and_changed_files(self, fb, repo):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        b = fb.get_branch(bid)
        assert b["diff"] is not None
        assert "+the fix" in b["diff"]
        assert b["changed_files"] == [{"status": "A", "path": "fix.txt"}]

    def test_missing_diff_file_yields_none(self, fb, repo, tmp_data_home):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        b = fb.get_branch(bid)
        (tmp_data_home / "branch-diffs" / f"{b['patch_digest']}.patch").unlink()
        assert fb.get_branch(bid)["diff"] is None

    def test_refresh_writes_new_diff_file(self, fb, repo, tmp_data_home):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        d1 = fb.get_branch(bid)["patch_digest"]
        _git(repo, "checkout", "fix/widget")
        _commit(repo, "more.txt", "more\n", "fix: more")
        _git(repo, "checkout", "main")
        b = fb.refresh_head(bid)
        assert b["patch_digest"] != d1
        new_file = tmp_data_home / "branch-diffs" / f"{b['patch_digest']}.patch"
        assert new_file.exists()
        assert "more.txt" in new_file.read_text()
        assert b["diff"] is not None and "more.txt" in b["diff"]
        assert {"status": "A", "path": "more.txt"} in b["changed_files"]

class TestMarkShipped:
    def test_only_approved_can_ship(self, fb, repo):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        with pytest.raises(ValueError, match="only an approved branch"):
            fb.mark_shipped(bid)
        assert fb.get_branch(bid)["status"] == "registered"

    def test_approved_flips_to_shipped(self, fb, repo, catalog):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        _validate_current(fb, bid)
        _policy(catalog)
        fb.record_review(bid, reviewer="changeme", decision="approve")
        fb.mark_shipped(bid)
        assert fb.get_branch(bid)["status"] == "shipped"
        with pytest.raises(ValueError, match="only an approved branch"):
            fb.mark_shipped(bid)

class TestShipState:
    """mark_shipping / latest_ship / set_ship_outcome (wave-11 W2)."""

    def _approved(self, fb, catalog, repo) -> int:
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        _validate_current(fb, bid)
        _policy(catalog)
        fb.record_review(bid, reviewer="changeme", decision="approve")
        return bid

    def test_mark_shipping_records_row(self, fb, repo, catalog, raw):
        bid = self._approved(fb, catalog, repo)
        head = fb.get_branch(bid)["head_sha"]
        sid = fb.mark_shipping(bid, workflow_id="release:o/r:abc",
                               tested_sha=head, requested_by="changeme")
        assert fb.get_branch(bid)["status"] == "shipping"
        row = fb.latest_ship(bid)
        assert row["id"] == sid
        assert row["workflow_id"] == "release:o/r:abc"
        assert row["tested_sha"] == head
        assert row["requested_by"] == "changeme"
        import sqlite3 as _sq

        with pytest.raises(_sq.IntegrityError, match="immutable"):
            raw.execute("UPDATE fix_branch_ship SET workflow_id='x'")
        with pytest.raises(_sq.IntegrityError, match="append-only"):
            raw.execute("DELETE FROM fix_branch_ship")

    def test_mark_shipping_requires_approved(self, fb, repo):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        with pytest.raises(ValueError, match="only an approved branch"):
            fb.mark_shipping(bid, workflow_id="w", tested_sha="s",
                             requested_by="changeme")
        assert fb.latest_ship(bid) is None

    def test_set_ship_outcome_shipped_and_rolled_back(self, fb, repo, catalog):
        bid = self._approved(fb, catalog, repo)
        head = fb.get_branch(bid)["head_sha"]
        fb.mark_shipping(bid, workflow_id="w1", tested_sha=head,
                         requested_by="changeme")
        fb.set_ship_outcome(bid, "shipped")
        assert fb.get_branch(bid)["status"] == "shipped"
        with pytest.raises(ValueError, match="only a shipping branch"):
            fb.set_ship_outcome(bid, "rolled-back")

    def test_set_ship_outcome_rejects_unknown(self, fb, repo, catalog):
        bid = self._approved(fb, catalog, repo)
        with pytest.raises(ValueError, match="'shipped' or 'rolled-back'"):
            fb.set_ship_outcome(bid, "party")

class TestQueries:
    def test_list_branches_filters(self, fb, repo):
        _make_fix_branch(repo, "fix/a")
        _make_fix_branch(repo, "fix/b")
        bid_a = fb.register_branch(str(repo), "fix/a")
        fb.register_branch(str(repo), "fix/b")
        _validate_current(fb, bid_a)
        assert len(fb.list_branches()) == 2
        assert [b["branch_name"] for b in fb.list_branches(status="validated")] == [
            "fix/a"
        ]
        assert [b["branch_name"] for b in fb.list_branches(status="registered")] == [
            "fix/b"
        ]

    def test_list_unknown_status_raises(self, fb):
        with pytest.raises(ValueError, match="unknown status"):
            fb.list_branches(status="bogus")

    def test_get_unknown_id_raises(self, fb):
        with pytest.raises(LookupError, match="no fix_branch"):
            fb.get_branch(999)

    def test_cli_register_list_get(self, fb, repo, capsys):
        _make_fix_branch(repo)
        assert fb.main(["register", "--repo", str(repo),
                        "--branch", "fix/widget"]) == 0
        assert fb.main(["list"]) == 0
        assert fb.main(["get", "--id", "1"]) == 0
        out = capsys.readouterr().out
        assert "fix/widget" in out

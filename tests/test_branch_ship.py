"""Tests for bin/branch_ship.py — the fail-closed ship gateway
(wave-10 V3). The release client is stubbed with a tiny script that
records its argv (never imports temporalio); every refusal path is
asserted by machine-readable reason code.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from tests.test_fix_branches import (
    _commit,
    _git,
    _make_fix_branch,
    _make_repo,
    _policy,
    _validate_current,
)


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
def ship(fb):
    from bin import branch_ship as mod

    return mod


@pytest.fixture
def repo(tmp_path) -> Path:
    return _make_repo(tmp_path)


@pytest.fixture
def stub_client(tmp_path):
    """A stub release client: records argv as JSON, prints the 'started
    <workflow_id>' line the real client prints."""
    record = tmp_path / "client-argv.json"
    script = tmp_path / "stub_client.py"
    script.write_text(
        "import json, sys\n"
        f"open({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        "repo, commit = sys.argv[2], sys.argv[3]\n"
        "print(f'started release:{repo}:{commit}')\n"
    )
    return {"cmd": f"{sys.executable} {script}", "record": record}


@pytest.fixture
def failing_client(tmp_path):
    """A stub client that exits nonzero (temporal down, etc.)."""
    script = tmp_path / "boom_client.py"
    script.write_text(
        "import sys\n"
        "print('cannot connect to temporal', file=sys.stderr)\n"
        "sys.exit(3)\n"
    )
    return f"{sys.executable} {script}"


def _approved_branch(fb, catalog, repo) -> int:
    """Register + validate + approve fix/widget; returns the branch id."""
    _make_fix_branch(repo)
    bid = fb.register_branch(str(repo), "fix/widget")
    _validate_current(fb, bid)
    _policy(catalog)
    fb.record_review(bid, reviewer="esteban", decision="approve")
    return bid


class TestShipAccepted:
    def test_approved_current_ships_with_derived_sha(self, fb, catalog, repo,
                                                     ship, stub_client):
        bid = _approved_branch(fb, catalog, repo)
        head = fb.get_branch(bid)["head_sha"]
        res = ship.ship_branch(
            bid,
            requested_by="esteban",
            project="proj-x",
            domain="unity",
            client_cmd=stub_client["cmd"],
        )
        # commit DERIVES from the record — never caller-supplied
        assert res["head_sha"] == head
        assert res["repo"] == repo.name  # no origin remote -> basename
        assert res["workflow_id"] == f"release:{repo.name}:{head}"
        argv = json.loads(stub_client["record"].read_text())
        assert argv[0] == "start"
        assert argv[1] == repo.name
        assert argv[2] == head
        assert argv[argv.index("--project") + 1] == "proj-x"
        assert argv[argv.index("--domain") + 1] == "unity"
        assert argv[argv.index("--requested-by") + 1] == "esteban"
        # a release is IN FLIGHT: 'shipping', not terminal 'shipped'
        # ('shipped' means release-COMPLETED, projected by sync_completion)
        assert fb.get_branch(bid)["status"] == "shipping"
        # the ship row records the started workflow bound to the tested SHA
        row = fb.latest_ship(bid)
        assert row["id"] == res["ship_id"]
        assert row["workflow_id"] == res["workflow_id"]
        assert row["tested_sha"] == head
        assert row["requested_by"] == "esteban"
        assert row["approval_review_id"] is not None
        assert row["validation_id"] is not None
        # a second ship of the same approval refuses while in flight
        with pytest.raises(ship.ShipRefused) as exc:
            ship.ship_branch(bid, requested_by="esteban",
                             client_cmd=stub_client["cmd"])
        assert exc.value.reason == "ship-in-progress"

    def test_env_override_client_cmd(self, fb, catalog, repo, ship,
                                     stub_client, monkeypatch):
        bid = _approved_branch(fb, catalog, repo)
        monkeypatch.setenv(ship.CLIENT_CMD_ENV, stub_client["cmd"])
        res = ship.ship_branch(bid, requested_by="esteban")
        assert stub_client["record"].exists()
        assert res["repo"] == repo.name


class TestShipRefused:
    def test_failed_branch_refused(self, fb, repo, ship, stub_client):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        _validate_current(fb, bid, passed=False)
        with pytest.raises(ship.ShipRefused) as exc:
            ship.ship_branch(bid, requested_by="esteban",
                             client_cmd=stub_client["cmd"])
        assert exc.value.reason == "failed"
        assert not stub_client["record"].exists()

    def test_rejected_branch_refused(self, fb, repo, ship, stub_client):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        fb.record_review(bid, reviewer="esteban", decision="reject",
                         rationale="wrong approach")
        with pytest.raises(ship.ShipRefused) as exc:
            ship.ship_branch(bid, requested_by="esteban",
                             client_cmd=stub_client["cmd"])
        assert exc.value.reason == "rejected"
        assert not stub_client["record"].exists()

    def test_merely_validated_refused(self, fb, repo, ship, stub_client):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        _validate_current(fb, bid)
        with pytest.raises(ship.ShipRefused) as exc:
            ship.ship_branch(bid, requested_by="esteban",
                             client_cmd=stub_client["cmd"])
        assert exc.value.reason == "not-approved"

    def test_stale_branch_refused(self, fb, catalog, repo, ship, stub_client):
        """Head moved and refresh_head already ran: status='stale'."""
        bid = _approved_branch(fb, catalog, repo)
        _git(repo, "checkout", "fix/widget")
        _commit(repo, "late.txt", "late\n", "late tweak")
        _git(repo, "checkout", "main")
        fb.refresh_head(bid)
        assert fb.get_branch(bid)["status"] == "stale"
        with pytest.raises(ship.ShipRefused) as exc:
            ship.ship_branch(bid, requested_by="esteban",
                             client_cmd=stub_client["cmd"])
        assert exc.value.reason == "stale"
        assert not stub_client["record"].exists()

    def test_tip_moved_after_approval_refused_via_refresh(self, fb, catalog,
                                                          repo, ship,
                                                          stub_client):
        """The DB still says 'approved' but the tip moved AFTER approval —
        the gateway's own refresh_head must detect it and refuse."""
        bid = _approved_branch(fb, catalog, repo)
        _git(repo, "checkout", "fix/widget")
        _commit(repo, "sneak.txt", "sneak\n", "sneak in after approval")
        _git(repo, "checkout", "main")
        # NO refresh_head here — the record still reads 'approved'
        assert fb.get_branch(bid)["status"] == "approved"
        with pytest.raises(ship.ShipRefused) as exc:
            ship.ship_branch(bid, requested_by="esteban",
                             client_cmd=stub_client["cmd"])
        assert exc.value.reason == "stale"
        assert not stub_client["record"].exists()
        # and the record now reflects reality
        assert fb.get_branch(bid)["status"] == "stale"

    def test_client_failure_surfaces_no_false_success(self, fb, catalog, repo,
                                                      ship, failing_client):
        bid = _approved_branch(fb, catalog, repo)
        with pytest.raises(ship.ShipClientError, match="exited 3"):
            ship.ship_branch(bid, requested_by="esteban",
                             client_cmd=failing_client)
        # NOT flipped to shipped — the ship did not happen
        assert fb.get_branch(bid)["status"] == "approved"


class TestRefusalMatrix:
    def test_shape_and_values_for_approved(self, fb, catalog, repo, ship):
        bid = _approved_branch(fb, catalog, repo)
        m = ship.refusal_matrix(bid)
        assert m["fix_branch_id"] == bid
        assert m["status"] == "approved"
        assert m["ship_allowed"] is True
        assert m["refusal_reason"] is None
        assert m["approved_sha"] == m["head_sha"]
        assert m["gates"] == {
            "head-current": True,
            "status-approved": True,
            "approval-current": True,
            "validation-passed": True,
            "no-ship-in-progress": True,
            "approval-fresh": True,
        }

    def test_matrix_for_failed_branch(self, fb, repo, ship):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        _validate_current(fb, bid, passed=False)
        m = ship.refusal_matrix(bid)
        assert m["ship_allowed"] is False
        assert m["refusal_reason"] == "failed"
        assert m["gates"]["status-approved"] is False
        assert m["gates"]["validation-passed"] is False

    def test_matrix_detects_moved_tip(self, fb, catalog, repo, ship):
        bid = _approved_branch(fb, catalog, repo)
        _git(repo, "checkout", "fix/widget")
        _commit(repo, "late.txt", "late\n", "late")
        _git(repo, "checkout", "main")
        m = ship.refusal_matrix(bid)  # refreshes internally
        assert m["ship_allowed"] is False
        assert m["refusal_reason"] == "stale"
        assert m["gates"]["head-current"] is False


class TestCli:
    def test_check_prints_matrix_json(self, fb, catalog, repo, ship, capsys):
        bid = _approved_branch(fb, catalog, repo)
        assert ship.main(["check", "--id", str(bid)]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ship_allowed"] is True

    def test_ship_cli_refusal_exit_code(self, fb, repo, ship, stub_client,
                                        capsys, monkeypatch):
        _make_fix_branch(repo)
        bid = fb.register_branch(str(repo), "fix/widget")
        monkeypatch.setenv(ship.CLIENT_CMD_ENV, stub_client["cmd"])
        assert ship.main(["ship", "--id", str(bid),
                          "--requested-by", "esteban"]) == 1
        out = json.loads(capsys.readouterr().out)
        assert out["refused"] == "not-approved"

    def test_ship_cli_success(self, fb, catalog, repo, ship, stub_client,
                              capsys, monkeypatch):
        bid = _approved_branch(fb, catalog, repo)
        monkeypatch.setenv(ship.CLIENT_CMD_ENV, stub_client["cmd"])
        assert ship.main(["ship", "--id", str(bid),
                          "--requested-by", "esteban"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["workflow_id"].startswith("release:")
        # workflow STARTED -> 'shipping'; 'shipped' comes from sync
        assert fb.get_branch(bid)["status"] == "shipping"

    def test_ship_cli_repo_name_flag(self, fb, catalog, repo, ship,
                                     stub_client, capsys, monkeypatch):
        bid = _approved_branch(fb, catalog, repo)
        monkeypatch.setenv(ship.CLIENT_CMD_ENV, stub_client["cmd"])
        assert ship.main(["ship", "--id", str(bid),
                          "--requested-by", "esteban",
                          "--repo-name", "acme/widgets"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["repo"] == "acme/widgets"
        argv = json.loads(stub_client["record"].read_text())
        assert argv[1] == "acme/widgets"

    def test_sync_cli(self, fb, catalog, repo, ship, stub_client, capsys):
        from bin import workflow_runs

        bid = _approved_branch(fb, catalog, repo)
        res = ship.ship_branch(bid, requested_by="esteban",
                               client_cmd=stub_client["cmd"])
        rid = workflow_runs.start(
            temporal_workflow_id=res["workflow_id"], temporal_run_id="r1"
        )
        workflow_runs.set_status(rid, "done")
        assert ship.main(["sync", "--id", str(bid)]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["fix_branch_status"] == "shipped"
        assert out["changed"] is True

    def test_sync_cli_no_run_row_exits_1(self, fb, catalog, repo, ship,
                                         stub_client, capsys):
        bid = _approved_branch(fb, catalog, repo)
        ship.ship_branch(bid, requested_by="esteban",
                         client_cmd=stub_client["cmd"])
        assert ship.main(["sync", "--id", str(bid)]) == 1
        out = json.loads(capsys.readouterr().out)
        assert "no workflow_run row" in out["error"]


class TestCompletionProjection:
    """sync_completion: workflow_run outcome -> fix_branch status
    (wave-11 W2: 'shipped' means release-COMPLETED)."""

    def _shipped_branch(self, fb, catalog, repo, ship, stub_client) -> tuple:
        bid = _approved_branch(fb, catalog, repo)
        res = ship.ship_branch(bid, requested_by="esteban",
                               client_cmd=stub_client["cmd"])
        return bid, res["workflow_id"]

    def test_done_projects_shipped(self, fb, catalog, repo, ship,
                                   stub_client):
        from bin import workflow_runs

        bid, wfid = self._shipped_branch(fb, catalog, repo, ship, stub_client)
        rid = workflow_runs.start(temporal_workflow_id=wfid,
                                  temporal_run_id="r1")
        workflow_runs.set_status(rid, "done")
        out = ship.sync_completion(bid)
        assert out["workflow_status"] == "done"
        assert out["fix_branch_status"] == "shipped"
        assert out["changed"] is True
        assert fb.get_branch(bid)["status"] == "shipped"

    def test_rolled_back_projects_rolled_back(self, fb, catalog, repo, ship,
                                              stub_client):
        from bin import workflow_runs

        bid, wfid = self._shipped_branch(fb, catalog, repo, ship, stub_client)
        rid = workflow_runs.start(temporal_workflow_id=wfid,
                                  temporal_run_id="r1")
        workflow_runs.set_status(rid, "rolled-back")
        out = ship.sync_completion(bid)
        assert out["workflow_status"] == "rolled-back"
        assert out["fix_branch_status"] == "rolled-back"
        assert out["changed"] is True
        assert fb.get_branch(bid)["status"] == "rolled-back"

    def test_still_running_is_noop(self, fb, catalog, repo, ship,
                                   stub_client):
        from bin import workflow_runs

        bid, wfid = self._shipped_branch(fb, catalog, repo, ship, stub_client)
        workflow_runs.start(temporal_workflow_id=wfid, temporal_run_id="r1")
        out = ship.sync_completion(bid)
        assert out["workflow_status"] == "running"
        assert out["fix_branch_status"] == "shipping"
        assert out["changed"] is False
        assert fb.get_branch(bid)["status"] == "shipping"

    def test_missing_run_row_is_honest_error(self, fb, catalog, repo, ship,
                                             stub_client):
        bid, wfid = self._shipped_branch(fb, catalog, repo, ship, stub_client)
        out = ship.sync_completion(bid)
        assert "no workflow_run row" in out["error"]
        assert out["changed"] is False
        assert fb.get_branch(bid)["status"] == "shipping"

    def test_never_shipped_branch_is_honest_error(self, fb, catalog, repo,
                                                  ship):
        bid = _approved_branch(fb, catalog, repo)
        out = ship.sync_completion(bid)
        assert "no ship record" in out["error"]
        assert out["changed"] is False
        assert fb.get_branch(bid)["status"] == "approved"


class TestReShip:
    """Re-ship semantics: a rolled-back branch may not ship again on the
    OLD approval — fresh validation + fresh approving review required."""

    def _rolled_back(self, fb, catalog, repo, ship, stub_client) -> int:
        from bin import workflow_runs

        bid = _approved_branch(fb, catalog, repo)
        res = ship.ship_branch(bid, requested_by="esteban",
                               client_cmd=stub_client["cmd"])
        rid = workflow_runs.start(temporal_workflow_id=res["workflow_id"],
                                  temporal_run_id="r1")
        workflow_runs.set_status(rid, "rolled-back")
        ship.sync_completion(bid)
        assert fb.get_branch(bid)["status"] == "rolled-back"
        return bid

    def test_rolled_back_refuses_on_old_approval(self, fb, catalog, repo,
                                                 ship, stub_client):
        bid = self._rolled_back(fb, catalog, repo, ship, stub_client)
        with pytest.raises(ship.ShipRefused) as exc:
            ship.ship_branch(bid, requested_by="esteban",
                             client_cmd=stub_client["cmd"])
        assert exc.value.reason == "rolled-back"

    def test_fresh_validation_alone_not_enough(self, fb, catalog, repo,
                                               ship, stub_client):
        """A new passing validation without a NEW approving review still
        refuses — the status is 'rolled-back', never silently approved."""
        bid = self._rolled_back(fb, catalog, repo, ship, stub_client)
        _validate_current(fb, bid)
        # record_validation does not clobber rolled-back... but assert the
        # gateway refuses regardless of what the status shows
        with pytest.raises(ship.ShipRefused) as exc:
            ship.ship_branch(bid, requested_by="esteban",
                             client_cmd=stub_client["cmd"])
        assert exc.value.reason in ("rolled-back", "not-approved")

    def test_fresh_validation_and_review_allow_reship(self, fb, catalog,
                                                      repo, ship,
                                                      stub_client):
        bid = self._rolled_back(fb, catalog, repo, ship, stub_client)
        # FRESH evidence at the current head: validation + approving review
        _validate_current(fb, bid)
        fb.record_review(bid, reviewer="esteban", decision="approve",
                         rationale="re-validated after rollback")
        res = ship.ship_branch(bid, requested_by="esteban",
                               client_cmd=stub_client["cmd"])
        assert fb.get_branch(bid)["status"] == "shipping"
        # the new ship row consumed the NEW review/validation
        row = fb.latest_ship(bid)
        assert row["id"] == res["ship_id"]
        first = fb.get_branch(bid)["reviews"][0]
        assert row["approval_review_id"] > first["id"]


class TestRepoIdentity:
    def test_origin_url_yields_org_slash_repo(self, fb, catalog, repo, ship,
                                              stub_client):
        _git(repo, "remote", "add", "origin",
             "https://github.com/acme/catalyrst-e2e.git")
        bid = _approved_branch(fb, catalog, repo)
        res = ship.ship_branch(bid, requested_by="esteban",
                               client_cmd=stub_client["cmd"])
        assert res["repo"] == "acme/catalyrst-e2e"
        argv = json.loads(stub_client["record"].read_text())
        assert argv[1] == "acme/catalyrst-e2e"

    def test_scp_like_origin_url(self, ship):
        assert ship._repo_name_from_origin_url(
            "git@github.com:acme/widgets.git") == "acme/widgets"
        assert ship._repo_name_from_origin_url(
            "ssh://git@github.com/acme/widgets.git") == "acme/widgets"
        assert ship._repo_name_from_origin_url("") is None

    def test_no_origin_falls_back_to_basename(self, ship, repo):
        assert ship.derive_repo_name(str(repo)) == repo.name

    def test_explicit_repo_name_overrides(self, fb, catalog, repo, ship,
                                          stub_client):
        _git(repo, "remote", "add", "origin",
             "https://github.com/acme/catalyrst-e2e.git")
        bid = _approved_branch(fb, catalog, repo)
        res = ship.ship_branch(bid, requested_by="esteban",
                               repo_name="override/name",
                               client_cmd=stub_client["cmd"])
        assert res["repo"] == "override/name"

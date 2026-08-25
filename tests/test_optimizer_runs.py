"""Tests for the optimizer-run registry: persistent state for async opt jobs.

The user judges a few cards, clicks "Optimize", and walks away. Later they
come back and want to see:
  - whether the optimization is still running
  - what the live log says (or said)
  - whether it succeeded or failed
  - if succeeded, what candidate prompt was published

Implementation: a small `optimizer_run` table in the fabric DB. Created when
the runner starts; updated on each log line and on exit.
"""
from __future__ import annotations
import sqlite3
import pytest

from bin import optimizer_runs


@pytest.fixture
def fresh_fabric(fake_langfuse, tmp_data_home, monkeypatch):
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    fake_langfuse.enable("create_prompt")
    import importlib, judgement
    importlib.reload(judgement)
    judgement.init_db()
    optimizer_runs.init()
    return tmp_data_home / "judgements.db"


def test_init_creates_optimizer_run_table(fresh_fabric):
    db = sqlite3.connect(str(fresh_fabric))
    cols = [r[1] for r in db.execute("PRAGMA table_info(optimizer_run)").fetchall()]
    assert "id" in cols
    assert "domain" in cols
    assert "status" in cols
    assert "log" in cols
    assert "started_at" in cols


def test_start_returns_id_and_creates_pending_row(fresh_fabric):
    run_id = optimizer_runs.start(domain="commit-msg", target="judge")
    assert isinstance(run_id, int)
    row = optimizer_runs.get(run_id)
    assert row["domain"] == "commit-msg"
    assert row["target"] == "judge"
    assert row["status"] == "running"
    assert row["log"] == ""


def test_append_log_accumulates(fresh_fabric):
    run_id = optimizer_runs.start(domain="d", target="judge")
    optimizer_runs.append_log(run_id, "line 1")
    optimizer_runs.append_log(run_id, "line 2")
    row = optimizer_runs.get(run_id)
    assert row["log"] == "line 1\nline 2\n"


def test_finish_marks_done_and_records_result(fresh_fabric):
    run_id = optimizer_runs.start(domain="d", target="judge")
    optimizer_runs.finish(run_id, status="done", result={"candidate_version": 3, "metric": 0.82})
    row = optimizer_runs.get(run_id)
    assert row["status"] == "done"
    assert row["exit_code"] == 0
    assert row["result"]["candidate_version"] == 3
    assert row["finished_at"] is not None


def test_finish_with_error_records_nonzero_exit(fresh_fabric):
    run_id = optimizer_runs.start(domain="d", target="judge")
    optimizer_runs.finish(run_id, status="error", exit_code=1, result={"error": "no training data"})
    row = optimizer_runs.get(run_id)
    assert row["status"] == "error"
    assert row["exit_code"] == 1


def test_list_for_domain_returns_newest_first(fresh_fabric):
    optimizer_runs.start(domain="d", target="judge")
    optimizer_runs.start(domain="d", target="generator")
    optimizer_runs.start(domain="other", target="judge")
    rows = optimizer_runs.list_for_domain("d")
    assert len(rows) == 2
    assert rows[0]["id"] > rows[1]["id"]


def test_latest_for_domain_target_returns_most_recent(fresh_fabric):
    r1 = optimizer_runs.start(domain="d", target="judge")
    optimizer_runs.finish(r1, status="done")
    r2 = optimizer_runs.start(domain="d", target="judge")
    latest = optimizer_runs.latest(domain="d", target="judge")
    assert latest["id"] == r2
    assert latest["status"] == "running"


def test_latest_returns_none_when_no_runs(fresh_fabric):
    assert optimizer_runs.latest(domain="never-run", target="judge") is None

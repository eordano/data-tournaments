"""Tests for bin/workflow_runs.py — the Temporal state projection
(ADR 0001 §4 step 6). Temporal stays authoritative; these rows are the
LiveView audit surface, written only by Activities."""
from __future__ import annotations

import pytest

from bin import workflow_runs as wr

@pytest.fixture
def runs(tmp_data_home):
    wr.init()
    return wr

def _start(runs, wfid="release:unity-explorer:abc123", runid="run-1", **kw):
    return runs.start(
        temporal_workflow_id=wfid, temporal_run_id=runid, **kw
    )

def test_start_creates_running_row(runs):
    rid = _start(runs)
    row = runs.get(rid)
    assert row["status"] == "running"
    assert row["temporal_workflow_id"] == "release:unity-explorer:abc123"
    assert row["stage_history"] == []
    assert row["finished_at"] is None

def test_start_is_idempotent_per_execution(runs):
    a = _start(runs)
    b = _start(runs)
    assert a == b
    assert len(runs.get_by_workflow_id("release:unity-explorer:abc123")) == 1

def test_new_temporal_run_id_mints_new_row(runs):
    a = _start(runs, runid="run-1")
    b = _start(runs, runid="run-2")
    assert a != b
    rows = runs.get_by_workflow_id("release:unity-explorer:abc123")
    assert len(rows) == 2
    assert rows[0]["id"] == b

def test_record_stage_appends_only(runs):
    rid = _start(runs)
    runs.record_stage(rid, stage="assemble_context", status="ok")
    runs.record_stage(rid, stage="judging_gate", status="ok", detail={"quorum": 3})
    hist = runs.get(rid)["stage_history"]
    assert [h["stage"] for h in hist] == ["assemble_context", "judging_gate"]
    assert hist[1]["detail"] == {"quorum": 3}
    assert all("at" in h for h in hist)

def test_record_stage_unknown_run_raises(runs):
    with pytest.raises(KeyError):
        runs.record_stage(999, stage="x", status="ok")

def test_set_status_terminal_stamps_finished_at(runs):
    rid = _start(runs)
    runs.set_status(rid, "awaiting-approval")
    assert runs.get(rid)["finished_at"] is None
    runs.set_status(rid, "rolled-back", detail={"reason": "approval timeout"})
    row = runs.get(rid)
    assert row["status"] == "rolled-back"
    assert row["finished_at"] is not None
    assert row["detail"]["reason"] == "approval timeout"

def test_terminal_status_is_sticky(runs):
    """A late-arriving projection update (retried activity) must not flip a
    terminal state back to running — mirrors judgement.py's done-flip guard."""
    rid = _start(runs)
    runs.set_status(rid, "done")
    runs.set_status(rid, "running")
    runs.set_status(rid, "failed")
    assert runs.get(rid)["status"] == "done"

def test_unknown_status_rejected(runs):
    rid = _start(runs)
    with pytest.raises(ValueError, match="unknown status"):
        runs.set_status(rid, "exploded")

def test_list_runs_filters_by_status(runs):
    a = _start(runs, wfid="release:r:1", runid="r1")
    b = _start(runs, wfid="release:r:2", runid="r2")
    runs.set_status(a, "done")
    assert [r["id"] for r in runs.list_runs(status="done")] == [a]
    assert {r["id"] for r in runs.list_runs()} == {a, b}

def test_rebuildable_disclaimer_is_documented():
    assert "rebuilt from" in wr.__doc__

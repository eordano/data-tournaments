"""Rating-write integrity under concurrent workers.

Two demonstrated gaps in bin/judgement.py, each pinned by a test here:

1. Duplicate-rating TOCTOU in ``write_judgement``: the "already resolved"
   check was a plain SELECT (no lock), and the final status flip was an
   unconditional UPDATE. Two workers that both pass the pre-check before
   either commits each insert their two score rows — four scores for one
   pending row, with the loser's rating_id overwriting the winner's.
   Guard: the status flip is a conditional
   ``UPDATE ... WHERE id=? AND status='pending'`` whose rowcount is checked
   inside the same transaction; a losing writer raises and its score INSERTs
   roll back.

2. Invalid done→error transition in ``run_llm_judge_for_pending``: when the
   race loser's ``write_judgement`` raised "already resolved", the broad
   except handler stomped the *completed* row back to status='error' with an
   unguarded UPDATE — leaving error status alongside a live rating_id and
   committed score rows.
   Guard: the error UPDATE only applies ``WHERE ... status='pending'``.

The interleavings are simulated deterministically (no sleeps, no real
threads-race): worker A is injected exactly inside worker B's race window —
after B's pre-check, before B's first write — which is precisely the
scheduling a second drain process/UI writer produces.
"""
from __future__ import annotations

import importlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

def _seed_one_pending(tmp_data_home) -> int:
    """One pending row against the seeded llm config; returns its id."""
    db = sqlite3.connect(str(tmp_data_home / "judgements.db"))
    cfg_id = db.execute(
        "SELECT id FROM job_configuration WHERE rater_type='llm' AND status='active'"
    ).fetchone()[0]
    pid = db.execute(
        "INSERT INTO pending_judgement(config_id, tournament_db_path, match_id, "
        "trace_payload) VALUES (?, ?, ?, ?)",
        (cfg_id, "/tmp/race.db", 1, json.dumps({
            "card_a": {"title": "A", "body": "a"},
            "card_b": {"title": "B", "body": "b"},
        })),
    ).lastrowid
    db.commit()
    db.close()
    return pid

@pytest.fixture
def judgement_mod(tmp_data_home, fake_langfuse, monkeypatch):
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    fake_langfuse.enable("create_prompt")
    fake_langfuse.enable("list_prompts")
    import judgement
    importlib.reload(judgement)
    judgement.init_db()
    return judgement

def test_concurrent_write_judgement_cannot_double_rate(
    judgement_mod, tmp_data_home, monkeypatch
):
    """Worker A resolves the row inside worker B's check→write window.

    B must lose: exactly one rating (2 score rows) survives, and the pending
    row keeps A's rating_id.
    """
    judgement = judgement_mod
    pid = _seed_one_pending(tmp_data_home)

    real_connect = judgement._connect
    state = {"armed": True, "rating_a": None}

    class InterceptingConn:
        """Delegating wrapper that fires worker A inside B's race window."""

        def __init__(self, conn):
            self._conn = conn

        def __enter__(self):
            self._conn.__enter__()
            return self

        def __exit__(self, *exc):
            return self._conn.__exit__(*exc)

        def execute(self, sql, *args):
            if state["armed"] and sql.lstrip().lower().startswith("insert into score"):
                state["armed"] = False
                state["rating_a"] = judgement.write_judgement(
                    pending_id=pid,
                    verdict="tie",
                    confidence="low",
                    rationale=None,
                    rater={"type": "llm", "model": "worker-a"},
                )
            return self._conn.execute(sql, *args)

        def commit(self):
            return self._conn.commit()

    def patched_connect(readonly: bool = False):
        conn = real_connect(readonly=readonly)
        return conn if readonly else InterceptingConn(conn)

    monkeypatch.setattr(judgement, "_connect", patched_connect)

    with pytest.raises(RuntimeError, match="already resolved"):
        judgement.write_judgement(
            pending_id=pid,
            verdict="a-wins",
            confidence="mid",
            rationale="worker-b duplicate",
            rater={"type": "llm", "model": "worker-b"},
        )

    assert state["rating_a"] is not None
    db = sqlite3.connect(str(tmp_data_home / "judgements.db"))
    db.row_factory = sqlite3.Row
    scores = db.execute(
        "SELECT rating_id, name, metadata FROM score WHERE pending_id=?", (pid,)
    ).fetchall()
    prow = db.execute(
        "SELECT status, rating_id FROM pending_judgement WHERE id=?", (pid,)
    ).fetchone()
    db.close()

    assert len(scores) == 2, f"duplicate rating written: {len(scores)} score rows"
    assert {s["rating_id"] for s in scores} == {state["rating_a"]}
    assert all(
        json.loads(s["metadata"])["rater"]["model"] == "worker-a" for s in scores
    )
    assert prow["status"] == "done"
    assert prow["rating_id"] == state["rating_a"]

def test_race_loser_cannot_flip_done_row_to_error(
    judgement_mod, tmp_data_home, monkeypatch
):
    """A completed row must never transition done→error.

    Simulates: worker B's LLM call is in flight when worker A completes the
    same pending row. B's write then fails ("already resolved"), and B's
    error handler must NOT stomp the done row to status='error'.
    """
    judgement = judgement_mod
    pid = _seed_one_pending(tmp_data_home)
    rating_a = {}

    class RacingJudge:
        def __init__(self, **_kwargs):
            pass

        def __call__(self, **_cards):
            rating_a["id"] = judgement.write_judgement(
                pending_id=pid,
                verdict="tie",
                confidence="low",
                rationale=None,
                rater={"type": "llm", "model": "worker-a"},
            )
            return SimpleNamespace(
                verdict="a-wins", confidence="mid", rationale="B's take",
            )

    monkeypatch.setattr("bin.judges.match_judge.MatchJudge", RacingJudge)
    monkeypatch.setattr(judgement, "_build_dspy_lm", lambda cfg: object())

    with pytest.raises(RuntimeError, match="already resolved"):
        judgement.run_llm_judge_for_pending(pid)

    db = sqlite3.connect(str(tmp_data_home / "judgements.db"))
    db.row_factory = sqlite3.Row
    prow = db.execute(
        "SELECT status, rating_id, error_message FROM pending_judgement WHERE id=?",
        (pid,),
    ).fetchone()
    n_scores = db.execute(
        "SELECT COUNT(*) FROM score WHERE pending_id=?", (pid,)
    ).fetchone()[0]
    db.close()

    assert prow["status"] == "done", (
        f"done row stomped to {prow['status']!r} by the race loser"
    )
    assert prow["rating_id"] == rating_a["id"]
    assert prow["error_message"] is None
    assert n_scores == 2

def test_error_status_still_recorded_for_genuine_failures(
    judgement_mod, tmp_data_home, monkeypatch
):
    """The guard must not break the legitimate pending→error transition."""
    judgement = judgement_mod
    pid = _seed_one_pending(tmp_data_home)

    class ExplodingJudge:
        def __init__(self, **_kwargs):
            pass

        def __call__(self, **_cards):
            raise TimeoutError("upstream LLM timed out")

    monkeypatch.setattr("bin.judges.match_judge.MatchJudge", ExplodingJudge)
    monkeypatch.setattr(judgement, "_build_dspy_lm", lambda cfg: object())

    with pytest.raises(TimeoutError):
        judgement.run_llm_judge_for_pending(pid)

    db = sqlite3.connect(str(tmp_data_home / "judgements.db"))
    db.row_factory = sqlite3.Row
    prow = db.execute(
        "SELECT status, error_message FROM pending_judgement WHERE id=?", (pid,)
    ).fetchone()
    db.close()
    assert prow["status"] == "error"
    assert "TimeoutError" in prow["error_message"]

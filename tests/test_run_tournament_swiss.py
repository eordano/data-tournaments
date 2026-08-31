"""bin/run-tournament.py runs Swiss, not single elimination.

One answer to "who plays whom" lives in bin/swiss.py; the orchestrator asks it.
The pool is the configured inputs from first round to last, a match the agent
will not call is a draw, no pair is ever asked twice, and the run ends with a
standings table instead of one conclusion.
"""
import importlib.util
import json
import math
import sqlite3
from pathlib import Path

import pytest

from bin import swiss

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "bin" / "run-tournament.py"


@pytest.fixture(scope="module")
def orchestrator():
    spec = importlib.util.spec_from_file_location("run_tournament_under_test",
                                                  SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tournament(tmp_path, orchestrator, monkeypatch):
    """A config over eight input files, with the agent and Langfuse stubbed."""
    inputs = []
    for index in range(8):
        path = tmp_path / f"input-{index}.md"
        path.write_text(f"# input {index}\n\nbody {index}\n")
        inputs.append(str(path))
    config = {
        "name": "swiss-under-test",
        "inputs": inputs,
        "seed": 20260101,
        "db_path": str(tmp_path / "t.db"),
        "workdir": str(tmp_path / "work"),
        "match_prompt": "compare {INPUTS} for {LABEL}",
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))

    monkeypatch.setattr(orchestrator, "maybe_init_langfuse", lambda _cfg: None)
    import judgement
    monkeypatch.setattr(judgement, "init_db", lambda: None)
    monkeypatch.setattr(judgement, "enqueue_for_match", lambda **_kw: [])
    return config_path, config


def _index_of(path):
    return int(Path(path).stem.split("-")[1])


def _stub_agent(calls, winner=None):
    """A deterministic stand-in for the harness: the higher-numbered input
    matters more, unless the caller wants every match undecided."""
    def run(cfg, db_path, round_n, row, workdir, trace_id, parent_obs_id):
        mid, slot, input_a, input_b, _is_bye = row[:5]
        calls.append((round_n, input_a, input_b))
        winner_id = winner
        if winner is None and _index_of(input_a) != _index_of(input_b):
            winner_id = 1 if _index_of(input_a) > _index_of(input_b) else 2
        return {
            "match_id": mid,
            "slot": slot,
            "is_bye": False,
            "synthesis": f"# R{round_n}-{slot + 1}\n\nsynthesis\n",
            "winner_id": winner_id,
            "outcome": (winner_id if isinstance(winner_id, str)
                        else _outcome(winner_id)),
            "winner_reasoning": "stub",
            "exit_code": 0,
            "stderr_tail": "",
        }
    return run


def _outcome(winner_id):
    return {1: "a", 2: "b"}.get(winner_id, "draw")


def _rows(db_path):
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT round, slot, input_a, input_b, is_bye, pair_key, outcome, "
        "       winner_id FROM matches ORDER BY round, slot"
    ).fetchall()
    db.close()
    return rows


def test_pairing_comes_from_bin_swiss(orchestrator):
    assert orchestrator.swiss is swiss
    assert not hasattr(orchestrator, "random_pair"), (
        "the single-elimination pairing is retired, not left alongside"
    )
    assert orchestrator.VERDICT_FOR_OUTCOME[swiss.OUTCOME_DRAW] in (
        swiss.known_verdicts()
    )


def test_rounds_run_over_a_stable_pool_and_never_repeat_a_pair(
    tournament, orchestrator, monkeypatch, capsys
):
    config_path, config = tournament
    calls = []
    monkeypatch.setattr(orchestrator, "run_match_subprocess", _stub_agent(calls))
    monkeypatch.setattr("sys.argv", ["run-tournament", str(config_path)])

    orchestrator.main()

    rounds = sorted({round_n for round_n, _a, _b in calls})
    assert rounds == [1, 2, 3] == list(range(1, math.ceil(math.log2(8)) + 1))
    assert len(calls) == 12, "eight items, four matches a round, three rounds"

    for round_n in rounds:
        played = {a for r, a, _b in calls if r == round_n}
        played |= {b for r, _a, b in calls if r == round_n}
        assert played == set(config["inputs"]), (
            f"round {round_n} must draw on the whole pool, not a shrinking one"
        )

    rows = _rows(config["db_path"])
    keys = [r["pair_key"] for r in rows if not r["is_bye"]]
    assert len(keys) == len(set(keys)) == 12
    pairs = {frozenset((r["input_a"], r["input_b"])) for r in rows}
    assert len(pairs) == 12
    assert all(r["outcome"] in ("a", "b") for r in rows)

    out = capsys.readouterr().out
    assert "STANDINGS" in out and "FINAL RESULT" not in out


def test_a_draw_is_recorded_without_inventing_a_winner(
    tournament, orchestrator, monkeypatch
):
    config_path, config = tournament
    calls = []
    monkeypatch.setattr(orchestrator, "run_match_subprocess",
                        _stub_agent(calls, winner=0))
    monkeypatch.setattr("sys.argv", ["run-tournament", str(config_path)])

    orchestrator.main()

    rows = [r for r in _rows(config["db_path"]) if not r["is_bye"]]
    assert all(r["outcome"] == "draw" for r in rows)
    assert all(r["winner_id"] in (0, None) for r in rows)

    db = sqlite3.connect(str(config["db_path"]))
    pool = orchestrator.build_pool(config, db)
    db.close()
    table = swiss.standings(pool)
    assert [s.points for s in table] == [3] * 8, (
        "three drawn rounds is three points each, and nobody is a winner"
    )
    assert all(s.draws == 3 and s.wins == 0 for s in table)


def test_a_resumed_run_reconstructs_standings_and_continues_at_the_round(
    tournament, orchestrator, monkeypatch
):
    config_path, config = tournament
    db = orchestrator.init_db(Path(config["db_path"]))
    pool = orchestrator.build_pool(config, db)
    orchestrator.insert_round(db, swiss.pair_round(pool, 1))
    first = db.execute(
        "SELECT id, input_a, input_b FROM matches WHERE round=1 ORDER BY slot"
    ).fetchall()
    winners = []
    for mid, input_a, input_b in first:
        winner = input_a if _index_of(input_a) > _index_of(input_b) else input_b
        winners.append(winner)
        db.execute(
            "UPDATE matches SET outcome=?, conclusion='done', synthesis='done', "
            "winner_id=? WHERE id=?",
            ("a" if winner == input_a else "b",
             1 if winner == input_a else 2, mid),
        )
    db.commit()

    assert orchestrator.resume_round(db) == 2
    reconstructed = swiss.standings(orchestrator.build_pool(config, db))
    assert {s.item_id for s in reconstructed[:4]} == set(winners)
    assert [s.points for s in reconstructed] == [3, 3, 3, 3, 0, 0, 0, 0]
    db.close()

    calls = []
    monkeypatch.setattr(orchestrator, "run_match_subprocess", _stub_agent(calls))
    monkeypatch.setattr("sys.argv", ["run-tournament", str(config_path)])
    orchestrator.main()

    assert sorted({round_n for round_n, _a, _b in calls}) == [2, 3], (
        "a resumed run picks up at the round it stopped in, re-asking nothing"
    )
    rows = _rows(config["db_path"])
    keys = [r["pair_key"] for r in rows if r["pair_key"]]
    assert len(keys) == len(set(keys))
    for round_n, input_a, input_b in calls:
        if round_n == 2:
            assert (input_a in winners) == (input_b in winners), (
                "round two pairs the reconstructed standings on similar points"
            )


def test_an_odd_pool_gives_one_bye_a_round_and_runs_no_agent_for_it(
    tmp_path, orchestrator, monkeypatch
):
    inputs = []
    for index in range(9):
        path = tmp_path / f"input-{index}.md"
        path.write_text(f"# input {index}\n")
        inputs.append(str(path))
    config = {
        "name": "odd-pool",
        "inputs": inputs,
        "seed": 7,
        "db_path": str(tmp_path / "odd.db"),
        "workdir": str(tmp_path / "work"),
        "match_prompt": "{LABEL}",
    }
    config_path = tmp_path / "odd.json"
    config_path.write_text(json.dumps(config))

    monkeypatch.setattr(orchestrator, "maybe_init_langfuse", lambda _cfg: None)
    import judgement
    monkeypatch.setattr(judgement, "init_db", lambda: None)
    monkeypatch.setattr(judgement, "enqueue_for_match", lambda **_kw: [])
    calls = []
    monkeypatch.setattr(orchestrator, "run_match_subprocess", _stub_agent(calls))
    monkeypatch.setattr("sys.argv", ["run-tournament", str(config_path)])

    orchestrator.main()

    rows = _rows(config["db_path"])
    for round_n in range(1, math.ceil(math.log2(9)) + 1):
        byes = [r for r in rows if r["round"] == round_n and r["is_bye"]]
        assert len(byes) == 1
        assert byes[0]["input_b"] is None
        assert byes[0]["input_a"] not in [
            call_input for r, a, b in calls if r == round_n
            for call_input in (a, b)
        ], "a bye runs no agent"

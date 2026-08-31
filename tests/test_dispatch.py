"""Tests for bin/dispatch.py — settled standing -> implementation queue.

Two layers, on purpose:

* pool-level, with a hand-built ``swiss.Pool`` and hand-recorded verdicts, so
  the ordering/skipping/routing rules are pinned against values computed by
  hand rather than by the code under test;
* fabric-level, with a real judgements.db, real pending rows resolved through
  ``judgement.write_judgement``, and a real throwaway git repo, so "dispatch
  authors a branch" means a commit exists in git.
"""
from __future__ import annotations

import json
import os
import random
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
        capture_output=True, text=True, env=_env(), check=True,
    )
    return proc.stdout.strip()


def _make_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)],
                   capture_output=True, env=_env(), check=True)
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "base")
    return repo


def _branches(repo: Path) -> list[str]:
    out = _git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    return [b for b in out.splitlines() if b]


def _stub_script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _payload(title: str, work_type: str = "bug-fix", priority: str = "P2") -> dict:
    """A work-order judge payload in the shape bin/generate_cards writes."""
    return {
        "kind": "work-order",
        "title": title,
        "body": f"# {title}",
        "source_ref": f"ref-{title}",
        "work_order": {
            "title": title,
            "goal": "goal",
            "plan": "plan",
            "work_type": work_type,
            "priority": priority,
        },
    }


def _pool(payloads, rubric="pair-wheel-v2", version=1):
    from bin import swiss

    items = [swiss.item_from_payload(p) for p in payloads]
    pool = swiss.new_pool(items, rubric_id=rubric, rubric_version=version)
    return pool, [i.id for i in items]


def _by_title(pool, title: str) -> str:
    for item_id, item in pool.items.items():
        if item.payload.get("title") == title:
            return item_id
    raise AssertionError(f"no item titled {title!r} in the pool")


class TestQueueOrdering:
    def test_order_is_the_points_table_not_the_self_assessed_priority(self):
        from bin import dispatch, swiss

        payloads = [
            _payload("a", priority="P0"),
            _payload("b", priority="P0"),
            _payload("c", priority="P3"),
            _payload("d", priority="P3"),
        ]
        pool, ids = _pool(payloads)
        a, b, c, d = ids
        swiss.record(pool, round=1, item_a=a, item_b=b, verdict="a-wins-big")
        swiss.record(pool, round=1, item_a=c, item_b=d, verdict="tie")
        swiss.record(pool, round=2, item_a=a, item_b=c, verdict="b-wins-big")
        swiss.record(pool, round=2, item_a=b, item_b=d, verdict="a-wins")

        entries = dispatch.queue_from_pool(pool, pool_id="unit")
        assert [e.points for e in entries] == [4, 3, 3, 1], (
            "hand-computed: c = 1 (draw) + 3 (win); a = 3 + 0; b = 0 + 3; "
            "d = 1 + 0"
        )
        assert [e.rank for e in entries] == [1, 2, 3, 4]
        assert entries[0].item_id == c
        assert entries[0].title == "c"
        assert entries[-1].title == "d"
        expected_tie = sorted([a, b])
        assert [e.item_id for e in entries[1:3]] == expected_tie

        priorities = [e.payload["work_order"]["priority"] for e in entries]
        assert priorities[0] == "P3", (
            "the item the comparisons put first is one the model scored P3; "
            "dispatching on the self-assessed field would have inverted this"
        )

    def test_an_unplayed_item_carries_no_position_and_is_skipped(self):
        from bin import dispatch, swiss

        pool, ids = _pool([_payload("a"), _payload("b"), _payload("late")])
        a, b, late = ids
        swiss.record(pool, round=1, item_a=a, item_b=b, verdict="a-wins-big")

        table = swiss.standings(pool)
        assert {s.item_id for s in table} == {a, b, late}
        assert [s.rank for s in table if s.item_id == late] == [0]

        entries = dispatch.queue_from_pool(pool)
        assert [e.item_id for e in entries] == [a, b]
        assert late not in [e.item_id for e in entries], (
            "an item that has played nothing has no standing to dispatch on"
        )

    def test_a_discarded_item_is_never_dispatched_even_after_winning(self):
        from bin import dispatch, swiss

        pool, ids = _pool([_payload("a"), _payload("b"), _payload("c")])
        a, b, c = ids
        swiss.record(pool, round=1, item_a=a, item_b=b, verdict="a-wins-big")
        swiss.record(pool, round=2, item_a=a, item_b=c, verdict="discard-a")

        assert a in pool.discarded and c not in pool.discarded, (
            "discard-a ejects A alone; C stays in the pool on its own merits"
        )
        assert any(r.outcome == swiss.OUTCOME_A for r in pool.results), (
            "the win it earned in round one is still in the pool's results"
        )
        entries = dispatch.queue_from_pool(pool)
        assert [e.item_id for e in entries] == [b], (
            "a discard leaves the pool: it does not score zero and it does "
            "not reach the implementation queue"
        )

    def test_min_played_below_one_is_refused(self):
        from bin import dispatch

        pool, _ids = _pool([_payload("a"), _payload("b")])
        with pytest.raises(ValueError, match="rank 0 is not a position"):
            dispatch.queue_from_pool(pool, min_played=0)

    def test_limit_takes_the_top_of_the_table(self):
        from bin import dispatch, swiss

        pool, ids = _pool([_payload(t) for t in "abcd"])
        a, b, c, d = ids
        swiss.record(pool, round=1, item_a=a, item_b=b, verdict="a-wins-big")
        swiss.record(pool, round=1, item_a=c, item_b=d, verdict="a-wins-big")
        entries = dispatch.queue_from_pool(pool, limit=2)
        assert [e.rank for e in entries] == [1, 2]
        assert {e.points for e in entries} == {3}

    def test_standing_carried_is_the_tournament_standing(self):
        from bin import dispatch, swiss

        pool, ids = _pool([_payload("a"), _payload("b"), _payload("c")])
        a, b, c = ids
        swiss.record(pool, round=1, item_a=a, item_b=b, verdict="a-wins-big")
        swiss.record(pool, round=2, item_a=a, item_b=c, verdict="tie")

        entry = dispatch.queue_from_pool(pool, pool_id="pool-7")[0]
        assert entry.item_id == a
        assert entry.standing.points == 4 and entry.standing.played == 2
        assert entry.standing.rank == 1
        assert entry.standing.rounds == 2, "three entrants play ceil(log2 3) rounds"
        assert entry.standing.pool_id == "pool-7"

        expected = {
            swiss.pair_key(pool.content(a), pool.content(b), "pair-wheel-v2", 1),
            swiss.pair_key(pool.content(a), pool.content(c), "pair-wheel-v2", 1),
        }
        assert set(entry.standing.pair_keys) == expected, (
            "the standing carries the same pair keys the no-rematch rule uses"
        )

    def test_dispatch_key_is_the_content_digest_not_a_row_id(self):
        from bin import dispatch, swiss

        payload = _payload("stable")
        pool_one, _ = _pool([payload, _payload("other")])
        pool_two, _ = _pool([_payload("other"), payload])
        key_one = dispatch.dispatch_key(pool_one.content(_by_title(pool_one, "stable")))
        key_two = dispatch.dispatch_key(pool_two.content(_by_title(pool_two, "stable")))
        assert key_one == key_two
        assert key_one == swiss.content_digest(
            json.dumps(payload, sort_keys=True, ensure_ascii=False)
        )


class TestWorkTypeRouting:
    @pytest.mark.parametrize(
        "work_type", ["bug-fix", "feature", "change-request", "refactor"]
    )
    def test_authorable_types_go_to_the_branch_author(self, work_type):
        from bin import dispatch

        assert dispatch.destination_for(work_type) == dispatch.DEST_BRANCH_AUTHOR

    @pytest.mark.parametrize("work_type", ["investigation", "epic-saga", "", None])
    def test_everything_else_goes_to_a_person(self, work_type):
        from bin import dispatch

        assert dispatch.destination_for(work_type) == dispatch.DEST_HUMAN_QUEUE

    def test_dispatch_fails_closed_where_the_author_fails_open(self):
        from bin import branch_author, dispatch

        assert branch_author.is_authorable(None) is True, (
            "the author fails OPEN for callers that carry no work type"
        )
        assert dispatch.destination_for(None) == dispatch.DEST_HUMAN_QUEUE, (
            "dispatch always knows the work type, so it fails CLOSED"
        )

    def test_work_type_is_read_off_the_work_order(self):
        from bin import dispatch

        assert dispatch.work_type_of(_payload("x", "investigation")) == "investigation"
        assert dispatch.work_type_of({"work_type": " Bug-Fix "}) == "bug-fix"
        assert dispatch.work_type_of({"kind": "card", "title": "t"}) == ""
        assert dispatch.work_type_of(None) == ""

    def test_an_investigation_at_the_top_is_routed_not_authored(self):
        from bin import dispatch, swiss

        pool, ids = _pool([
            _payload("dig", work_type="investigation"),
            _payload("fix", work_type="bug-fix"),
        ])
        dig, fix = ids
        swiss.record(pool, round=1, item_a=dig, item_b=fix,
                     verdict="a-wins-big")
        entries = dispatch.queue_from_pool(pool)
        assert entries[0].item_id == dig and entries[0].rank == 1
        assert entries[0].destination == dispatch.DEST_HUMAN_QUEUE
        assert entries[1].destination == dispatch.DEST_BRANCH_AUTHOR


@pytest.fixture
def fabric(fake_langfuse, monkeypatch, tmp_data_home):
    monkeypatch.setattr("bin.prompts._client_factory",
                        lambda: fake_langfuse.as_client())
    for hook in ("create_prompt", "list_prompts", "set_label"):
        fake_langfuse.enable(hook)
    import importlib
    import judgement

    importlib.reload(judgement)
    judgement.init_db()
    from bin import dispatch

    dispatch.init()
    return tmp_data_home / "judgements.db"


@pytest.fixture
def repo(tmp_path) -> Path:
    return _make_repo(tmp_path)


def _domain(payloads, name="dispatch-domain"):
    from bin import domains, prompts

    prompts.push(f"card-generator:{name}", "Generate work orders.",
                 labels=["production"])
    prompts.push(f"judge-instructions:{name}", "Judge work orders.",
                 labels=["production"])
    domain_id = domains.create_domain(
        name=name,
        description="work orders for the priority tournament",
        corpus_source={"kind": "inline", "items": [{"text": "x"}]},
        rubric="pair-wheel-v2",
    )
    from bin import generate_cards

    generate_cards._enqueue_pairs(domain_id, payloads, random.Random(3))
    return domain_id, name


def _pending(db_path):
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, trace_payload FROM pending_judgement "
        "WHERE status='pending' ORDER BY match_id"
    ).fetchall()
    db.close()
    return rows


def _judge(db_path, losers=()):
    """Resolve every open pair; any title in ``losers`` loses its match."""
    import judgement

    for row in _pending(db_path):
        payload = json.loads(row["trace_payload"])
        title_a = payload["card_a"].get("title")
        verdict = "b-wins-big" if title_a in losers else "a-wins-big"
        judgement.write_judgement(
            pending_id=row["id"], verdict=verdict, confidence="mid",
            rater={"type": "human", "userId": "tester"},
        )


FIXTURE_FILES = {"files": {"fix.py": "FIXED = True\n"}, "label": "unit-fix"}


def _per_item_config(entry):
    return {
        "files": {f"fix-{entry.key[:8]}.py": f"{entry.title}\n"},
        "label": entry.title,
    }


class TestDispatchDomain:
    def test_authors_every_settled_item_in_standings_order(self, fabric, repo):
        from bin import dispatch

        domain_id, name = _domain([
            _payload("loser", priority="P0"),
            _payload("w1", priority="P3"),
            _payload("w2", priority="P3"),
            _payload("l2", priority="P3"),
        ])
        _judge(fabric, losers=("loser",))

        records = dispatch.dispatch_domain(
            name, repo_path=str(repo), base_ref="main", backend="fixture",
            backend_config_for=_per_item_config,
        )
        assert [r.outcome for r in records] == [dispatch.OUTCOME_AUTHORED] * 4
        assert [r.rank for r in records] == [1, 2, 3, 4]
        assert all(r.work_type == "bug-fix" for r in records)
        assert all(r.destination == dispatch.DEST_BRANCH_AUTHOR for r in records)

        titles = [r.title for r in records]
        assert titles.index("loser") > 0, (
            "the item the judge put down ranks below the ones it lost to, "
            "however the model scored it"
        )

        base = _git(repo, "rev-parse", "main")
        for record in records:
            head = _git(repo, "rev-parse", record.branch_name)
            assert _git(repo, "rev-parse", f"{head}^") == base
            files = _git(repo, "show", "--name-only", "--format=", head)
            assert files == f"fix-{record.key[:8]}.py"

        ledger = dispatch.dispatched(name)
        assert len(ledger) == 4
        assert [r["standing_rank"] for r in ledger] == [1, 2, 3, 4]
        points = [r["points"] for r in ledger]
        assert points == sorted(points, reverse=True)
        assert [r["workorder_ref"] for r in ledger] == [
            r.item_id for r in records
        ], "the ledger names the ITEM, not the domain every item shares"
        assert len({r["workorder_ref"] for r in ledger}) == 4
        assert all(r["destination"] == "branch-author" for r in ledger)
        assert all(r["fix_branch_id"] for r in ledger)
        assert all(r["outcome"] == "authored" for r in ledger)

        from bin import branch_author

        for record in records:
            rows = branch_author.get_authoring(record.fix_branch_id)
            assert len(rows) == 1 and rows[0]["workorder_ref"] == record.item_id

    def test_dispatching_twice_does_not_author_twice(self, fabric, repo):
        from bin import dispatch

        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        first = dispatch.dispatch_domain(
            "dispatch-domain", repo_path=str(repo), base_ref="main",
            backend="fixture", backend_config_for=_per_item_config,
        )
        assert [r.outcome for r in first] == [dispatch.OUTCOME_AUTHORED] * 2
        branches = sorted(_branches(repo))
        authoring = sqlite3.connect(str(fabric)).execute(
            "SELECT COUNT(*) FROM branch_authoring"
        ).fetchone()[0]

        second = dispatch.dispatch_domain(
            "dispatch-domain", repo_path=str(repo), base_ref="main",
            backend="fixture", backend_config_for=_per_item_config,
        )
        assert [r.outcome for r in second] == [
            dispatch.OUTCOME_ALREADY_DISPATCHED] * 2
        assert [r.dispatch_id for r in second] == [None, None]
        assert sorted(_branches(repo)) == branches, "no second branch"
        assert sqlite3.connect(str(fabric)).execute(
            "SELECT COUNT(*) FROM branch_authoring"
        ).fetchone()[0] == authoring
        assert len(dispatch.dispatched("dispatch-domain")) == 2

        keys = {r.key for r in first}
        assert dispatch.claimed_keys(
            dispatch._resolve_domain("dispatch-domain")[0]
        ) == keys

    def test_the_claim_key_is_enforced_by_the_database_too(self, fabric, repo):
        from bin import dispatch

        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        records = dispatch.dispatch_domain(
            "dispatch-domain", repo_path=str(repo), base_ref="main",
            backend="fixture", backend_config_for=_per_item_config,
        )
        domain_id = dispatch._resolve_domain("dispatch-domain")[0]
        conn = sqlite3.connect(str(fabric))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO work_dispatch(domain_id, dispatch_key, destination, "
                "outcome) VALUES (?, ?, 'branch-author', 'authored')",
                (domain_id, records[0].key),
            )
        conn.close()

    def test_the_ledger_is_append_only(self, fabric, repo):
        from bin import dispatch

        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        dispatch.dispatch_domain(
            "dispatch-domain", repo_path=str(repo), base_ref="main",
            backend="fixture", backend_config_for=_per_item_config,
        )
        conn = sqlite3.connect(str(fabric))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE work_dispatch SET outcome='failed'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM work_dispatch")
        conn.close()

    def test_a_non_authorable_type_reaches_a_person_and_blocks_nobody(
        self, fabric, repo
    ):
        from bin import branch_author, dispatch

        _domain([
            _payload("dig", work_type="investigation"),
            _payload("fix", work_type="bug-fix"),
            {"kind": "work-order", "title": "bare", "body": "no work order"},
            _payload("feat", work_type="feature"),
        ])
        _judge(fabric)
        records = dispatch.dispatch_domain(
            "dispatch-domain", repo_path=str(repo), base_ref="main",
            backend="fixture", backend_config_for=_per_item_config,
        )
        by_title = {r.title: r for r in records}
        assert len(records) == 4

        assert by_title["dig"].outcome == dispatch.OUTCOME_ROUTED_TO_HUMAN
        assert by_title["dig"].destination == dispatch.DEST_HUMAN_QUEUE
        assert by_title["dig"].branch_name == ""
        assert by_title["bare"].outcome == dispatch.OUTCOME_ROUTED_TO_HUMAN
        assert by_title["bare"].work_type == ""
        assert by_title["fix"].outcome == dispatch.OUTCOME_AUTHORED
        assert by_title["feat"].outcome == dispatch.OUTCOME_AUTHORED

        assert sorted(_branches(repo)) == sorted(
            ["main", by_title["fix"].branch_name, by_title["feat"].branch_name]
        ), "an investigation must not silently become an empty commit"

        refusals = branch_author.refusals()
        assert sorted(r["work_type"] for r in refusals) == [
            "(undeclared)", "investigation"
        ]
        assert all(r["disposition"] == "route-to-human" for r in refusals)
        assert by_title["dig"].refusal_id in [r["id"] for r in refusals]
        assert branch_author.refusals(
            workorder_ref=by_title["dig"].item_id
        ), "a refusal names the item a person has to pick up, not its domain"

    def test_an_authoring_failure_does_not_block_the_rest_and_is_retryable(
        self, fabric, repo, tmp_path
    ):
        from bin import dispatch

        stub = _stub_script(tmp_path / "author.sh", (
            'if [ "$WORKORDER_TITLE" = "poison" ]; then exit 7; fi\n'
            'printf "%s\\n" "$WORKORDER_WORK_TYPE rank $WORKORDER_RANK" '
            '> authored.txt\n'
        ))
        _domain([_payload("poison"), _payload("clean")])
        _judge(fabric, losers=("clean",))

        records = dispatch.dispatch_domain(
            "dispatch-domain", repo_path=str(repo), base_ref="main",
            backend="command", backend_config={"argv": [str(stub)]},
        )
        by_title = {r.title: r for r in records}
        assert by_title["poison"].rank == 1
        assert by_title["poison"].outcome == dispatch.OUTCOME_FAILED
        assert "exited 7" in by_title["poison"].detail
        assert by_title["clean"].outcome == dispatch.OUTCOME_AUTHORED
        assert by_title["poison"].branch_name not in _branches(repo)
        assert by_title["clean"].branch_name in _branches(repo)

        domain_id = dispatch._resolve_domain("dispatch-domain")[0]
        assert by_title["poison"].key not in dispatch.claimed_keys(domain_id), (
            "nothing was authored, so the failed item is retryable"
        )
        again = dispatch.dispatch_domain(
            "dispatch-domain", repo_path=str(repo), base_ref="main",
            backend="command", backend_config={"argv": [str(stub)]},
        )
        outcomes = {r.title: r.outcome for r in again}
        assert outcomes["poison"] == dispatch.OUTCOME_FAILED
        assert outcomes["clean"] == dispatch.OUTCOME_ALREADY_DISPATCHED
        assert len(dispatch.dispatched("dispatch-domain")) == 3

    def test_the_authored_branch_carries_the_work_type_and_the_standing(
        self, fabric, repo, tmp_path
    ):
        from bin import dispatch

        stub = _stub_script(tmp_path / "author.sh", (
            'printf "%s\\n" "$WORKORDER_WORK_TYPE rank $WORKORDER_RANK '
            'points $WORKORDER_POINTS domain $WORKORDER_DOMAIN" > authored.txt\n'
        ))
        _domain([_payload("top", work_type="refactor"), _payload("bottom")])
        _judge(fabric)

        records = dispatch.dispatch_domain(
            "dispatch-domain", repo_path=str(repo), base_ref="main",
            backend="command", backend_config={"argv": [str(stub)]},
        )
        top = [r for r in records if r.rank == 1][0]
        content = _git(repo, "show", f"{top.branch_name}:authored.txt")
        assert content == (
            f"{top.work_type} rank 1 points 3 domain dispatch-domain"
        ), "the backend saw the work type dispatch routed on, not a guess"

    def test_an_authorable_item_without_a_repo_is_refused_loudly(self, fabric):
        from bin import dispatch

        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        with pytest.raises(ValueError, match="no repo_path"):
            dispatch.dispatch_domain("dispatch-domain")
        assert dispatch.dispatched("dispatch-domain") == []

    def test_an_unjudged_domain_dispatches_nothing(self, fabric, repo):
        from bin import dispatch

        _domain([_payload("a"), _payload("b")])
        assert dispatch.dispatch_domain(
            "dispatch-domain", repo_path=str(repo), base_ref="main",
            backend="fixture", backend_config=FIXTURE_FILES,
        ) == []
        assert _branches(repo) == ["main"]

    def test_a_discarded_winner_is_not_dispatched_from_the_fabric(
        self, fabric, repo
    ):
        import judgement
        from bin import dispatch

        _domain([_payload("a"), _payload("b"), _payload("c"), _payload("d")])
        rows = _pending(fabric)
        first = json.loads(rows[0]["trace_payload"])
        judgement.write_judgement(
            pending_id=rows[0]["id"], verdict="discard-a", confidence="mid",
            rater={"type": "human", "userId": "tester"},
        )
        judgement.write_judgement(
            pending_id=rows[1]["id"], verdict="a-wins-big",
            confidence="mid", rater={"type": "human", "userId": "tester"},
        )
        records = dispatch.dispatch_domain(
            "dispatch-domain", repo_path=str(repo), base_ref="main",
            backend="fixture", backend_config_for=_per_item_config,
        )
        dispatched_titles = {r.title for r in records}
        assert first["card_a"]["title"] not in dispatched_titles
        assert first["card_b"]["title"] not in dispatched_titles
        assert dispatched_titles == {
            json.loads(rows[1]["trace_payload"])["card_a"]["title"],
            json.loads(rows[1]["trace_payload"])["card_b"]["title"],
        }

    def test_queue_reads_the_fabric_standings(self, fabric):
        from bin import dispatch

        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        entries = dispatch.queue("dispatch-domain")
        assert [e.rank for e in entries] == [1, 2]
        assert [e.points for e in entries] == [3, 0]
        domain_id = dispatch._resolve_domain("dispatch-domain")[0]
        import judgement

        assert entries[0].standing.pool_id == (
            f"domain:{domain_id}:pair-wheel-v2:"
            f"v{judgement.PAIR_WHEEL_TEMPLATE_VERSION}"
        )

    def test_unknown_domain_is_a_lookup_error(self, fabric):
        from bin import dispatch

        with pytest.raises(LookupError, match="no domain"):
            dispatch.queue("nope")


class TestCli:
    def test_queue_command_prints_the_table(self, fabric, capsys):
        from bin import dispatch

        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        assert dispatch.main(["queue", "--domain", "dispatch-domain"]) == 0
        rows = json.loads(capsys.readouterr().out)
        assert [r["rank"] for r in rows] == [1, 2]
        assert rows[0]["destination"] == "branch-author"

    def test_run_command_authors_and_logs(self, fabric, repo, capsys):
        from bin import dispatch

        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        config = repo.parent / "backend.json"
        config.write_text(json.dumps(FIXTURE_FILES))
        code = dispatch.main([
            "run", "--domain", "dispatch-domain", "--repo", str(repo),
            "--base-ref", "main", "--backend", "fixture",
            "--backend-config", str(config),
        ])
        assert code == 0
        rows = json.loads(capsys.readouterr().out)
        assert [r["outcome"] for r in rows] == ["authored", "authored"]

        assert dispatch.main(["log", "--domain", "dispatch-domain"]) == 0
        logged = json.loads(capsys.readouterr().out)
        assert len(logged) == 2

    def test_unknown_domain_exits_nonzero(self, fabric, capsys):
        from bin import dispatch

        assert dispatch.main(["queue", "--domain", "nope"]) == 1
        assert "no domain" in capsys.readouterr().err



def _harness_repo(root: Path) -> Path:
    """A repo whose main carries the red/green scripts a validation runs."""
    repo = _make_repo(root)
    _stub_script(repo / "red.sh", "echo 'RED 1/1'\n")
    _stub_script(repo / "green.sh", "echo 'GREEN 2/2'\n")
    _git(repo, "add", "red.sh", "green.sh")
    _git(repo, "commit", "-m", "harness")
    return repo


class TestOnlyDecidedMatchesEarnAPosition:
    def test_a_skip_only_item_has_no_position_and_is_not_dispatched(self):
        from bin import dispatch, swiss

        pool, ids = _pool([_payload(t) for t in "abc"])
        a, b, c = ids
        swiss.record(pool, round=1, item_a=a, item_b=b, verdict="a-wins-big")
        swiss.record(pool, round=2, item_a=c, item_b=a,
                     verdict="a-lean-both-invalid",
                     default_outcome=swiss.OUTCOME_SKIP)

        table = {row.item_id: row for row in swiss.standings(pool)}
        assert (table[c].played, table[c].points, table[c].rank) == (0, 0, 0), (
            swiss.A_SKIP_MUST_NOT_AWARD_A_RANK_SO_IT_TAKES_THE_SAME_PATH_A_BYE_TAKES
        )
        assert dispatch.decided_matches(table[c]) == 0

        entries = dispatch.queue_from_pool(pool)
        assert c not in [e.item_id for e in entries], (
            "an item whose only comparison was skipped established nothing "
            "and must not be dispatched on a rank it got for free"
        )
        assert sorted(e.item_id for e in entries) == sorted([a, b])

    def test_a_decided_match_beside_a_skip_still_counts(self):
        from bin import dispatch, swiss

        pool, ids = _pool([_payload(t) for t in "abc"])
        a, b, c = ids
        swiss.record(pool, round=1, item_a=a, item_b=c, verdict="a-wins")
        swiss.record(pool, round=2, item_a=c, item_b=b, verdict="nonsense",
                     default_outcome=swiss.OUTCOME_SKIP)

        entry = [e for e in dispatch.queue_from_pool(pool) if e.item_id == c][0]
        assert entry.played == 1, (
            "the decided pairing is the only one the standing reports; the "
            "skipped one established nothing about either side"
        )
        assert entry.points == 0


class TestRubricSelection:
    def test_a_rubric_that_matches_nothing_is_refused(self, fabric):
        from bin import dispatch

        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        assert dispatch.queue("dispatch-domain", rubric="pair-wheel-v2")
        with pytest.raises(ValueError, match="matches no active human"):
            dispatch.queue("dispatch-domain", rubric="no-such-rubric")

    def test_the_arbitrary_fallback_never_authors(self, fabric, repo):
        from bin import dispatch

        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        with pytest.raises(ValueError, match="ordering nobody asked for"):
            dispatch.dispatch_domain(
                "dispatch-domain", repo_path=str(repo), base_ref="main",
                backend="fixture", backend_config=FIXTURE_FILES,
                rubric="no-such-rubric",
            )
        assert _branches(repo) == ["main"]
        assert dispatch.dispatched("dispatch-domain") == []


class TestRepoIsValidatedBeforeAnythingIsClaimed:
    def test_a_missing_repo_claims_nothing_at_all(self, fabric):
        from bin import branch_author, dispatch

        _domain([
            _payload("dig", work_type="investigation"),
            _payload("fix", work_type="bug-fix"),
        ])
        _judge(fabric)
        with pytest.raises(ValueError, match="got no repo_path"):
            dispatch.dispatch_domain("dispatch-domain")
        assert dispatch.dispatched("dispatch-domain") == [], (
            "the human-destined item ahead of the authorable one must not be "
            "claimed by a run that cannot finish"
        )
        assert branch_author.refusals() == []

    def test_the_refusal_names_the_authorable_items(self, fabric):
        from bin import dispatch

        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        with pytest.raises(ValueError) as excinfo:
            dispatch.dispatch_domain("dispatch-domain")
        message = str(excinfo.value)
        assert "2 of 2 queued items are authorable" in message
        assert "min_played" not in message and "limit" not in message, (
            "neither flag removes an authorable item from the top of the "
            "table, so neither is a remedy"
        )

    def test_a_repo_that_is_not_a_repo_claims_nothing(self, fabric, tmp_path):
        from bin import dispatch

        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        empty = tmp_path / "not-a-repo"
        empty.mkdir()
        with pytest.raises(ValueError, match="cannot author from"):
            dispatch.dispatch_domain(
                "dispatch-domain", repo_path=str(empty), base_ref="main",
                backend="fixture", backend_config=FIXTURE_FILES,
            )
        assert dispatch.dispatched("dispatch-domain") == []

    def test_a_base_ref_that_names_no_commit_claims_nothing(
        self, fabric, repo
    ):
        from bin import dispatch

        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        with pytest.raises(ValueError, match="cannot author from 'no-such-ref'"):
            dispatch.dispatch_domain(
                "dispatch-domain", repo_path=str(repo), base_ref="no-such-ref",
                backend="fixture", backend_config=FIXTURE_FILES,
            )
        assert dispatch.dispatched("dispatch-domain") == [], (
            "a base ref that resolves nowhere fails on EVERY item, so it is "
            "refused before the first one is claimed"
        )

    def test_a_domain_with_only_human_items_needs_no_repo(self, fabric):
        from bin import dispatch

        _domain([
            _payload("dig", work_type="investigation"),
            _payload("dig2", work_type="investigation"),
        ])
        _judge(fabric)
        records = dispatch.dispatch_domain("dispatch-domain")
        assert [r.outcome for r in records] == [
            dispatch.OUTCOME_ROUTED_TO_HUMAN] * 2


class TestTheClaimIsTakenBeforeTheBackendRuns:
    def test_an_interrupted_beat_leaves_a_claim_naming_its_branch(
        self, fabric, repo, monkeypatch
    ):
        from bin import branch_author, dispatch

        _domain([_payload("a"), _payload("b")])
        _judge(fabric)

        def _killed(*args, **kwargs):
            raise KeyboardInterrupt("kill -9 between the commit and the row")

        monkeypatch.setattr(branch_author, "author_branch", _killed)
        with pytest.raises(KeyboardInterrupt):
            dispatch.dispatch_domain(
                "dispatch-domain", repo_path=str(repo), base_ref="main",
                backend="fixture", backend_config=FIXTURE_FILES,
            )

        ledger = dispatch.dispatched("dispatch-domain")
        assert [r["outcome"] for r in ledger] == ["claimed"]
        assert ledger[0]["branch_name"].startswith("dispatch/d"), (
            "the ledger names the branch the killed run was about to author, "
            "so a real branch can never exist with nothing recorded about it"
        )
        assert ledger[0]["pool_id"] and ledger[0]["pair_keys"]

    def test_the_retry_reports_the_unresolved_claim(self, fabric, repo):
        from bin import branch_author, dispatch

        _domain([_payload("a"), _payload("b")])
        _judge(fabric)

        def _killed(*args, **kwargs):
            raise SystemExit(1)

        original = branch_author.author_branch
        branch_author.author_branch = _killed
        try:
            with pytest.raises(SystemExit):
                dispatch.dispatch_domain(
                    "dispatch-domain", repo_path=str(repo), base_ref="main",
                    backend="fixture", backend_config=FIXTURE_FILES,
                )
        finally:
            branch_author.author_branch = original

        again = dispatch.dispatch_domain(
            "dispatch-domain", repo_path=str(repo), base_ref="main",
            backend="fixture", backend_config_for=_per_item_config,
        )
        by_rank = {r.rank: r for r in again}
        assert by_rank[1].outcome == dispatch.OUTCOME_ALREADY_DISPATCHED
        assert "UNRESOLVED claim" in by_rank[1].detail
        assert by_rank[2].outcome == dispatch.OUTCOME_AUTHORED, (
            "one stuck item never blocks the rest of the table"
        )

    def test_a_second_claim_on_one_key_is_reported_not_raised(
        self, fabric, repo
    ):
        from bin import dispatch

        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        dispatch.init()
        domain_id = dispatch._resolve_domain("dispatch-domain")[0]
        entry = dispatch.queue("dispatch-domain")[0]

        first = dispatch._claim(domain_id, entry, rubric=None,
                                branch_name="dispatch/x", workorder_ref=entry.item_id)
        assert first.status == dispatch.OUTCOME_CLAIMED
        second = dispatch._claim(domain_id, entry, rubric=None,
                                 branch_name="dispatch/x",
                                 workorder_ref=entry.item_id)
        assert second.status == dispatch.OUTCOME_ALREADY_DISPATCHED, (
            "the UNIQUE claim index is the race guard, and losing that race "
            "is a documented outcome, not an IntegrityError out of the "
            "middle of the table"
        )
        assert second.dispatch_id is None
        assert len(dispatch.dispatched("dispatch-domain")) == 1

    def test_a_failed_beat_releases_the_claim(self, fabric, repo, tmp_path):
        from bin import dispatch

        stub = _stub_script(tmp_path / "boom.sh", "exit 9\n")
        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        records = dispatch.dispatch_domain(
            "dispatch-domain", repo_path=str(repo), base_ref="main",
            backend="command", backend_config={"argv": [str(stub)]},
        )
        assert {r.outcome for r in records} == {dispatch.OUTCOME_FAILED}
        domain_id = dispatch._resolve_domain("dispatch-domain")[0]
        assert dispatch.claimed_keys(domain_id) == set()
        assert [r["outcome"] for r in dispatch.dispatched("dispatch-domain")] == [
            "failed", "failed"
        ]


class TestTheQueueIsRereadInsideTheLoop:
    def test_an_item_discarded_mid_run_is_never_authored(self, fabric, repo):
        import judgement
        from bin import dispatch, generate_cards

        _domain([_payload(t) for t in "abcd"])
        _judge(fabric)
        assert generate_cards.advance_round(
            dispatch._resolve_domain("dispatch-domain")[0]
        )["status"] == "drawn"
        open_rows = _pending(fabric)
        assert open_rows, "round two is on the queue and unjudged"

        first_seen: list[str] = []
        ejected: dict[str, str] = {}

        def _config_and_eject(entry):
            first_seen.append(entry.title)
            if len(first_seen) == 1:
                for row in open_rows:
                    payload = json.loads(row["trace_payload"])
                    if payload["card_a"]["title"] != entry.title:
                        judgement.write_judgement(
                            pending_id=row["id"], verdict="discard-a",
                            confidence="mid",
                            rater={"type": "human", "userId": "tester"},
                        )
                        ejected["title"] = payload["card_a"]["title"]
                        break
            return _per_item_config(entry)

        records = dispatch.dispatch_domain(
            "dispatch-domain", repo_path=str(repo), base_ref="main",
            backend="fixture", backend_config_for=_config_and_eject,
        )
        assert ejected, "the mid-run discard was written"
        dropped = [r for r in records if r.title == ejected["title"]]
        assert [r.outcome for r in dropped] == [
            dispatch.OUTCOME_DISCARDED_SINCE_QUEUED
        ], "the queue snapshot is stale by the time the loop reaches it"
        assert dropped[0].branch_name == ""
        assert "discard-a" in dropped[0].detail
        assert not [
            b for b in _branches(repo)
            if b.endswith(dropped[0].key[:12])
        ]
        assert ejected["title"] not in [
            r["item_id"] for r in dispatch.dispatched("dispatch-domain")
        ]


class TestTheLedgerStateMachine:
    def test_a_claim_resolves_once_and_never_moves_again(self, fabric, repo):
        from bin import dispatch

        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        records = dispatch.dispatch_domain(
            "dispatch-domain", repo_path=str(repo), base_ref="main",
            backend="fixture", backend_config_for=_per_item_config,
        )
        dispatch_id = records[0].dispatch_id
        with pytest.raises(RuntimeError, match="not an open claim"):
            dispatch._resolve(dispatch_id, outcome=dispatch.OUTCOME_FAILED)

    def test_the_claimed_standing_cannot_be_rewritten(self, fabric, repo):
        from bin import dispatch

        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        dispatch.dispatch_domain(
            "dispatch-domain", repo_path=str(repo), base_ref="main",
            backend="fixture", backend_config_for=_per_item_config,
        )
        conn = sqlite3.connect(str(fabric))
        for column, value in (("standing_rank", 99), ("pool_id", "elsewhere"),
                              ("item_id", "somebody-else"), ("points", 0)):
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                conn.execute(f"UPDATE work_dispatch SET {column}=?", (value,))
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE work_dispatch SET outcome='failed'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM work_dispatch")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM work_dispatch_pair")
        conn.close()


class TestALedgerWrittenBeforeTheReturnEdge:
    OLD_SHAPE = """
    CREATE TABLE work_dispatch (
      id             INTEGER PRIMARY KEY AUTOINCREMENT,
      domain_id      INTEGER NOT NULL,
      dispatch_key   TEXT NOT NULL,
      item_id        TEXT NOT NULL DEFAULT '',
      standing_rank  INTEGER NOT NULL DEFAULT 0,
      points         INTEGER NOT NULL DEFAULT 0,
      played         INTEGER NOT NULL DEFAULT 0,
      work_type      TEXT NOT NULL DEFAULT '',
      destination    TEXT NOT NULL,
      outcome        TEXT NOT NULL,
      workorder_ref  TEXT NOT NULL DEFAULT '',
      branch_name    TEXT NOT NULL DEFAULT '',
      fix_branch_id  INTEGER,
      authoring_id   INTEGER,
      refusal_id     INTEGER,
      detail         TEXT NOT NULL DEFAULT '',
      created_at     TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TRIGGER work_dispatch_immutable
      BEFORE UPDATE ON work_dispatch
      BEGIN SELECT RAISE(ABORT, 'work_dispatch rows are immutable'); END;
    """

    def test_the_old_columns_are_added_and_the_old_trigger_is_dropped(
        self, fabric, repo
    ):
        from bin import dispatch

        conn = sqlite3.connect(str(fabric))
        conn.execute("DROP TABLE work_dispatch")
        conn.executescript(self.OLD_SHAPE)
        conn.execute(
            "INSERT INTO work_dispatch(domain_id, dispatch_key, item_id, "
            "destination, outcome) "
            "VALUES (1, 'old-key', 'old-item', 'branch-author', 'authored')"
        )
        conn.commit()
        conn.close()

        dispatch.init()
        old = [r for r in dispatch.dispatched() if r["item_id"] == "old-item"]
        assert old and old[0]["pool_id"] == "" and old[0]["rounds"] == 0
        conn = sqlite3.connect(str(fabric))
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            ).fetchall()
        }
        conn.close()
        assert "work_dispatch_immutable" not in names, (
            "the blanket immutability rule is DELETED, not layered under the "
            "claim state machine: it would abort every claim resolution"
        )
        assert "work_dispatch_only_a_claim_resolves_once" in names

        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        records = dispatch.dispatch_domain(
            "dispatch-domain", repo_path=str(repo), base_ref="main",
            backend="fixture", backend_config_for=_per_item_config,
        )
        assert [r.outcome for r in records] == [dispatch.OUTCOME_AUTHORED] * 2


class TestReturnEdgeCloses:
    def test_the_ledger_carries_the_pool_and_the_pairs(self, fabric, repo):
        from bin import dispatch

        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        entries = {e.item_id: e for e in dispatch.queue("dispatch-domain")}
        records = dispatch.dispatch_domain(
            "dispatch-domain", repo_path=str(repo), base_ref="main",
            backend="fixture", backend_config_for=_per_item_config,
        )
        for record in records:
            entry = entries[record.item_id]
            standing = dispatch.standing_for_branch(record.fix_branch_id)
            assert standing == {
                "points": entry.points,
                "played": entry.played,
                "rank": entry.rank,
                "rounds": entry.standing.rounds,
                "pool_id": entry.standing.pool_id,
                "pair_keys": sorted(entry.standing.pair_keys),
            }

    def test_a_branch_from_outside_the_tournament_has_no_standing(
        self, fabric, repo
    ):
        from bin import fix_branches

        _git(repo, "checkout", "-b", "hand-written")
        (repo / "by-hand.txt").write_text("hand\n")
        _git(repo, "add", "by-hand.txt")
        _git(repo, "commit", "-m", "by hand")
        _git(repo, "checkout", "main")
        bid = fix_branches.register_branch(str(repo), "hand-written")

        from bin import dispatch

        assert dispatch.standing_for_branch(bid) is None

    def test_a_ledger_row_predating_the_pool_id_is_refused_by_name(
        self, fabric, repo
    ):
        from bin import dispatch

        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        record = dispatch.dispatch_domain(
            "dispatch-domain", repo_path=str(repo), base_ref="main",
            backend="fixture", backend_config_for=_per_item_config,
        )[0]
        conn = sqlite3.connect(str(fabric))
        conn.execute("DROP TRIGGER work_dispatch_only_a_claim_resolves_once")
        conn.execute("UPDATE work_dispatch SET pool_id='' WHERE fix_branch_id=?",
                     (record.fix_branch_id,))
        conn.commit()
        conn.close()
        with pytest.raises(ValueError, match="carries no pool_id"):
            dispatch.standing_for_branch(record.fix_branch_id)

    def test_validation_records_ranking_evidence_for_a_dispatched_branch(
        self, fabric, tmp_path
    ):
        from bin import branch_validator, dispatch

        repo = _harness_repo(tmp_path / "harness")
        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        entries = {e.item_id: e for e in dispatch.queue("dispatch-domain")}
        record = dispatch.dispatch_domain(
            "dispatch-domain", repo_path=str(repo), base_ref="main",
            backend="fixture", backend_config_for=_per_item_config,
        )[0]
        entry = entries[record.item_id]

        result = branch_validator.validate(
            record.fix_branch_id,
            red_cmd="./red.sh",
            green_cmd="./green.sh",
            scratch_dir=str(tmp_path / "scratch"),
            standing=dispatch.standing_for_branch(record.fix_branch_id),
        )
        assert result["passed"] is True
        assert result["ranking_evidence_join"] == "joined", (
            "the standing the dispatcher recorded is the standing the "
            "validator keys the beat outcome to"
        )
        example = result["ranking_evidence"]
        assert example["pool_id"] == entry.standing.pool_id
        assert example["pair_keys"] == sorted(entry.standing.pair_keys)
        assert example["workorder_ref"] == entry.item_id
        assert [
            row["workorder_ref"]
            for row in branch_validator.evidence_for_pair(
                entry.standing.pair_keys[0]
            )
        ] == [entry.item_id]

    def test_the_fix_branches_cli_closes_the_loop_on_its_own(
        self, fabric, tmp_path, capsys
    ):
        from bin import branch_validator, dispatch, fix_branches

        repo = _harness_repo(tmp_path / "harness")
        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        record = dispatch.dispatch_domain(
            "dispatch-domain", repo_path=str(repo), base_ref="main",
            backend="fixture", backend_config_for=_per_item_config,
        )[0]
        pool_id = dispatch.standing_for_branch(record.fix_branch_id)["pool_id"]

        assert fix_branches.main([
            "validate", "--id", str(record.fix_branch_id),
            "--red-cmd", "./red.sh", "--green-cmd", "./green.sh",
            "--scratch-dir", str(tmp_path / "scratch"),
        ]) == 0
        captured = capsys.readouterr()
        assert "standing from the dispatch ledger" in captured.err

        triples = branch_validator.ranking_evidence_for_pool(pool_id)
        assert [t["workorder_ref"] for t in triples] == [record.item_id], (
            "the production caller passes the standing; without it the "
            "return edge is dead on every real invocation"
        )
        assert triples[0]["join_status"] == "joined"
        assert triples[0]["outcome"] == "passed"

    def test_the_cli_can_refuse_the_return_edge_explicitly(
        self, fabric, tmp_path, capsys
    ):
        from bin import branch_validator, dispatch, fix_branches

        repo = _harness_repo(tmp_path / "harness")
        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        record = dispatch.dispatch_domain(
            "dispatch-domain", repo_path=str(repo), base_ref="main",
            backend="fixture", backend_config_for=_per_item_config,
        )[0]
        pool_id = dispatch.standing_for_branch(record.fix_branch_id)["pool_id"]
        assert fix_branches.main([
            "validate", "--id", str(record.fix_branch_id),
            "--red-cmd", "./red.sh", "--green-cmd", "./green.sh",
            "--scratch-dir", str(tmp_path / "scratch"), "--no-standing",
        ]) == 0
        assert branch_validator.ranking_evidence_for_pool(pool_id) == []


class TestWorkorderRefNamesTheItem:
    def test_two_items_of_one_domain_do_not_share_a_lineage(self, fabric, repo):
        from bin import dispatch, fix_branches

        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        records = dispatch.dispatch_domain(
            "dispatch-domain", repo_path=str(repo), base_ref="main",
            backend="fixture", backend_config_for=_per_item_config,
        )
        refs = [
            fix_branches.get_branch(r.fix_branch_id)["workorder_ref"]
            for r in records
        ]
        assert refs == [r.item_id for r in records]
        assert len(set(refs)) == 2, (
            "the domain name is a per-domain constant: lineage stamped with "
            "it cannot say which ranked item a branch implements"
        )

    def test_an_item_id_resolves_as_lineage_because_the_claim_precedes_it(
        self, fabric
    ):
        from bin import dispatch, fix_branches

        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        entry = dispatch.queue("dispatch-domain")[0]
        dispatch.init()
        with fix_branches._connect() as conn:
            assert not fix_branches._workorder_ref_resolves(conn, entry.item_id)
        domain_id = dispatch._resolve_domain("dispatch-domain")[0]
        dispatch._claim(domain_id, entry, rubric=None, branch_name="dispatch/x",
                        workorder_ref=entry.item_id)
        with fix_branches._connect() as conn:
            assert fix_branches._workorder_ref_resolves(conn, entry.item_id)


class TestRunExitCode:
    def test_a_run_that_authored_nothing_and_failed_exits_nonzero(
        self, fabric, repo, tmp_path, capsys
    ):
        from bin import dispatch

        stub = _stub_script(tmp_path / "always-fails.sh", "exit 3\n")
        config = tmp_path / "backend.json"
        config.write_text(json.dumps({"argv": [str(stub)]}))
        _domain([_payload("a"), _payload("b")])
        _judge(fabric)
        code = dispatch.main([
            "run", "--domain", "dispatch-domain", "--repo", str(repo),
            "--base-ref", "main", "--backend", "command",
            "--backend-config", str(config),
        ])
        assert code == 1
        captured = capsys.readouterr()
        assert "nothing was authored" in captured.err
        assert json.loads(captured.out)[0]["outcome"] == "failed"

    def test_a_partial_failure_still_exits_zero(self, fabric, repo, tmp_path):
        from bin import dispatch

        stub = _stub_script(tmp_path / "picky.sh", (
            'if [ "$WORKORDER_TITLE" = "poison" ]; then exit 7; fi\n'
            'echo ok > authored.txt\n'
        ))
        config = tmp_path / "backend.json"
        config.write_text(json.dumps({"argv": [str(stub)]}))
        _domain([_payload("poison"), _payload("clean")])
        _judge(fabric, losers=("clean",))
        assert dispatch.main([
            "run", "--domain", "dispatch-domain", "--repo", str(repo),
            "--base-ref", "main", "--backend", "command",
            "--backend-config", str(config),
        ]) == 0

    def test_a_run_with_nothing_to_do_exits_zero(self, fabric, repo):
        from bin import dispatch

        _domain([_payload("a"), _payload("b")])
        assert dispatch.main([
            "run", "--domain", "dispatch-domain", "--repo", str(repo),
            "--base-ref", "main", "--backend", "fixture",
        ]) == 0

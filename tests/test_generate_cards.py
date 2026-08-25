"""Tests for bin/generate_cards.py — corpus → cards → pending_judgement."""
import json
import sqlite3
import dspy
import pytest

from tests.conftest import _scripted_lm


@pytest.fixture
def fresh_fabric_with_domain(fake_langfuse, monkeypatch, tmp_data_home):
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    fake_langfuse.enable("create_prompt")
    fake_langfuse.enable("list_prompts")
    fake_langfuse.enable("set_label")
    import importlib, judgement
    importlib.reload(judgement)
    judgement.init_db()
    return tmp_data_home / "judgements.db"


def _make_inline_domain(name="test-domain", items=None):
    from bin import domains, prompts
    prompts.push(f"card-generator:{name}", "Generate cards.", labels=["production"])
    prompts.push(f"judge-instructions:{name}", "Judge cards.", labels=["production"])
    items = items or [{"text": "Item one"}, {"text": "Item two"}]
    return domains.create_domain(
        name=name,
        description="Test domain",
        corpus_source={"kind": "inline", "items": items},
    )


def test_iter_corpus_inline(fresh_fabric_with_domain):
    _make_inline_domain(items=[{"text": "A"}, {"text": "B"}, {"text": "C"}])
    from bin.generate_cards import _iter_corpus
    from bin import domains
    spec = domains.get_domain("test-domain")
    items = list(_iter_corpus(spec))
    assert len(items) == 3
    assert items[0]["text"] == "A"


def test_iter_corpus_filesystem(fresh_fabric_with_domain, tmp_path):
    (tmp_path / "a.md").write_text("# Alpha\nbody")
    (tmp_path / "b.md").write_text("# Beta\nbody")
    (tmp_path / "c.txt").write_text("not matched")
    from bin import domains, prompts
    prompts.push("card-generator:fs-domain", "x", labels=["production"])
    prompts.push("judge-instructions:fs-domain", "y", labels=["production"])
    domains.create_domain(
        name="fs-domain",
        description="",
        corpus_source={"kind": "filesystem", "root": str(tmp_path), "glob": "*.md"},
    )
    from bin.generate_cards import _iter_corpus
    spec = domains.get_domain("fs-domain")
    items = list(_iter_corpus(spec))
    paths = sorted(item["source_ref"] for item in items)
    assert paths == [str(tmp_path / "a.md"), str(tmp_path / "b.md")]
    assert any("Alpha" in i["text"] for i in items)


def test_iter_corpus_filesystem_supports_code_globs_and_skips_generated_or_binary(
    fresh_fabric_with_domain, tmp_path
):
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "worker.py").write_text("def work(): pass")
    (tmp_path / "lib" / "worker.ts").write_text("export const work = () => 1")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "vendor.ts").write_text("vendor")
    (tmp_path / "lib" / "binary.py").write_bytes(b"\xff\xfe\x00")

    from bin import domains, prompts
    prompts.push("card-generator:code-domain", "x", labels=["production"])
    prompts.push("judge-instructions:code-domain", "y", labels=["production"])
    domains.create_domain(
        name="code-domain",
        description="",
        corpus_source={
            "kind": "filesystem",
            "root": str(tmp_path),
            "glob": "**/*.py, **/*.ts",
        },
    )

    from bin.generate_cards import _iter_corpus
    spec = domains.get_domain("code-domain")
    items = list(_iter_corpus(spec))

    refs = {i["source_ref"] for i in items}
    assert refs == {
        str(tmp_path / "lib" / "worker.py"),
        str(tmp_path / "lib" / "worker.ts"),
    }


def test_run_replaces_model_guessed_source_with_authoritative_corpus_ref(
    fresh_fabric_with_domain, monkeypatch
):
    from bin import domains, generate_cards, prompts
    from bin.generators.card_gen import Card

    prompts.push("card-generator:provenance", "x", labels=["production"])
    prompts.push("judge-instructions:provenance", "y", labels=["production"])
    domains.create_domain(
        name="provenance",
        description="",
        corpus_source={
            "kind": "inline",
            "items": [{"text": "code", "source_ref": "actual/File.cs"}],
        },
    )

    class GuessingCardGen:
        def __init__(self, **_kwargs):
            pass

        def __call__(self, **_kwargs):
            return type(
                "Prediction",
                (),
                {"cards": [Card(title="Bug", body="Details", source_ref="guessed.cs")]},
            )()

    captured = []
    monkeypatch.setattr(generate_cards, "CardGen", GuessingCardGen)
    monkeypatch.setattr(
        generate_cards,
        "_enqueue_pairs",
        lambda _domain_id, items, _rng: captured.extend(items) or 0,
    )

    generate_cards.run("provenance")

    # _enqueue_pairs receives payload dicts; the authoritative corpus ref
    # must have replaced the model's guessed one.
    assert [item["source_ref"] for item in captured] == ["actual/File.cs"]


def test_iter_corpus_sqlite(fresh_fabric_with_domain, tmp_path):
    src_db_path = tmp_path / "src.db"
    src = sqlite3.connect(str(src_db_path))
    src.execute("CREATE TABLE notes(id INTEGER PRIMARY KEY, content TEXT, ref TEXT)")
    src.execute("INSERT INTO notes(content, ref) VALUES (?, ?)", ("first note", "n1"))
    src.execute("INSERT INTO notes(content, ref) VALUES (?, ?)", ("second note", "n2"))
    src.commit()
    src.close()
    from bin import domains, prompts
    prompts.push("card-generator:sql-domain", "x", labels=["production"])
    prompts.push("judge-instructions:sql-domain", "y", labels=["production"])
    domains.create_domain(
        name="sql-domain",
        description="",
        corpus_source={
            "kind": "sqlite",
            "path": str(src_db_path),
            "query": "SELECT id, content AS text, ref AS source_ref FROM notes",
        },
    )
    from bin.generate_cards import _iter_corpus
    spec = domains.get_domain("sql-domain")
    items = list(_iter_corpus(spec))
    refs = sorted(i["source_ref"] for i in items)
    assert refs == ["n1", "n2"]


def test_run_generates_cards_and_enqueues_pairs(fresh_fabric_with_domain):
    _make_inline_domain(items=[
        {"text": "first item"}, {"text": "second item"}, {"text": "third item"}
    ])
    dspy.settings.configure(lm=_scripted_lm(
        {"cards": [{"title": "Card from item 1", "body": "body 1"}]},
        {"cards": [{"title": "Card from item 2", "body": "body 2"}]},
        {"cards": [{"title": "Card from item 3", "body": "body 3"}]},
    ))
    from bin.generate_cards import run
    result = run("test-domain", seed=42)
    assert result.cards_generated == 3
    assert result.pairs_enqueued == 1  # 3 cards → 1 pair + 1 bye

    db = sqlite3.connect(str(fresh_fabric_with_domain))
    rows = db.execute(
        "SELECT trace_payload, domain_id FROM pending_judgement"
    ).fetchall()
    assert len(rows) >= 1
    payload = json.loads(rows[0][0])
    assert "card_a" in payload and "card_b" in payload


def test_repeated_runs_use_unique_match_ids(fresh_fabric_with_domain):
    _make_inline_domain(items=[{"text": "first"}, {"text": "second"}])
    dspy.settings.configure(lm=_scripted_lm(
        {"cards": [{"title": "First A", "body": "body"}]},
        {"cards": [{"title": "First B", "body": "body"}]},
        {"cards": [{"title": "Second A", "body": "body"}]},
        {"cards": [{"title": "Second B", "body": "body"}]},
    ))

    from bin.generate_cards import run

    assert run("test-domain", seed=1).pairs_enqueued == 1
    assert run("test-domain", seed=2).pairs_enqueued == 1

    db = sqlite3.connect(str(fresh_fabric_with_domain))
    match_ids = [
        row[0]
        for row in db.execute(
            "SELECT DISTINCT match_id FROM pending_judgement ORDER BY match_id"
        )
    ]
    assert match_ids == [0, 1]


# ── L6 regression: exactly ONE pending row per pair, human config only ────
# Baseline showcase run (2026-08-17): one run logged 'enqueued 1 pair(s)'
# but wrote 4 identical rows — one per ACTIVE job_configuration (3-model
# LLM panel + human) because the domain rubric matched no template name.


def test_one_pending_row_per_pair_human_config_only(fresh_fabric_with_domain):
    _make_inline_domain(items=[{"text": "first"}, {"text": "second"}])
    dspy.settings.configure(lm=_scripted_lm(
        {"cards": [{"title": "A", "body": "body"}]},
        {"cards": [{"title": "B", "body": "body"}]},
    ))
    from bin.generate_cards import run

    result = run("test-domain", seed=7)
    assert result.pairs_enqueued == 1

    db = sqlite3.connect(str(fresh_fabric_with_domain))
    db.row_factory = sqlite3.Row
    active_cfgs = db.execute(
        "SELECT COUNT(*) c FROM job_configuration WHERE status='active'"
    ).fetchone()["c"]
    assert active_cfgs >= 2, "fixture must have LLM panel + human configs"

    rows = db.execute(
        "SELECT p.id, c.rater_type FROM pending_judgement p "
        "JOIN job_configuration c ON c.id = p.config_id"
    ).fetchall()
    # Logged pair count == inserted row count — never one per config.
    assert len(rows) == result.pairs_enqueued == 1
    assert all(r["rater_type"] == "human" for r in rows)


def test_second_domain_does_not_multiply_pending_rows(fresh_fabric_with_domain):
    _make_inline_domain(name="domain-one", items=[{"text": "a"}, {"text": "b"}])
    _make_inline_domain(name="domain-two", items=[{"text": "c"}, {"text": "d"}])
    dspy.settings.configure(lm=_scripted_lm(
        {"cards": [{"title": "A1", "body": "body"}]},
        {"cards": [{"title": "B1", "body": "body"}]},
        {"cards": [{"title": "A2", "body": "body"}]},
        {"cards": [{"title": "B2", "body": "body"}]},
    ))
    from bin.generate_cards import run

    assert run("domain-one", seed=1).pairs_enqueued == 1
    assert run("domain-two", seed=2).pairs_enqueued == 1

    db = sqlite3.connect(str(fresh_fabric_with_domain))
    counts = db.execute(
        "SELECT domain_id, COUNT(*) FROM pending_judgement GROUP BY domain_id"
    ).fetchall()
    # One row per domain — creating more domains/configs never multiplies.
    assert sorted(n for _, n in counts) == [1, 1]


def test_run_emits_progress_lines(fresh_fabric_with_domain, capsys):
    _make_inline_domain(items=[{"text": "x"}])
    dspy.settings.configure(lm=_scripted_lm(
        {"cards": [{"title": "T", "body": "B"}]},
    ))
    from bin.generate_cards import run
    run("test-domain", seed=1)
    out = capsys.readouterr().out
    assert "[generate]" in out


def test_run_continues_on_per_item_error(fresh_fabric_with_domain):
    _make_inline_domain(items=[{"text": "good"}, {"text": "bad"}, {"text": "good2"}])

    calls = {"n": 0}

    from bin.generators.card_gen import Card
    from bin.generators import card_gen
    import bin.generate_cards as gc

    def flaky_forward(self, *, corpus_text):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("LM exploded")
        return type("P", (), {"cards": [Card(title=f"t{calls['n']}", body="b")]})()

    real_card_gen = card_gen.CardGen
    class FlakyCardGen(real_card_gen):
        forward = flaky_forward

    gc.CardGen = FlakyCardGen
    try:
        result = gc.run("test-domain", seed=1)
    finally:
        gc.CardGen = real_card_gen

    assert result.cards_generated == 2
    assert result.errors == 1
    # An unclassified exception is still counted, under the generic class.
    assert result.failures == {"error": 1}


def _run_with_failing_gen(gc, exceptions_by_call, good_title="ok"):
    """Run the loop with a CardGen whose forward raises per scripted call.

    ``exceptions_by_call`` maps 1-based call numbers to exception instances;
    unscripted calls succeed with a single card.
    """
    from bin.generators.card_gen import Card
    from bin.generators import card_gen

    calls = {"n": 0}

    def scripted_forward(self, *, corpus_text):
        calls["n"] += 1
        exc = exceptions_by_call.get(calls["n"])
        if exc is not None:
            raise exc
        return type(
            "P", (), {"cards": [Card(title=f"{good_title}{calls['n']}", body="b")]}
        )()

    real_card_gen = card_gen.CardGen
    class ScriptedCardGen(real_card_gen):
        forward = scripted_forward

    gc.CardGen = ScriptedCardGen
    try:
        return gc.run("test-domain", seed=1)
    finally:
        gc.CardGen = real_card_gen


def test_run_classifies_failures_per_class_and_continues(
    fresh_fabric_with_domain, capsys
):
    """Timeout, parse-error, and truncation are counted separately in the
    loop diagnostics; the loop still processes every remaining item."""
    from bin.generators.card_gen import (
        CardGenParseError, CardGenTimeout, CardGenTruncation,
    )
    import bin.generate_cards as gc

    _make_inline_domain(items=[
        {"text": "a"}, {"text": "b"}, {"text": "c"}, {"text": "d"}
    ])

    result = _run_with_failing_gen(gc, {
        1: CardGenTimeout("timed out"),
        2: CardGenParseError("bad payload"),
        3: CardGenTruncation("hit token limit"),
    })

    # Loop continued past all three failures and item 4 still succeeded.
    assert result.cards_generated == 1
    assert result.errors == 3
    assert result.failures == {"timeout": 1, "parse-error": 1, "truncation": 1}

    captured = capsys.readouterr()
    assert "failure=timeout" in captured.err
    assert "failure=parse-error" in captured.err
    assert "failure=truncation" in captured.err
    # Summary line carries the per-class breakdown.
    assert "3 errors" in captured.out
    assert "timeout=1" in captured.out


def test_run_truncation_yields_no_card_for_that_item(fresh_fabric_with_domain):
    """A truncated finding is failed, never repaired into a card."""
    from bin.generators.card_gen import CardGenTruncation
    import bin.generate_cards as gc

    _make_inline_domain(items=[{"text": "a"}, {"text": "b"}])
    result = _run_with_failing_gen(gc, {1: CardGenTruncation("cut off")})

    assert result.cards_generated == 1  # only item 2's card
    assert result.failures == {"truncation": 1}


def test_run_success_path_reports_no_failures(fresh_fabric_with_domain, capsys):
    _make_inline_domain(items=[{"text": "a"}, {"text": "b"}])
    dspy.settings.configure(lm=_scripted_lm(
        {"cards": [{"title": "A", "body": "b"}]},
        {"cards": [{"title": "B", "body": "b"}]},
    ))
    from bin.generate_cards import run
    result = run("test-domain", seed=1)
    assert result.cards_generated == 2
    assert result.errors == 0
    assert result.failures == {}
    assert "(0 errors)" in capsys.readouterr().out


def test_generation_lm_has_role_specific_bounds(monkeypatch):
    from bin import generate_cards, optimize

    captured = {}
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("LLM_NUM_RETRIES", "2")
    monkeypatch.setattr(
        optimize.dspy,
        "LM",
        lambda **kwargs: captured.update(kwargs) or object(),
    )

    generate_cards._build_generation_lm()

    assert captured["timeout"] == 90.0
    assert captured["num_retries"] == 0
    assert captured["max_tokens"] == 16384


def test_generation_lm_bounds_are_configurable(monkeypatch):
    from bin import generate_cards, optimize

    captured = {}
    monkeypatch.setenv("GENERATOR_MODEL", "z-ai/glm-5.2")
    monkeypatch.setenv("GENERATOR_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("GENERATOR_NUM_RETRIES", "1")
    monkeypatch.setenv("GENERATOR_MAX_TOKENS", "1024")
    monkeypatch.setattr(
        optimize.dspy,
        "LM",
        lambda **kwargs: captured.update(kwargs) or object(),
    )

    generate_cards._build_generation_lm()

    assert captured["model"] == "openai/z-ai/glm-5.2"
    assert captured["timeout"] == 12.5
    assert captured["num_retries"] == 1
    assert captured["max_tokens"] == 1024


# ── systemic-failure circuit breaker + item budget (2026-08-17) ─────────
# Live repro: a filesystem domain over a large Unity repo emitted one
# identical AuthenticationError / connect-failure per file, hundreds of
# times, because provider-level failures were treated like per-item
# content failures. The loop must trip a breaker and stop; per-item
# parse/timeout failures must keep continuing as before.

def test_run_aborts_after_consecutive_systemic_provider_failures(
    fresh_fabric_with_domain, capsys
):
    import bin.generate_cards as gc

    _make_inline_domain(items=[{"text": t} for t in "abcdefgh"])

    auth = RuntimeError(
        "litellm.AuthenticationError: AuthenticationError: OpenAIException - "
        "Authentication Error, All connection attempts failed"
    )
    result = _run_with_failing_gen(gc, {1: auth, 2: auth, 3: auth, 4: auth})

    # Breaker trips at 3 consecutive systemic failures; items 4-8 never run.
    assert result.errors == 3
    assert result.cards_generated == 0
    assert "provider unavailable after 3 consecutive failures" in result.aborted_reason
    captured = capsys.readouterr()
    assert "ABORTED" in captured.err
    assert "OPENROUTER_API_KEY" in captured.err


def test_run_systemic_breaker_resets_on_success(fresh_fabric_with_domain):
    import bin.generate_cards as gc

    _make_inline_domain(items=[{"text": t} for t in "abcdef"])

    conn_err = RuntimeError("Cannot connect to host 127.0.0.1:5663")
    # Two systemic failures, then a success, then two more: never 3 in a row.
    result = _run_with_failing_gen(gc, {1: conn_err, 2: conn_err, 4: conn_err, 5: conn_err})

    assert result.aborted_reason == ""
    assert result.errors == 4
    assert result.cards_generated == 2  # items 3 and 6


def test_run_per_item_failures_do_not_trip_breaker(fresh_fabric_with_domain):
    from bin.generators.card_gen import CardGenParseError, CardGenTimeout
    import bin.generate_cards as gc

    _make_inline_domain(items=[{"text": t} for t in "abcde"])

    result = _run_with_failing_gen(gc, {
        1: CardGenTimeout("slow"),
        2: CardGenParseError("bad"),
        3: CardGenTimeout("slow again"),
        4: CardGenParseError("bad again"),
    })

    assert result.aborted_reason == ""
    assert result.errors == 4
    assert result.cards_generated == 1  # item 5 still ran


def test_run_default_item_budget_bounds_large_corpora(
    fresh_fabric_with_domain, monkeypatch, capsys
):
    import bin.generate_cards as gc

    _make_inline_domain(items=[{"text": f"item {i}"} for i in range(6)])
    monkeypatch.setenv("GENERATOR_MAX_ITEMS", "3")

    result = _run_with_failing_gen(gc, {})

    assert result.cards_generated == 3  # stopped at the budget, not at 6
    out = capsys.readouterr().out
    assert "item_budget=3" in out
    assert "item budget reached (3)" in out
    assert "GENERATOR_MAX_ITEMS" in out


def test_explicit_limit_still_overrides_default_budget(fresh_fabric_with_domain):
    from bin.generators.card_gen import Card
    from bin.generators import card_gen
    import bin.generate_cards as gc

    _make_inline_domain(items=[{"text": f"item {i}"} for i in range(5)])

    calls = {"n": 0}

    def counting_forward(self, *, corpus_text):
        calls["n"] += 1
        return type("P", (), {"cards": [Card(title=f"t{calls['n']}", body="b")]})()

    real = card_gen.CardGen
    class CountingCardGen(real):
        forward = counting_forward

    gc.CardGen = CountingCardGen
    try:
        result = gc.run("test-domain", limit=2, seed=1)
    finally:
        gc.CardGen = real

    assert calls["n"] == 2
    assert result.cards_generated == 2


def test_env_loader_never_overrides_real_environment(tmp_path, monkeypatch):
    from bin.env_loader import load_dotenv

    (tmp_path / ".env").write_text("DEMO_KEY_A=from_dotenv\nDEMO_KEY_B=only_dotenv\n")
    monkeypatch.setenv("DEMO_KEY_A", "from_real_env")
    monkeypatch.delenv("DEMO_KEY_B", raising=False)

    load_dotenv(start=tmp_path)

    import os
    assert os.environ["DEMO_KEY_A"] == "from_real_env"
    assert os.environ["DEMO_KEY_B"] == "only_dotenv"
    monkeypatch.delenv("DEMO_KEY_B", raising=False)


# ── config banner + hidden tool dirs + abort exit code (2026-08-17) ─────
# Live repro: a stale in-flight job printed old bounds (max_tokens=4096)
# with no way to tell it apart from a fresh one, and its first corpus item
# was .claude/skills/... — agent config, not product code.

def test_run_prints_effective_lm_config_banner(fresh_fabric_with_domain, capsys):
    import bin.generate_cards as gc

    _make_inline_domain(items=[{"text": "a"}])
    _run_with_failing_gen(gc, {})

    out = capsys.readouterr().out
    assert "config max_tokens=16384 timeout=180s retries=0" in out


def test_iter_filesystem_skips_hidden_tool_dirs(tmp_path):
    from bin.corpus import iter_filesystem_paths

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('real code')")
    for tool_dir in (".claude/skills/foo", ".cursor", ".vscode", ".idea"):
        d = tmp_path / tool_dir
        d.mkdir(parents=True)
        (d / "config.py").write_text("# agent config, not product code")

    paths = list(iter_filesystem_paths({"root": str(tmp_path), "glob": "**/*.py"}))
    assert [p.name for p in paths] == ["app.py"]


def test_main_exits_nonzero_on_systemic_abort(fresh_fabric_with_domain, monkeypatch, capsys):
    import bin.generate_cards as gc

    _make_inline_domain(items=[{"text": t} for t in "abcd"])
    auth = RuntimeError("AuthenticationError: All connection attempts failed")

    monkeypatch.setattr(
        "sys.argv", ["generate_cards.py", "--domain", "test-domain", "--seed", "1"]
    )

    from bin.generators.card_gen import Card
    from bin.generators import card_gen

    calls = {"n": 0}

    def scripted_forward(self, *, corpus_text):
        calls["n"] += 1
        raise auth

    real = card_gen.CardGen
    class FailingCardGen(real):
        forward = scripted_forward

    gc.CardGen = FailingCardGen
    try:
        with pytest.raises(SystemExit) as exc:
            gc.main()
    finally:
        gc.CardGen = real

    assert exc.value.code == 2


# ── work-order model provenance from active LM (2026-08-17) ─────────────
# cfg.model may be unset (falls through to the frontier panel default in
# the LM builder); recording an empty models list while a real model did
# the work would be false provenance. run() must read the ACTIVE LM.

def test_workorder_models_come_from_active_lm(fresh_fabric_with_domain, monkeypatch):
    import dspy
    import bin.generate_cards as gc
    from bin.workorder import WorkOrderDraft

    monkeypatch.delenv("GENERATOR_MODEL", raising=False)

    _make_inline_domain(items=[{"text": "a"}, {"text": "b"}])

    class DraftingGen:
        def __init__(self, **_kw):
            pass

        def __call__(self, **_kw):
            return type("P", (), {"work_orders": [
                WorkOrderDraft(title="t", goal="g", plan="p")
            ]})()

    captured = []
    monkeypatch.setattr(gc, "WorkOrderGen", DraftingGen)
    monkeypatch.setattr(
        gc, "_enqueue_pairs", lambda _d, items, _r: captured.extend(items) or 0
    )
    # Simulate the frontier-panel fallback: active LM has a model name the
    # generator config never saw.
    monkeypatch.setattr(
        dspy.settings, "lm", type("LM", (), {"model": "openrouter/panel-default"})(),
        raising=False,
    )

    gc.run("test-domain", seed=1, artifact="work-order")

    assert captured, "expected work-order payloads"
    for item in captured:
        assert item["work_order"]["models"] == ["openrouter/panel-default"]


# ── pipeline-stamped cited_evidence (wave-8 B1, 2026-08-17) ──────────────
# The judge view renders payload["cited_evidence"] digests; generation must
# STAMP them from system-captured git state. The model never supplies them
# (WorkOrderDraft has no such field).

def _git_corpus(tmp_path):
    """Throwaway git repo with one committed corpus file."""
    import subprocess
    root = tmp_path / "gitrepo"
    root.mkdir()
    (root / "mod.py").write_text("def f():\n    return 1\n")
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
           "PATH": "/usr/bin:/bin"}
    for args in (["git", "init", "-q"],
                 ["git", "add", "mod.py"],
                 ["git", "-c", "user.name=t", "-c", "user.email=t@t",
                  "commit", "-qm", "seed"]):
        subprocess.run(args, cwd=root, env=env, check=True)
    return root


def _drafting_gen(monkeypatch):
    import bin.generate_cards as gc
    from bin.workorder import WorkOrderDraft

    class DraftingGen:
        def __init__(self, **_kw):
            pass

        def __call__(self, **_kw):
            return type("P", (), {"work_orders": [
                WorkOrderDraft(title="t", goal="g", plan="p")
            ]})()

    monkeypatch.setattr(gc, "WorkOrderGen", DraftingGen)
    return gc


def test_workorder_payloads_carry_pipeline_stamped_citations(
    fresh_fabric_with_domain, tmp_path, monkeypatch
):
    from bin import catalog, domains, prompts
    gc = _drafting_gen(monkeypatch)
    root = _git_corpus(tmp_path)

    prompts.push("card-generator:cited-domain", "x", labels=["production"])
    prompts.push("judge-instructions:cited-domain", "y", labels=["production"])
    domains.create_domain(
        name="cited-domain", description="",
        corpus_source={"kind": "filesystem", "root": str(root), "glob": "*.py"},
    )

    captured = []
    monkeypatch.setattr(
        gc, "_enqueue_pairs", lambda _d, items, _r: captured.extend(items) or 0
    )
    gc.run("cited-domain", seed=1, artifact="work-order")

    assert captured, "expected work-order payloads"
    payload = captured[0]
    digests = payload.get("cited_evidence")
    assert digests, "pipeline must stamp cited_evidence for git corpora"

    # The digests resolve in the catalog to TIER1 refs pinned to the commit.
    ref = catalog.get_evidence_ref(digests[0])
    assert ref["trust_tier"] == 1
    assert ref["locator"].startswith("git:")
    assert "mod.py" in ref["locator"]
    # Auto-created project/source plumbing exists and is linked.
    src = catalog.get_source("cited-domain", "corpus")
    assert ref["source_id"] == src["id"]


def test_non_git_corpus_generates_without_citations(
    fresh_fabric_with_domain, tmp_path, monkeypatch
):
    """Fail-open: a filesystem corpus outside git still generates fine —
    payloads simply carry no cited_evidence key."""
    from bin import domains, prompts
    gc = _drafting_gen(monkeypatch)
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "doc.py").write_text("x = 1")

    prompts.push("card-generator:plain-domain", "x", labels=["production"])
    prompts.push("judge-instructions:plain-domain", "y", labels=["production"])
    domains.create_domain(
        name="plain-domain", description="",
        corpus_source={"kind": "filesystem", "root": str(plain), "glob": "*.py"},
    )

    captured = []
    monkeypatch.setattr(
        gc, "_enqueue_pairs", lambda _d, items, _r: captured.extend(items) or 0
    )
    gc.run("plain-domain", seed=1, artifact="work-order")

    assert captured
    assert "cited_evidence" not in captured[0]


def test_model_can_never_supply_cited_evidence():
    """The draft schema must not grow a citations field — citations are
    pipeline-stamped, and a model-writable field would be laundering."""
    from bin.workorder import WorkOrderDraft
    assert "cited_evidence" not in WorkOrderDraft.model_fields
    assert "evidence_digests" not in WorkOrderDraft.model_fields


# ── Wave-12: single enqueue (one pending per artifact, no fake pairs) ─────


def _make_single_domain(name="single-domain", items=None,
                        rubric="single-execution-v1"):
    from bin import domains, prompts
    prompts.push(f"card-generator:{name}", "Generate cards.", labels=["production"])
    prompts.push(f"judge-instructions:{name}", "Judge cards.", labels=["production"])
    items = items or [{"text": "Item one"}, {"text": "Item two"}, {"text": "Item three"}]
    return domains.create_domain(
        name=name,
        description="Single-judgement domain",
        corpus_source={"kind": "inline", "items": items},
        rubric=rubric,
    )


def test_enqueue_singles_writes_one_pending_per_artifact(fresh_fabric_with_domain):
    domain_id = _make_single_domain()
    from bin.generate_cards import enqueue_singles

    items = [
        {"title": "One", "body": "b1", "source_ref": "r1"},
        {"title": "Two", "body": "b2", "source_ref": "r2"},
        {"title": "Three", "body": "b3", "source_ref": "r3"},
    ]
    assert enqueue_singles(domain_id, items) == 3

    db = sqlite3.connect(str(fresh_fabric_with_domain))
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT p.match_id, p.trace_payload, p.domain_id, c.rater_type, "
        "       t.name AS template_name "
        "FROM pending_judgement p "
        "JOIN job_configuration c ON c.id = p.config_id "
        "JOIN eval_template t ON t.id = c.template_id "
        "ORDER BY p.match_id"
    ).fetchall()
    db.close()

    # L6 invariant: exactly ONE pending row per artifact, human config only,
    # bound to the domain's single-kind rubric.
    assert len(rows) == 3
    assert all(r["rater_type"] == "human" for r in rows)
    assert all(r["template_name"] == "single-execution-v1" for r in rows)
    assert all(r["domain_id"] == domain_id for r in rows)
    # match_id stays unique per domain — no pair pretence.
    assert [r["match_id"] for r in rows] == [0, 1, 2]
    for r, item in zip(rows, items):
        payload = json.loads(r["trace_payload"])
        assert payload["card"] == item
        assert "card_a" not in payload and "card_b" not in payload
        assert payload["label"]


def test_enqueue_singles_continues_match_ids_across_runs(fresh_fabric_with_domain):
    domain_id = _make_single_domain(name="single-continue")
    from bin.generate_cards import enqueue_singles

    assert enqueue_singles(domain_id, [{"title": "A", "body": "b"}]) == 1
    assert enqueue_singles(domain_id, [{"title": "B", "body": "b"}]) == 1

    db = sqlite3.connect(str(fresh_fabric_with_domain))
    match_ids = [r[0] for r in db.execute(
        "SELECT match_id FROM pending_judgement WHERE domain_id=? "
        "ORDER BY match_id", (domain_id,),
    )]
    db.close()
    assert match_ids == [0, 1]


def test_run_uses_singles_when_domain_rubric_is_single_kind(
    fresh_fabric_with_domain,
):
    """No flag needed: the domain's active template is single-kind, so run()
    selects the singles path — every artifact gets its own pending row."""
    _make_single_domain(name="auto-single", items=[
        {"text": "first"}, {"text": "second"}, {"text": "third"},
    ], rubric="single-execution-v1")
    dspy.settings.configure(lm=_scripted_lm(
        {"cards": [{"title": "C1", "body": "b1"}]},
        {"cards": [{"title": "C2", "body": "b2"}]},
        {"cards": [{"title": "C3", "body": "b3"}]},
    ))
    from bin.generate_cards import run

    result = run("auto-single", seed=3)
    assert result.cards_generated == 3
    assert result.singles_enqueued == 3  # 3 artifacts → 3 singles, no bye
    assert result.pairs_enqueued == 0

    db = sqlite3.connect(str(fresh_fabric_with_domain))
    payloads = [json.loads(r[0]) for r in db.execute(
        "SELECT trace_payload FROM pending_judgement"
    )]
    db.close()
    assert len(payloads) == 3
    for p in payloads:
        assert "card" in p and "card_a" not in p and "card_b" not in p


def test_run_judgement_kind_flag_forces_singles(fresh_fabric_with_domain):
    """Explicit judgement_kind='single' selects singles even when the
    domain's rubric is the legacy pair-kind card-prioritizer-v0."""
    _make_inline_domain(items=[{"text": "first"}, {"text": "second"}])
    dspy.settings.configure(lm=_scripted_lm(
        {"cards": [{"title": "A", "body": "body"}]},
        {"cards": [{"title": "B", "body": "body"}]},
    ))
    from bin.generate_cards import run

    result = run("test-domain", seed=5, judgement_kind="single")
    assert result.singles_enqueued == 2
    assert result.pairs_enqueued == 0

    db = sqlite3.connect(str(fresh_fabric_with_domain))
    payloads = [json.loads(r[0]) for r in db.execute(
        "SELECT trace_payload FROM pending_judgement"
    )]
    db.close()
    assert all("card" in p and "card_a" not in p for p in payloads)


def test_run_defaults_to_pairs_for_legacy_rubric(fresh_fabric_with_domain):
    """Regression: default kind stays 'pair' — legacy domains unchanged."""
    _make_inline_domain(items=[{"text": "first"}, {"text": "second"}])
    dspy.settings.configure(lm=_scripted_lm(
        {"cards": [{"title": "A", "body": "body"}]},
        {"cards": [{"title": "B", "body": "body"}]},
    ))
    from bin.generate_cards import run

    result = run("test-domain", seed=5)
    assert result.pairs_enqueued == 1
    assert result.singles_enqueued == 0


def test_single_enqueue_end_to_end_judgement_against_single_rubric(
    fresh_fabric_with_domain,
):
    """Full loop: generate → single pending → write_judgement validates the
    verdict against the single-kind rubric (and skip stays valid)."""
    _make_single_domain(name="e2e-single", items=[
        {"text": "one"}, {"text": "two"},
    ], rubric="single-execution-v1")
    dspy.settings.configure(lm=_scripted_lm(
        {"cards": [{"title": "C1", "body": "b1"}]},
        {"cards": [{"title": "C2", "body": "b2"}]},
    ))
    from bin.generate_cards import run
    import judgement

    result = run("e2e-single", seed=9)
    assert result.singles_enqueued == 2

    pending = judgement.list_pending(rater_type="human", limit=10)
    assert len(pending) == 2
    outdef = pending[0]["output_definition"]
    assert outdef["judgement_kind"] == "single"
    assert outdef["wheel"]["n"] == "approve"

    rating_id = judgement.write_judgement(
        pending_id=pending[0]["id"], verdict="approve-with-notes",
        confidence="high", rationale="solid but note the edge case",
        rater={"type": "human", "userId": "u1"},
    )
    assert rating_id
    # skip remains valid on the second pending.
    assert judgement.write_judgement(
        pending_id=pending[1]["id"], verdict="skip", confidence="low",
        rationale=None, rater={"type": "human", "userId": "u1"},
    )
    # A pair-style verdict is refused by the single-kind rubric.
    db = sqlite3.connect(str(fresh_fabric_with_domain))
    scores = db.execute(
        "SELECT name, value FROM score WHERE rating_id=?", (rating_id,)
    ).fetchall()
    statuses = [r[0] for r in db.execute(
        "SELECT status FROM pending_judgement"
    )]
    db.close()
    assert sorted(s[0] for s in scores) == [
        "judgement.confidence", "judgement.verdict"
    ]
    assert dict(scores)["judgement.verdict"] == "approve-with-notes"
    assert statuses == ["done", "done"]


def test_run_explicit_rubric_overrides_domain_for_kind_and_binding(
    fresh_fabric_with_domain,
):
    """--rubric single-idea-v1 on a legacy pair domain: singles path, rows
    bound to the explicit rubric's human config."""
    _make_inline_domain(items=[{"text": "first"}])
    dspy.settings.configure(lm=_scripted_lm(
        {"cards": [{"title": "A", "body": "body"}]},
    ))
    from bin.generate_cards import run

    result = run("test-domain", seed=1, rubric="single-idea-v1")
    assert result.singles_enqueued == 1

    db = sqlite3.connect(str(fresh_fabric_with_domain))
    row = db.execute(
        "SELECT t.name FROM pending_judgement p "
        "JOIN job_configuration c ON c.id = p.config_id "
        "JOIN eval_template t ON t.id = c.template_id"
    ).fetchone()
    db.close()
    assert row[0] == "single-idea-v1"

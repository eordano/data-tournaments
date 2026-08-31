"""Tests for bin/generate_cards.py — corpus → cards → pending_judgement."""
import json
import sqlite3
import dspy
import pathlib
import pytest

from tests.conftest import hermetic_git_env

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
        lambda _domain_id, items, _rng, rounds=None: captured.extend(items) or 0,
    )

    generate_cards.run("provenance")

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
    assert result.pairs_enqueued == 1

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

    assert result.cards_generated == 1
    assert result.errors == 3
    assert result.failures == {"timeout": 1, "parse-error": 1, "truncation": 1}

    captured = capsys.readouterr()
    assert "failure=timeout" in captured.err
    assert "failure=parse-error" in captured.err
    assert "failure=truncation" in captured.err
    assert "3 errors" in captured.out
    assert "timeout=1" in captured.out

def test_run_truncation_yields_no_card_for_that_item(fresh_fabric_with_domain):
    """A truncated finding is failed, never repaired into a card."""
    from bin.generators.card_gen import CardGenTruncation
    import bin.generate_cards as gc

    _make_inline_domain(items=[{"text": "a"}, {"text": "b"}])
    result = _run_with_failing_gen(gc, {1: CardGenTruncation("cut off")})

    assert result.cards_generated == 1
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
    result = _run_with_failing_gen(gc, {1: conn_err, 2: conn_err, 4: conn_err, 5: conn_err})

    assert result.aborted_reason == ""
    assert result.errors == 4
    assert result.cards_generated == 2

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
    assert result.cards_generated == 1

def test_run_default_item_budget_bounds_large_corpora(
    fresh_fabric_with_domain, monkeypatch, capsys
):
    import bin.generate_cards as gc

    _make_inline_domain(items=[{"text": f"item {i}"} for i in range(6)])
    monkeypatch.setenv("GENERATOR_MAX_ITEMS", "3")

    result = _run_with_failing_gen(gc, {})

    assert result.cards_generated == 3
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

def test_workorder_models_come_from_active_lm(fresh_fabric_with_domain, monkeypatch):
    import dspy
    import bin.generate_cards as gc
    from bin.workorder import WorkOrderDraft

    monkeypatch.delenv("GENERATOR_MODEL", raising=False)

    _make_inline_domain(items=[{"text": "a"}, {"text": "b"}])

    class DraftingGen:
        signature = type("Sig", (), {"instructions": "draft work orders"})()

        def __init__(self, **_kw):
            pass

        def __call__(self, **_kw):
            return type("P", (), {"work_orders": [
                WorkOrderDraft(title="t", goal="g", plan="p")
            ]})()

    captured = []
    monkeypatch.setattr(gc, "WorkOrderGen", DraftingGen)
    monkeypatch.setattr(
        gc, "_enqueue_pairs", lambda _d, items, _r, rounds=None: captured.extend(items) or 0
    )
    monkeypatch.setattr(
        dspy.settings, "lm", type("LM", (), {"model": "openrouter/panel-default"})(),
        raising=False,
    )

    gc.run("test-domain", seed=1, artifact="work-order")

    assert captured, "expected work-order payloads"
    for item in captured:
        assert item["work_order"]["models"] == ["openrouter/panel-default"]

def _git_corpus(tmp_path):
    """Throwaway git repo with one committed corpus file."""
    import subprocess
    root = tmp_path / "gitrepo"
    root.mkdir()
    (root / "mod.py").write_text("def f():\n    return 1\n")
    env = hermetic_git_env(root)
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
        signature = type("Sig", (), {"instructions": "draft work orders"})()

        def __init__(self, **_kw):
            pass

        def __call__(self, **_kw):
            return type("P", (), {"work_orders": [
                WorkOrderDraft(title="t", goal="g", plan="p")
            ]})()

    monkeypatch.setattr(gc, "WorkOrderGen", DraftingGen)
    _stub_explorer(monkeypatch)
    return gc

def _stub_explorer(monkeypatch):
    """Stand in for the ReAct explorer, which needs a live model.

    These tests are about what the pipeline stamps onto a payload, not about
    exploration, so the explorer returns one draft against the first corpus
    file it was handed.
    """
    from bin.generators import explorer
    from bin.workorder import WorkOrderDraft

    def explore(*, root, files, **_kw):
        first = files.splitlines()[0].split(" (")[0]
        ref = str(pathlib.Path(root) / first)
        return [(WorkOrderDraft(title="t", goal="g", plan="p"), ref)], []

    monkeypatch.setattr(explorer, "explore", explore)

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
        gc, "_enqueue_pairs", lambda _d, items, _r, rounds=None: captured.extend(items) or 0
    )
    gc.run("cited-domain", seed=1, artifact="work-order")

    assert captured, "expected work-order payloads"
    payload = captured[0]
    digests = payload.get("cited_evidence")
    assert digests, "pipeline must stamp cited_evidence for git corpora"

    ref = catalog.get_evidence_ref(digests[0])
    assert ref["trust_tier"] == 1
    assert ref["locator"].startswith("git:")
    assert "mod.py" in ref["locator"]
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
        gc, "_enqueue_pairs", lambda _d, items, _r, rounds=None: captured.extend(items) or 0
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

    assert len(rows) == 3
    assert all(r["rater_type"] == "human" for r in rows)
    assert all(r["template_name"] == "single-execution-v1" for r in rows)
    assert all(r["domain_id"] == domain_id for r in rows)
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
    assert result.singles_enqueued == 3
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
    domain's rubric is the legacy pair-kind pair-wheel-v2."""
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
    assert judgement.write_judgement(
        pending_id=pending[1]["id"], verdict="skip", confidence="low",
        rationale=None, rater={"type": "human", "userId": "u1"},
    )
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

def _wheel_domain(name="round-domain", count=9):
    """A work-order domain bound to pair-wheel-v2, plus `count` payloads."""
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
    payloads = [
        {"kind": "work-order", "title": f"order {i}", "body": f"body {i}",
         "source_ref": f"ref-{i}"}
        for i in range(count)
    ]
    return domain_id, payloads

def _pending_rows(db_path, status=None):
    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    sql = "SELECT id, match_id, trace_payload, status FROM pending_judgement"
    if status is not None:
        sql += f" WHERE status='{status}'"
    rows = db.execute(sql + " ORDER BY match_id").fetchall()
    db.close()
    return rows

def _payloads_of(rows):
    return [json.loads(r["trace_payload"]) for r in rows]

def _resolve(rows, verdict="a-wins"):
    import judgement
    for row in rows:
        judgement.write_judgement(
            pending_id=row["id"], verdict=verdict, confidence="mid",
            rater={"type": "human", "userId": "tester"},
        )

def test_round_one_enqueues_the_whole_work_order_pool(fresh_fabric_with_domain):
    import random as _random
    from bin import generate_cards

    domain_id, payloads = _wheel_domain(count=9)
    enqueued = generate_cards._enqueue_pairs(domain_id, payloads,
                                             _random.Random(3))
    assert enqueued == 4, "nine work orders pair into four matches and a bye"

    rows = _pending_rows(fresh_fabric_with_domain)
    drawn = _payloads_of(rows)
    assert [p["round"] for p in drawn] == [1, 1, 1, 1]
    assert [p["label"] for p in drawn] == ["R1-1", "R1-2", "R1-3", "R1-4"]
    assert len({p["pair_key"] for p in drawn}) == 4
    assert all(p["rubric"] == "pair-wheel-v2" for p in drawn)

    played = {p["item_a"] for p in drawn} | {p["item_b"] for p in drawn}
    byes = {b["item_id"] for p in drawn for b in p["byes"]}
    assert len(played) == 8 and len(byes) == 1
    assert played.isdisjoint(byes)
    assert all("card_a" in p and "card_b" in p for p in drawn), (
        "the judge surface still reads two cards and no standing"
    )
    assert all("points" not in json.dumps(p) for p in drawn)

def test_advancing_the_round_draws_round_two_from_standings(
    fresh_fabric_with_domain,
):
    import random as _random
    from bin import generate_cards, swiss

    domain_id, payloads = _wheel_domain(count=9)
    generate_cards._enqueue_pairs(domain_id, payloads, _random.Random(3))
    first = _pending_rows(fresh_fabric_with_domain)
    _resolve(first)

    drawn = generate_cards.advance_round(domain_id)
    assert drawn["round_drawn"] == 2
    assert drawn["pairs_enqueued"] == 4
    assert len(drawn["byes"]) == 1
    assert drawn["rounds_cap"] == 4, "nine items play ceil(log2 9) rounds"

    rows = _pending_rows(fresh_fabric_with_domain)
    second = [p for p in _payloads_of(rows) if p["round"] == 2]
    assert [p["label"] for p in second] == ["R2-5", "R2-6", "R2-7", "R2-8"]
    keys = [p["pair_key"] for p in _payloads_of(rows)]
    assert len(keys) == len(set(keys)) == 8, "no pair is asked twice"

    first_bye = {b["item_id"] for p in _payloads_of(first) for b in p["byes"]}
    assert first_bye.isdisjoint(set(drawn["byes"])), (
        "the bye moves on while an item has not had one"
    )

    conn = sqlite3.connect(str(fresh_fabric_with_domain))
    conn.row_factory = sqlite3.Row
    cfg = generate_cards._human_config_for_rubric(conn, domain_id)
    pool = generate_cards._load_pool(conn, domain_id, cfg)
    conn.close()
    table = swiss.standings(pool)
    assert len(table) == 9
    assert [s.points for s in table] == [3, 3, 3, 3, 0, 0, 0, 0, 0]
    winners = {p["item_a"] for p in _payloads_of(first)}
    assert {s.item_id for s in table[:4]} == winners, (
        "round two is drawn from the points table, not from a fresh shuffle"
    )
    crossing = [
        p for p in second
        if (p["item_a"] in winners) != (p["item_b"] in winners)
    ]
    assert len(crossing) <= 2, (
        "pairing is standing-driven, so a cross-group pair happens only where "
        "parity forces a float: the round-one bye has played nothing and is "
        "seated ahead of the leaders, which pulls one winner up and leaves an "
        "odd winner group that must float one down. More than two means the "
        f"draw is not following the points table; got {len(crossing)}"
    )
    assert first_bye & set().union(
        *[{p["item_a"], p["item_b"]} for p in crossing]
    ), "the round-one bye is one of the forced crossings"

def test_advancing_an_unfinished_round_refuses_loudly(fresh_fabric_with_domain):
    import random as _random
    import pytest as _pytest
    from bin import generate_cards

    domain_id, payloads = _wheel_domain(count=9)
    generate_cards._enqueue_pairs(domain_id, payloads, _random.Random(3))
    rows = _pending_rows(fresh_fabric_with_domain)
    _resolve(rows[:3])

    with _pytest.raises(RuntimeError, match="1 judgement"):
        generate_cards.advance_round(domain_id)

    assert len(_pending_rows(fresh_fabric_with_domain)) == 4, (
        "a refused advance enqueues nothing"
    )

def test_advancing_a_domain_with_no_round_refuses(fresh_fabric_with_domain):
    import pytest as _pytest
    from bin import generate_cards

    domain_id, _payloads = _wheel_domain(count=9)
    with _pytest.raises(RuntimeError, match="no enqueued round"):
        generate_cards.advance_round(domain_id)

def _drain(fabric, verdict="a-wins"):
    """Judge every pending row, so the next advance is not refused."""
    rows = _pending_rows(fabric, status="pending")
    assert rows, "nothing was pending: the round under test enqueued nothing"
    _resolve(rows, verdict=verdict)
    return len(rows)

def _pool_of(fabric, domain_id):
    from bin import generate_cards

    conn = sqlite3.connect(str(fabric))
    conn.row_factory = sqlite3.Row
    try:
        cfg = generate_cards._human_config_for_rubric(conn, domain_id)
        return generate_cards._load_pool(conn, domain_id, cfg)
    finally:
        conn.close()

def test_advancing_past_the_last_round_reports_a_settled_pool(
    fresh_fabric_with_domain,
):
    """The cap is the difference between finished and stuck: past
    ceil(log2 N) every pairing is a rematch, so the pre-fix draw stranded the
    whole pool as byes and looked identical to a jam."""
    import random as _random
    from bin import generate_cards

    domain_id, payloads = _wheel_domain(count=4)
    generate_cards._enqueue_pairs(domain_id, payloads, _random.Random(3))
    assert _drain(fresh_fabric_with_domain) == 2

    second = generate_cards.advance_round(domain_id)
    assert second["status"] == generate_cards.ROUND_DRAWN
    assert second["round_drawn"] == 2 and second["pairs_enqueued"] == 2
    assert second["last_round"] is True, "four items play ceil(log2 4) = 2 rounds"
    assert second["settled"] is False, "a drawn round is not a settled pool"
    assert _drain(fresh_fabric_with_domain) == 2

    before = len(_pending_rows(fresh_fabric_with_domain))
    done = generate_cards.advance_round(domain_id)
    assert done["status"] == generate_cards.ROUND_COMPLETE
    assert done["status"] in generate_cards.TERMINAL_ROUND_STATUSES
    assert done["settled"] is True
    assert done["pairs_enqueued"] == 0 and done["byes"] == []
    assert done["rounds_played"] == 2 and done["rounds_cap"] == 2
    assert "finished" in done["reason"] and "full ordering" in done["reason"]

    rows = _pending_rows(fresh_fabric_with_domain)
    assert len(rows) == before == 4, "a settled pool enqueues nothing"
    assert max(p["round"] for p in _payloads_of(rows)) == 2, (
        "no round 3 exists to be drawn as an all-bye round"
    )
    again = generate_cards.advance_round(domain_id)
    assert again["status"] == generate_cards.ROUND_COMPLETE, (
        "asking a finished tournament again is answered, not an error"
    )
    assert len(_pending_rows(fresh_fabric_with_domain)) == 4

def test_a_campaign_can_stop_early_for_a_coarser_ordering(
    fresh_fabric_with_domain,
):
    """Rounds are a dial. A campaign that stops at 2 of 4 gets less
    resolution and a complete, valid points table -- never an error."""
    import random as _random
    from bin import generate_cards, swiss

    domain_id, payloads = _wheel_domain(count=9)
    generate_cards._enqueue_pairs(domain_id, payloads, _random.Random(3))
    _drain(fresh_fabric_with_domain)

    second = generate_cards.advance_round(domain_id, rounds=2)
    assert second["status"] == generate_cards.ROUND_DRAWN
    assert second["rounds_cap"] == 2 and second["rounds_full"] == 4
    assert second["last_round"] is True
    _drain(fresh_fabric_with_domain)

    done = generate_cards.advance_round(domain_id)
    assert done["status"] == generate_cards.ROUND_COMPLETE, (
        "the dial has a home of its own on the domain, so the cap survives an "
        "advance that does not repeat the flag"
    )
    assert done["rounds_cap"] == 2 and done["rounds_full"] == 4
    assert "coarser ordering" in done["reason"]

    table = swiss.standings(_pool_of(fresh_fabric_with_domain, domain_id))
    assert len(table) == 9, "stopping early leaves nobody out of the table"
    assert sum(s.points for s in table) == 24, (
        "eight decided matches at three points each: the coarse table is the "
        "same arithmetic, just less of it"
    )
    assert [s.points for s in table] == sorted(
        (s.points for s in table), reverse=True)
    assert sum(1 for s in table if s.played) == 9, (
        "the round-one bye played in round two: nine items, nobody idle twice"
    )
    assert sum(s.played for s in table) == 16 and sum(s.byes for s in table) == 2
    assert all(s.rank > 0 for s in table if s.played)

def test_a_pool_out_of_comparisons_reports_exhausted_not_finished(
    fresh_fabric_with_domain,
):
    """Three items hold three comparisons. Under a cap of five the pool runs
    dry BEFORE the cap: that is terminal too, and it is not the same
    statement as 'the campaign played every round it asked for'."""
    import random as _random
    from bin import generate_cards

    domain_id, payloads = _wheel_domain(count=3)
    generate_cards._enqueue_pairs(domain_id, payloads, _random.Random(3))
    assert _drain(fresh_fabric_with_domain) == 1

    for expected in (2, 3):
        drawn = generate_cards.advance_round(domain_id, rounds=5)
        assert drawn["status"] == generate_cards.ROUND_DRAWN
        assert drawn["round_drawn"] == expected and drawn["pairs_enqueued"] == 1
        assert _drain(fresh_fabric_with_domain) == 1

    out = generate_cards.advance_round(domain_id)
    assert out["status"] == generate_cards.ROUND_EXHAUSTED
    assert out["status"] in generate_cards.TERMINAL_ROUND_STATUSES
    assert out["rounds_played"] == 3 and out["rounds_cap"] == 5
    assert out["pairs_enqueued"] == 0
    assert "out of comparisons" in out["reason"]
    assert "not stalled" in out["reason"]

    rows = _pending_rows(fresh_fabric_with_domain)
    keys = [p["pair_key"] for p in _payloads_of(rows)]
    assert len(keys) == len(set(keys)) == 3, (
        "every one of the three pairs was asked exactly once"
    )

def test_a_draw_that_seats_no_match_reports_stuck_not_finished(
    fresh_fabric_with_domain, monkeypatch,
):
    """Under the cap with pairs left, an empty draw is a pairing failure. It
    must never reach the queue as a round of byes."""
    import random as _random
    from bin import generate_cards, swiss

    domain_id, payloads = _wheel_domain(count=9)
    generate_cards._enqueue_pairs(domain_id, payloads, _random.Random(3))
    _drain(fresh_fabric_with_domain)

    monkeypatch.setattr(
        swiss, "pair_round",
        lambda pool, number: swiss.Round(number=number, matches=[],
                                         byes=swiss.active_ids(pool)),
    )
    stuck = generate_cards.advance_round(domain_id)
    assert stuck["status"] == generate_cards.ROUND_STUCK
    assert stuck["status"] in generate_cards.TERMINAL_ROUND_STATUSES
    assert stuck["pairs_enqueued"] == 0 and stuck["byes"] == []
    assert "pairing failure" in stuck["reason"]
    assert "unjudged pair(s) remain" in stuck["reason"]
    assert len(_pending_rows(fresh_fabric_with_domain)) == 4, (
        "a stuck draw enqueues nothing at all, least of all an all-bye round"
    )

def test_a_cap_below_one_is_refused_before_anything_is_written(
    fresh_fabric_with_domain,
):
    import random as _random
    import pytest as _pytest
    from bin import generate_cards

    domain_id, payloads = _wheel_domain(count=4)
    with _pytest.raises(ValueError, match="at least 1"):
        generate_cards._enqueue_pairs(domain_id, payloads, _random.Random(3),
                                      rounds=0)
    assert _pending_rows(fresh_fabric_with_domain) == []

    generate_cards._enqueue_pairs(domain_id, payloads, _random.Random(3))
    _drain(fresh_fabric_with_domain)
    with _pytest.raises(ValueError, match="at least 1"):
        generate_cards.advance_round(domain_id, rounds=-2)
    assert len(_pending_rows(fresh_fabric_with_domain)) == 2, (
        "a refused cap draws no round"
    )

def test_the_advance_cli_separates_a_finished_pool_from_a_stuck_one(
    fresh_fabric_with_domain, monkeypatch, capsys,
):
    import types
    import pytest as _pytest
    from bin import generate_cards

    monkeypatch.setattr(generate_cards._domains, "get_domain",
                        lambda name: types.SimpleNamespace(id=7, name=name))
    seen = {}

    def _terminal(status):
        def fake(domain_id, *, rubric=None, rounds=None):
            seen["domain_id"] = domain_id
            seen["rounds"] = rounds
            return {"status": status, "rounds_played": 3, "rounds_cap": 3,
                    "reason": "because"}
        return fake

    monkeypatch.setattr(generate_cards, "advance_round",
                        _terminal(generate_cards.ROUND_COMPLETE))
    monkeypatch.setattr(
        "sys.argv",
        ["generate-cards", "--domain", "d", "--advance-round", "--rounds", "3"],
    )
    generate_cards.main()
    assert seen == {"domain_id": 7, "rounds": 3}
    assert "COMPLETE after round 3/3: because" in capsys.readouterr().out

    codes = {}
    for status in (generate_cards.ROUND_EXHAUSTED, generate_cards.ROUND_STUCK):
        monkeypatch.setattr(generate_cards, "advance_round", _terminal(status))
        with _pytest.raises(SystemExit) as exc:
            generate_cards.main()
        codes[status] = exc.value.code
        assert exc.value.code, (
            f"{status} is terminal but not success: an operator polling the "
            "CLI must be able to tell it from a finished tournament"
        )
        assert status.upper() in capsys.readouterr().out
    assert codes[generate_cards.ROUND_EXHAUSTED] == 3
    assert codes[generate_cards.ROUND_STUCK] == 4, (
        "a pool that ran out of comparisons is a finished ordering; a draw "
        "that seated no match is broken and wants a person. One exit code "
        "for both makes the CLI unable to say which happened"
    )

    def drawn(domain_id, *, rubric=None, rounds=None):
        return {"status": generate_cards.ROUND_DRAWN, "round_drawn": 2,
                "rounds_cap": 3, "pairs_enqueued": 4, "byes": ["x"],
                "discarded": []}

    monkeypatch.setattr(generate_cards, "advance_round", drawn)
    generate_cards.main()
    assert "[advance] round 2/3: 4 pair(s), 1 bye, 0 discarded" in (
        capsys.readouterr().out)

def _write_from_another_connection_refused(fabric) -> bool:
    """True when an independent connection cannot write this database right
    now — i.e. somebody is holding the write lock."""
    other = sqlite3.connect(str(fabric), timeout=0.05)
    try:
        other.execute(
            "UPDATE pending_judgement SET error_message='lock probe' "
            "WHERE id=(SELECT MIN(id) FROM pending_judgement)"
        )
        other.commit()
        return False
    except sqlite3.OperationalError as exc:
        return "locked" in str(exc)
    finally:
        other.close()

def _human_config_id(fabric) -> int:
    db = sqlite3.connect(str(fabric))
    try:
        return db.execute(
            "SELECT id FROM job_configuration WHERE rater_type='human' "
            "AND status='active' LIMIT 1"
        ).fetchone()[0]
    finally:
        db.close()

def _raw_pending(fabric, domain_id, match_id, payload, status="done"):
    """Insert a queue row the way another writer would, bypassing the draw."""
    db = sqlite3.connect(str(fabric))
    try:
        db.execute(
            "INSERT INTO pending_judgement(config_id, tournament_db_path, "
            "match_id, trace_payload, domain_id, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_human_config_id(fabric), f"domain:{domain_id}", match_id,
             json.dumps(payload), domain_id, status),
        )
        db.commit()
    finally:
        db.close()

def test_the_whole_round_decision_is_one_write_locked_transaction(
    fresh_fabric_with_domain, monkeypatch,
):
    """Two operators advancing at once, or the open round's last verdict
    landing mid-decision, both come from reading unlocked. The lock has to be
    held from the FIRST read of the decision, not taken at the write."""
    import random as _random
    from bin import generate_cards, swiss

    domain_id, payloads = _wheel_domain(count=9)
    generate_cards._enqueue_pairs(domain_id, payloads, _random.Random(3))
    _drain(fresh_fabric_with_domain)

    assert not _write_from_another_connection_refused(fresh_fabric_with_domain), (
        "the probe has to be able to write when nobody holds the lock, or it "
        "proves nothing while somebody does"
    )

    probed = {}
    real_load = generate_cards._load_pool
    real_pair = swiss.pair_round

    def watched_load(conn, did, cfg):
        probed["at_the_pool_read"] = _write_from_another_connection_refused(
            fresh_fabric_with_domain)
        return real_load(conn, did, cfg)

    def watched_pair(pool, number):
        probed["at_the_draw"] = _write_from_another_connection_refused(
            fresh_fabric_with_domain)
        return real_pair(pool, number)

    monkeypatch.setattr(generate_cards, "_load_pool", watched_load)
    monkeypatch.setattr(swiss, "pair_round", watched_pair)
    out = generate_cards.advance_round(domain_id)

    assert out["status"] == generate_cards.ROUND_DRAWN
    assert probed == {"at_the_pool_read": True, "at_the_draw": True}, (
        "the standings read, the outstanding count and the write are one "
        "decision: a verdict that lands between them makes the guard pass on "
        "standings that are already stale, and a second operator reading the "
        f"same instant draws the same round twice. Got {probed}"
    )

def test_a_round_cannot_be_written_outside_the_round_decision(
    fresh_fabric_with_domain,
):
    import pytest as _pytest
    from bin import generate_cards, swiss

    domain_id, payloads = _wheel_domain(count=4)
    conn = sqlite3.connect(str(fresh_fabric_with_domain))
    conn.row_factory = sqlite3.Row
    try:
        cfg = generate_cards._human_config_for_rubric(conn, domain_id)
        pool = swiss.new_pool(
            (swiss.item_from_payload(p) for p in payloads),
            rubric_id=cfg["name"], rubric_version=int(cfg["template_version"]),
        )
        drawn = swiss.pair_round(pool, 1)
        with _pytest.raises(AssertionError, match="one database"):
            generate_cards._write_round(conn, cfg, domain_id, pool, drawn, 1)
        with _pytest.raises(AssertionError, match="one database"):
            generate_cards._next_match_id(conn, domain_id)
    finally:
        conn.close()
    assert _pending_rows(fresh_fabric_with_domain) == [], (
        "a match id allocated outside the lock is a match id two draws can "
        "both take"
    )

def test_two_rows_of_one_domain_cannot_share_a_match_id(
    fresh_fabric_with_domain,
):
    import random as _random
    import pytest as _pytest
    from bin import generate_cards

    domain_id, payloads = _wheel_domain(count=4)
    generate_cards._enqueue_pairs(domain_id, payloads, _random.Random(3))
    taken = _pending_rows(fresh_fabric_with_domain)[0]["match_id"]

    with _pytest.raises(sqlite3.IntegrityError):
        _raw_pending(fresh_fabric_with_domain, domain_id, taken,
                     {"label": "collision"}, status="pending")

    config_id = _human_config_id(fresh_fabric_with_domain)
    db = sqlite3.connect(str(fresh_fabric_with_domain))
    try:
        for _ in range(2):
            db.execute(
                "INSERT INTO pending_judgement(config_id, tournament_db_path, "
                "match_id, trace_payload) VALUES (?, ?, ?, ?)",
                (config_id, "/tmp/tournament.db", taken, "{}"),
            )
        db.commit()
        assert db.execute(
            "SELECT COUNT(*) FROM pending_judgement "
            "WHERE domain_id IS NULL AND match_id=?", (taken,)
        ).fetchone()[0] == 2, (
            "the tournament path writes one row per active config under one "
            "match id and has no domain: uniqueness is per domain, and its "
            "NULLs stay distinct"
        )
    finally:
        db.close()

def test_a_queue_that_already_collides_names_the_rows_it_cannot_key(
    fresh_fabric_with_domain,
):
    """A database written before the index existed can already hold the
    collision. That must arrive as a sentence naming the rows, not as a bare
    SQLite error out of a CREATE INDEX."""
    import pytest as _pytest
    from bin import generate_cards

    domain_id, payloads = _wheel_domain(count=4)
    body = {"round": 1, "label": "R1-1", "card_a": payloads[0],
            "card_b": payloads[1], "item_a": "a", "item_b": "b", "byes": []}
    for _ in range(2):
        _raw_pending(fresh_fabric_with_domain, domain_id, 0, body)

    with _pytest.raises(RuntimeError, match=f"domain {domain_id} match_id 0"):
        generate_cards.advance_round(domain_id)

def test_a_pool_with_fewer_than_two_entrants_is_reported_not_crashed(
    fresh_fabric_with_domain,
):
    """rounds_total is 0 below two entrants, and 0 is not a legal cap
    OVERRIDE. Feeding the default back through the override path turned a
    one-item pool into a ValueError out of the engine."""
    from bin import generate_cards

    domain_id, payloads = _wheel_domain(count=1)
    _raw_pending(fresh_fabric_with_domain, domain_id, 0,
                 {"round": 1, "label": "R1-1", "card_a": payloads[0],
                  "item_a": "solo", "byes": []})

    out = generate_cards.advance_round(domain_id)
    assert out["status"] == generate_cards.ROUND_COMPLETE
    assert out["rounds_cap"] == 0 and out["rounds_full"] == 0
    assert out["pairs_enqueued"] == 0
    assert "fewer than two entrants" in out["reason"]
    assert len(_pending_rows(fresh_fabric_with_domain)) == 1, (
        "nothing is drawn for a pool with nobody to compare"
    )

def test_the_cap_is_sticky_the_moment_it_is_set_even_drawing_nothing(
    fresh_fabric_with_domain,
):
    """--rounds at the moment the campaign is already there draws no round,
    so a dial persisted only by a draw was never written at all and the next
    flagless poll drew past it."""
    import random as _random
    from bin import generate_cards

    domain_id, payloads = _wheel_domain(count=9)
    generate_cards._enqueue_pairs(domain_id, payloads, _random.Random(3))
    _drain(fresh_fabric_with_domain)
    assert generate_cards.advance_round(domain_id)["round_drawn"] == 2
    _drain(fresh_fabric_with_domain)

    done = generate_cards.advance_round(domain_id, rounds=2)
    assert done["status"] == generate_cards.ROUND_COMPLETE
    assert done["rounds_cap"] == 2 and done["rounds_full"] == 4

    poll = generate_cards.advance_round(domain_id)
    assert poll["status"] == generate_cards.ROUND_COMPLETE, (
        "the cap was set on a call that wrote no rows; a dial that only "
        "reaches disk inside a draw is lost there, and the next poll draws "
        "round 3 of a campaign that reported itself finished"
    )
    assert poll["rounds_cap"] == 2
    assert max(p["round"] for p in
               _payloads_of(_pending_rows(fresh_fabric_with_domain))) == 2

def test_the_round_cap_lives_on_the_domain_and_never_in_the_judge_payload(
    fresh_fabric_with_domain,
):
    import random as _random
    from bin import generate_cards

    domain_id, payloads = _wheel_domain(count=9)
    generate_cards._enqueue_pairs(domain_id, payloads, _random.Random(3),
                                  rounds=2)

    db = sqlite3.connect(str(fresh_fabric_with_domain))
    try:
        assert db.execute(
            "SELECT rounds_cap FROM domain_campaign WHERE domain_id=?",
            (domain_id,),
        ).fetchone()[0] == 2, "the dial has a home of its own"
    finally:
        db.close()

    _drain(fresh_fabric_with_domain)
    drawn = generate_cards.advance_round(domain_id)
    assert drawn["rounds_cap"] == 2 and drawn["last_round"] is True

    for payload in _payloads_of(_pending_rows(fresh_fabric_with_domain)):
        assert "rounds_cap" not in json.dumps(payload), (
            "the trace payload is the judging surface: a person is shown it, "
            "and tournament state does not ride in what a person is shown"
        )

def test_a_cap_smuggled_in_old_payloads_is_abandoned_out_loud(
    fresh_fabric_with_domain, capsys,
):
    """The retired store was the display payload itself. Rows written that
    way are not read back — and the operator is told so, by domain, once."""
    import random as _random
    from bin import generate_cards

    domain_id, payloads = _wheel_domain(count=9)
    generate_cards._enqueue_pairs(domain_id, payloads, _random.Random(3))
    db = sqlite3.connect(str(fresh_fabric_with_domain))
    db.row_factory = sqlite3.Row
    try:
        for row in db.execute(
            "SELECT id, trace_payload FROM pending_judgement WHERE domain_id=?",
            (domain_id,),
        ).fetchall():
            legacy = dict(json.loads(row["trace_payload"]), rounds_cap=2)
            db.execute("UPDATE pending_judgement SET trace_payload=? WHERE id=?",
                       (json.dumps(legacy), row["id"]))
        db.commit()
    finally:
        db.close()
    _drain(fresh_fabric_with_domain)
    capsys.readouterr()

    drawn = generate_cards.advance_round(domain_id)
    assert drawn["rounds_cap"] == 4, (
        "the payload cap is abandoned, not honoured: this campaign is back on "
        "the default ceil(log2 9) = 4"
    )
    announced = capsys.readouterr().err
    assert "ABANDONED ROUND CAP" in announced
    assert f"domain {domain_id}" in announced and "rounds_cap=2" in announced
    assert "--rounds 2" in announced, (
        "a reset a person could mistake for data loss has to say how to get "
        "the ordering back"
    )

    _drain(fresh_fabric_with_domain)
    generate_cards.advance_round(domain_id)
    assert "ABANDONED" not in capsys.readouterr().err, (
        "the notice is loud once per domain, not on every poll forever"
    )

def test_a_discarded_item_takes_its_outstanding_rows_with_it(
    fresh_fabric_with_domain,
):
    """The withdrawal runs on the decision's own connection: the outstanding
    count that gates the draw has to see it, and a second connection would
    block against the lock this very call is holding."""
    import random as _random
    from bin import generate_cards

    domain_id, payloads = _wheel_domain(count=9)
    generate_cards._enqueue_pairs(domain_id, payloads, _random.Random(3))
    rows = _pending_rows(fresh_fabric_with_domain, status="pending")
    ejected = json.loads(rows[0]["trace_payload"])["item_a"]
    _resolve(rows[:1], verdict="discard-a")
    _resolve(rows[1:], verdict="a-wins")

    stray = dict(json.loads(rows[0]["trace_payload"]), label="R1-stray")
    _raw_pending(fresh_fabric_with_domain, domain_id, 99, stray,
                 status="pending")

    out = generate_cards.advance_round(domain_id)
    assert out["status"] == generate_cards.ROUND_DRAWN
    assert out["discarded"] == [ejected]

    db = sqlite3.connect(str(fresh_fabric_with_domain))
    try:
        status, why = db.execute(
            "SELECT status, error_message FROM pending_judgement "
            "WHERE domain_id=? AND match_id=99", (domain_id,)
        ).fetchone()
    finally:
        db.close()
    assert status == "cancelled" and "discarded" in why, (
        "a judge who threw an item out keeps being shown it otherwise, and "
        "the row it sits in blocks every later advance"
    )

def _report_for_every_status(fabric, monkeypatch):
    import random as _random
    from bin import generate_cards, swiss

    reports = {}
    settled_id, settled = _wheel_domain(name="shape-settled", count=4)
    generate_cards._enqueue_pairs(settled_id, settled, _random.Random(3))
    _drain(fabric)
    reports[generate_cards.ROUND_DRAWN] = generate_cards.advance_round(settled_id)
    _drain(fabric)
    reports[generate_cards.ROUND_COMPLETE] = generate_cards.advance_round(settled_id)

    dry_id, dry = _wheel_domain(name="shape-dry", count=3)
    generate_cards._enqueue_pairs(dry_id, dry, _random.Random(3))
    _drain(fabric)
    for _ in range(2):
        generate_cards.advance_round(dry_id, rounds=5)
        _drain(fabric)
    reports[generate_cards.ROUND_EXHAUSTED] = generate_cards.advance_round(dry_id)

    jam_id, jam = _wheel_domain(name="shape-jam", count=9)
    generate_cards._enqueue_pairs(jam_id, jam, _random.Random(3))
    _drain(fabric)
    monkeypatch.setattr(
        swiss, "pair_round",
        lambda pool, number: swiss.Round(number=number, matches=[],
                                         byes=swiss.active_ids(pool)),
    )
    reports[generate_cards.ROUND_STUCK] = generate_cards.advance_round(jam_id)
    monkeypatch.undo()
    return reports

def test_every_round_status_answers_in_one_shape(
    fresh_fabric_with_domain, monkeypatch,
):
    """One shape, and 'round' no longer means the round drawn on one path and
    the last round played on the other."""
    from bin import generate_cards

    reports = _report_for_every_status(fresh_fabric_with_domain, monkeypatch)
    assert set(reports) == generate_cards.TERMINAL_ROUND_STATUSES | {
        generate_cards.ROUND_DRAWN}

    shapes = {status: tuple(sorted(r)) for status, r in reports.items()}
    assert len(set(shapes.values())) == 1, (
        f"every answer has to carry the same keys; got {shapes}"
    )

    assert reports[generate_cards.ROUND_DRAWN]["round_drawn"] == 2
    for status in generate_cards.TERMINAL_ROUND_STATUSES:
        assert reports[status]["round_drawn"] is None, (
            f"{status} put no round on the queue, so it names none"
        )
        assert reports[status]["pairs_enqueued"] == 0
        assert reports[status]["reason"]
    assert reports[generate_cards.ROUND_DRAWN]["reason"], (
        "a drawn round explains itself in the same field the terminal ones do"
    )

def test_stuck_is_neither_settled_nor_the_same_state_as_a_dry_pool(
    fresh_fabric_with_domain, monkeypatch,
):
    from bin import generate_cards

    reports = _report_for_every_status(fresh_fabric_with_domain, monkeypatch)
    assert reports[generate_cards.ROUND_COMPLETE]["settled"] is True
    assert reports[generate_cards.ROUND_EXHAUSTED]["settled"] is True, (
        "out of comparisons means the standings are final"
    )
    assert reports[generate_cards.ROUND_STUCK]["settled"] is False, (
        "a draw that seated no match while unjudged pairs remain is a pairing "
        "failure; reporting it settled says the tournament finished"
    )
    assert reports[generate_cards.ROUND_STUCK]["last_round"] is False
    assert reports[generate_cards.ROUND_DRAWN]["settled"] is False

    exits = {status: generate_cards.ROUND_STATUS_EXIT_CODE[status]
             for status in reports}
    assert exits[generate_cards.ROUND_COMPLETE] == 0
    assert exits[generate_cards.ROUND_EXHAUSTED] != exits[
        generate_cards.ROUND_STUCK], (
        "sharing exit 3 collapses a benign end into a broken one for whoever "
        "is polling the CLI"
    )

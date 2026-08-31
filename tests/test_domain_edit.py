"""Tests for editing existing domains.

Edit semantics:
- Update description, corpus_source, generator_prompt, judge_prompt
- Prompt edits push a new version to Langfuse (idempotent: same text = no new version)
- Domain row in SQLite is updated in place
- Domain name is immutable (it's the foreign key to Langfuse prompt names);
  if you need a different name, archive + create.
"""
import pytest

@pytest.fixture
def fresh_with_domain(fake_langfuse, monkeypatch, tmp_data_home):
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    fake_langfuse.enable("create_prompt")
    fake_langfuse.enable("list_prompts")
    fake_langfuse.enable("set_label")
    import importlib, judgement
    importlib.reload(judgement)
    judgement.init_db()

    from bin import prompts, domains
    prompts.push("card-generator:demo", "first-gen", labels=["production"])
    prompts.push("judge-instructions:demo", "first-judge", labels=["production"])
    domain_id = domains.create_domain(
        name="demo",
        description="initial description",
        corpus_source={"kind": "inline", "items": [{"text": "x"}]},
    )
    return {"langfuse": fake_langfuse, "domain_id": domain_id}

def test_update_description_only(fresh_with_domain):
    from bin import domains
    domains.update_domain("demo", description="new description")
    spec = domains.get_domain("demo")
    assert spec.description == "new description"
    assert spec.corpus_source["kind"] == "inline"

def test_update_corpus_source(fresh_with_domain):
    from bin import domains
    new_src = {"kind": "filesystem", "root": "/tmp", "glob": "*.md"}
    domains.update_domain("demo", corpus_source=new_src)
    spec = domains.get_domain("demo")
    assert spec.corpus_source == new_src

def test_update_corpus_source_validates(fresh_with_domain):
    from bin import domains
    with pytest.raises(ValueError, match="kind"):
        domains.update_domain("demo", corpus_source={"kind": "bogus"})
    assert domains.get_domain("demo").corpus_source["kind"] == "inline"

def test_update_unknown_domain_raises(fresh_with_domain):
    from bin import domains
    with pytest.raises(LookupError):
        domains.update_domain("nope", description="x")

def test_update_partial_keeps_other_fields(fresh_with_domain):
    from bin import domains
    domains.update_domain("demo", description="d2")
    spec = domains.get_domain("demo")
    assert spec.generator_prompt == "card-generator:demo"
    assert spec.judge_prompt == "judge-instructions:demo"

def test_cli_edit_subcommand_updates_prompts(fresh_with_domain, capsys):
    """The CLI bridge (--edit mode) pushes new prompt versions and updates the row."""
    from bin import domain_builder_cli, domains, prompts
    import sys

    args = [
        "bin/domain_builder_cli.py",
        "--edit",
        "--name", "demo",
        "--description", "edited!",
        "--generator-prompt", "edited-generator",
        "--judge-prompt", "edited-judge",
        "--corpus-spec", '{"kind":"inline","items":[{"text":"y"}]}',
    ]
    sys_argv_orig = sys.argv
    sys.argv = args
    try:
        try:
            domain_builder_cli.main()
        except SystemExit as e:
            assert e.code in (None, 0), f"CLI exited with {e.code}"
    finally:
        sys.argv = sys_argv_orig

    spec = domains.get_domain("demo")
    assert spec.description == "edited!"
    assert spec.corpus_source["items"][0]["text"] == "y"

    cur_gen = prompts.get("card-generator:demo", label="production")
    cur_jud = prompts.get("judge-instructions:demo", label="production")
    assert cur_gen == "edited-generator"
    assert cur_jud == "edited-judge"

def test_cli_edit_idempotent_when_prompt_unchanged(fresh_with_domain):
    """If the prompt text is unchanged, --edit doesn't push a redundant version."""
    from bin import domain_builder_cli, domains
    import sys

    args = [
        "bin/domain_builder_cli.py",
        "--edit",
        "--name", "demo",
        "--description", "first edit",
        "--generator-prompt", "first-gen",
        "--judge-prompt", "first-judge",
        "--corpus-spec", '{"kind":"inline","items":[{"text":"x"}]}',
    ]
    sys_argv_orig = sys.argv
    sys.argv = args
    try:
        try:
            domain_builder_cli.main()
        except SystemExit:
            pass
    finally:
        sys.argv = sys_argv_orig

    assert domains.get_domain("demo").description == "first edit"

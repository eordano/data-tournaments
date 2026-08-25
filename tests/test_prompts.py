"""Tests for bin/prompts.py — Langfuse Prompts wrapper."""
import pytest


def test_get_returns_prompt_text_for_label(fake_langfuse, monkeypatch):
    fake_langfuse.add_prompt("judge-instructions", text="Be a fair judge.", version=1, labels=["production"])
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    from bin import prompts
    assert prompts.get("judge-instructions") == "Be a fair judge."


def test_get_defaults_to_production_label(fake_langfuse, monkeypatch):
    fake_langfuse.add_prompt("p", text="v1", version=1, labels=["latest"])
    fake_langfuse.add_prompt("p", text="v2", version=2, labels=["production"])
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    from bin import prompts
    assert prompts.get("p") == "v2"


def test_get_with_explicit_label(fake_langfuse, monkeypatch):
    fake_langfuse.add_prompt("p", text="v1", version=1, labels=["candidate"])
    fake_langfuse.add_prompt("p", text="v2", version=2, labels=["production"])
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    from bin import prompts
    assert prompts.get("p", label="candidate") == "v1"


def test_get_raises_lookup_error_when_missing(fake_langfuse, monkeypatch):
    fake_langfuse.enable("get_prompt")
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    from bin import prompts
    with pytest.raises(LookupError, match="judge-instructions"):
        prompts.get("judge-instructions")


def test_push_creates_first_version_with_labels(fake_langfuse, monkeypatch):
    fake_langfuse.enable("create_prompt")
    fake_langfuse.enable("list_prompts")
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    from bin import prompts
    v = prompts.push("judge-instructions", "Be fair.", labels=["production"])
    assert v == 1
    assert fake_langfuse.get_prompt("judge-instructions", label="production").prompt == "Be fair."


def test_push_increments_version_when_text_changes(fake_langfuse, monkeypatch):
    fake_langfuse.enable("create_prompt")
    fake_langfuse.enable("list_prompts")
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    from bin import prompts
    v1 = prompts.push("j", "v1 text", labels=["production"])
    v2 = prompts.push("j", "v2 text", labels=["candidate"])
    assert (v1, v2) == (1, 2)


def test_push_is_idempotent_when_text_unchanged(fake_langfuse, monkeypatch):
    fake_langfuse.enable("create_prompt")
    fake_langfuse.enable("list_prompts")
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    from bin import prompts
    v1 = prompts.push("j", "same text", labels=["production"])
    v2 = prompts.push("j", "same text", labels=["production"])
    assert v1 == v2 == 1
    assert fake_langfuse.versions("j") == [1]


def test_set_label_moves_label_between_versions(fake_langfuse, monkeypatch):
    fake_langfuse.add_prompt("j", text="v1", version=1, labels=["production"])
    fake_langfuse.add_prompt("j", text="v2", version=2, labels=["candidate"])
    fake_langfuse.enable("set_label")
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    from bin import prompts
    prompts.set_label("j", version=2, label="production")
    assert prompts.get("j", label="production") == "v2"
    assert "production" not in fake_langfuse.get_prompt("j", version=1).labels


def test_list_returns_metadata_with_production_version(fake_langfuse, monkeypatch):
    fake_langfuse.add_prompt("a", text="va", version=1, labels=["production"])
    fake_langfuse.add_prompt("b", text="vb1", version=1, labels=[])
    fake_langfuse.add_prompt("b", text="vb2", version=2, labels=["production", "candidate"])
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    from bin import prompts
    result = prompts.list()
    by_name = {p.name: p for p in result}
    assert set(by_name) == {"a", "b"}
    assert by_name["b"].production_version == 2
    assert "candidate" in by_name["b"].labels


def test_get_makes_one_roundtrip(fake_langfuse, monkeypatch):
    fake_langfuse.add_prompt("j", text="hello", version=1, labels=["production"])
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    from bin import prompts
    fake_langfuse.reset_call_log()
    prompts.get("j")
    assert fake_langfuse.call_count("get_prompt") == 1


def test_local_store_supports_prompt_lifecycle(tmp_data_home, monkeypatch):
    monkeypatch.setenv("PROMPT_BACKEND", "local")
    from bin import prompts

    v1 = prompts.push("local-judge", "first", labels=["production"])
    v2 = prompts.push("local-judge", "second", labels=["candidate"])

    assert (v1, v2) == (1, 2)
    assert prompts.get("local-judge", "production") == "first"
    assert prompts.get("local-judge", "candidate") == "second"

    prompts.set_label("local-judge", v2, "production")
    assert prompts.get("local-judge", "production") == "second"
    info = prompts.list()[0]
    assert info.name == "local-judge"
    assert info.production_version == 2

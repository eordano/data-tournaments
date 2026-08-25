"""Tests for bin/generators/builder.py — DomainBuilder DSPy program."""
import dspy
import pytest

from tests.conftest import _scripted_lm


@pytest.fixture
def builder_prompt(fake_langfuse, monkeypatch):
    fake_langfuse.add_prompt(
        "domain-builder",
        text="You design new card-prioritization domains. Given a description, draft prompts.",
        version=1,
        labels=["production"],
    )
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    return fake_langfuse


def test_signature_inputs_outputs():
    from bin.generators.builder import DomainBuilderSig
    inputs = set(DomainBuilderSig.input_fields)
    outputs = set(DomainBuilderSig.output_fields)
    assert {"description", "corpus_kind", "corpus_samples"} <= inputs
    assert {"domain_name", "generator_prompt", "judge_prompt"} <= outputs


def test_builder_loads_meta_prompt(builder_prompt):
    from bin.generators.builder import DomainBuilder
    b = DomainBuilder()
    assert "domains" in b.signature.instructions.lower()


def test_builder_uses_seed_prompt_without_langfuse(tmp_data_home, monkeypatch):
    monkeypatch.setenv("PROMPT_BACKEND", "local")
    from bin.generators.builder import DomainBuilder

    builder = DomainBuilder()
    assert "card-prioritization domains" in builder.signature.instructions


def test_draft_returns_drafted_fields(builder_prompt):
    dspy.settings.configure(lm=_scripted_lm({
        "domain_name": "memory-extraction",
        "generator_prompt": "Given a chat message, extract durable memories as Cards…",
        "judge_prompt": "Given two memory cards, pick the more durable one…",
    }))
    from bin.generators.builder import DomainBuilder
    b = DomainBuilder()
    draft = b.draft(
        description="Extract durable memories from chat archives",
        corpus_kind="sqlite",
        corpus_samples=[
            {"text": "user prefers Nix over pip"},
            {"text": "macOS aarch64-darwin dev box"},
            {"text": "..."},
        ],
    )
    assert draft.domain_name == "memory-extraction"
    assert "memories" in draft.generator_prompt.lower()
    assert "two memory cards" in draft.judge_prompt.lower()


def test_draft_normalizes_domain_name(builder_prompt):
    """LLMs return Title Case or 'My Domain Name'; we normalize to lowercase-hyphenated."""
    dspy.settings.configure(lm=_scripted_lm({
        "domain_name": "Memory Extraction!!!",
        "generator_prompt": "g",
        "judge_prompt": "j",
    }))
    from bin.generators.builder import DomainBuilder
    b = DomainBuilder()
    draft = b.draft(description="x", corpus_kind="inline", corpus_samples=[])
    assert draft.domain_name == "memory-extraction"


def test_draft_caps_domain_name_length(builder_prompt):
    dspy.settings.configure(lm=_scripted_lm({
        "domain_name": "a" * 200,
        "generator_prompt": "g",
        "judge_prompt": "j",
    }))
    from bin.generators.builder import DomainBuilder
    b = DomainBuilder()
    draft = b.draft(description="x", corpus_kind="inline", corpus_samples=[])
    assert len(draft.domain_name) <= 64


def test_seed_pushes_meta_prompt_on_init_db(fake_langfuse, monkeypatch, tmp_data_home):
    monkeypatch.setattr("bin.prompts._client_factory", lambda: fake_langfuse.as_client())
    fake_langfuse.enable("create_prompt")
    fake_langfuse.enable("list_prompts")
    fake_langfuse.enable("set_label")
    import importlib, judgement
    importlib.reload(judgement)
    judgement.init_db()
    p = fake_langfuse.get_prompt("domain-builder", label="production")
    assert "domain" in p.prompt.lower()


# ── domain_builder_cli env + error-hint regression (2026-08-16) ─────────
# A Phoenix shell-out inherits only the server env; the CLI must load the
# repo .env (via bin.judgement's loader) or drafting dies with an opaque
# AuthenticationError against the keyless default endpoint.

def _load_cli_module():
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "domain_builder_cli_under_test", root / "bin" / "domain_builder_cli.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_wires_dotenv_loader_via_judgement_import():
    mod = _load_cli_module()
    # The fix: the CLI must load the repo .env at import time via the shared
    # loader (bin.env_loader), so Phoenix shell-outs get credentials without
    # depending on judgement-import side effects.
    assert hasattr(mod, "_load_dotenv")
    from bin.env_loader import load_dotenv
    assert mod._load_dotenv is load_dotenv


def test_cli_error_hint_maps_auth_failures_to_guidance():
    mod = _load_cli_module()
    hint = mod._error_hint(Exception(
        "AuthenticationError: litellm.AuthenticationError: OpenAIException - "
        "Authentication Error, All connection attempts failed"
    ))
    assert "OPENROUTER_API_KEY" in hint
    assert "LLM_BASE_URL" in hint
    # Unrelated errors stay unhinted.
    assert mod._error_hint(ValueError("bad json")) == ""

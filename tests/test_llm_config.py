"""Tests for bin/llm_config.py — documented defaults and env overrides.

Every test scrubs the relevant env vars first: the suite must pass on a
machine that exports OPENROUTER_API_KEY / LLM_* for real work.
"""
from __future__ import annotations

import pytest

from bin import llm_config

ALL_VARS = (
    "OPENROUTER_API_KEY",
    "LLM_BASE_URL",
    "LLM_HJKL_API_KEY",
    "LLM_MODEL",
    "OPTIMIZER_MODEL",
    "CURATOR_MODEL",
    "LLM_TIMEOUT_SECONDS",
    "LLM_NUM_RETRIES",
    "GENERATOR_MODEL",
    "GENERATOR_MAX_TOKENS",
    "GENERATOR_TIMEOUT_SECONDS",
    "GENERATOR_NUM_RETRIES",
    "OPTIMIZER_MIN_VALIDATION",
    "OPTIMIZER_MIN_HOLDOUT",
    "OPTIMIZER_CONTEXT_CHAR_BUDGET",
)

@pytest.fixture(autouse=True)
def scrub_env(monkeypatch):
    for var in ALL_VARS:
        monkeypatch.delenv(var, raising=False)

def test_generator_defaults():
    cfg = llm_config.generator_config()
    assert cfg.model is None
    assert cfg.temperature == 0.0
    assert cfg.max_tokens == 16384
    assert cfg.timeout == 180.0
    assert cfg.num_retries == 0

def test_generator_timeout_falls_back_to_llm_timeout(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "90")
    assert llm_config.generator_config().timeout == 90.0

def test_generator_specific_timeout_wins(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("GENERATOR_TIMEOUT_SECONDS", "12.5")
    assert llm_config.generator_config().timeout == 12.5

def test_generator_env_overrides(monkeypatch):
    monkeypatch.setenv("GENERATOR_MODEL", "z-ai/glm-5.2")
    monkeypatch.setenv("GENERATOR_MAX_TOKENS", "1024")
    monkeypatch.setenv("GENERATOR_NUM_RETRIES", "1")
    cfg = llm_config.generator_config()
    assert cfg.model == "z-ai/glm-5.2"
    assert cfg.max_tokens == 1024
    assert cfg.num_retries == 1

def test_judge_config_seed_defaults():
    cfg = llm_config.judge_config()
    assert cfg.as_dict() == {
        "model": "moonshotai/kimi-k3",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key_env": "OPENROUTER_API_KEY",
        "temperature": 0.0,
        "timeout_seconds": 180,
        "num_retries": 1,
    }

def test_judge_config_model_argument():
    assert llm_config.judge_config("anthropic/claude-opus-5").model == "anthropic/claude-opus-5"

def test_role_defaults_without_openrouter_key():
    cfg = llm_config.role_lm_config(None, temperature=0.0)
    assert cfg.model == "llm-default"
    assert cfg.base_url == "https://llm.example.com/v1"
    assert cfg.api_key == "none"
    assert cfg.timeout == 300.0
    assert cfg.num_retries == 2
    assert cfg.max_tokens is None

def test_role_defaults_with_openrouter_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    cfg = llm_config.role_lm_config(None, temperature=0.0)
    assert cfg.model == "moonshotai/kimi-k3"
    assert cfg.base_url == "https://openrouter.ai/api/v1"
    assert cfg.api_key == "sk-or-test"

def test_role_hjkl_key_beats_openrouter_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.setenv("LLM_HJKL_API_KEY", "hjkl-key")
    assert llm_config.role_lm_config(None, temperature=0.0).api_key == "hjkl-key"

def test_role_base_url_override_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://example.test/v1/")
    assert llm_config.role_lm_config(None, temperature=0.0).base_url == "https://example.test/v1"

def test_role_bounds_env_overrides(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "17")
    monkeypatch.setenv("LLM_NUM_RETRIES", "5")
    cfg = llm_config.role_lm_config(None, temperature=0.0)
    assert cfg.timeout == 17.0
    assert cfg.num_retries == 5

def test_role_explicit_bounds_beat_env(monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "17")
    monkeypatch.setenv("LLM_NUM_RETRIES", "5")
    cfg = llm_config.role_lm_config(None, temperature=0.0, timeout=3.0, num_retries=0)
    assert cfg.timeout == 3.0
    assert cfg.num_retries == 0

def test_role_llm_model_env(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "custom-model")
    assert llm_config.role_lm_config(None, temperature=0.0).model == "custom-model"

def test_role_explicit_model_beats_env(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "custom-model")
    assert llm_config.role_lm_config("explicit", temperature=0.0).model == "explicit"

def test_optimizer_role_fixed_values(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    judge = llm_config.optimizer_lm_config("judge")
    reflection = llm_config.optimizer_lm_config("reflection")
    curator = llm_config.optimizer_lm_config("curator")
    assert (judge.temperature, judge.max_tokens) == (0.0, None)
    assert (reflection.temperature, reflection.max_tokens) == (1.0, 16000)
    assert (curator.temperature, curator.max_tokens) == (0.2, 16000)
    assert judge.model == "moonshotai/kimi-k3"
    assert reflection.model == "z-ai/glm-5.2"
    assert curator.model == "anthropic/claude-opus-5"

def test_optimizer_roles_default_without_key():
    for role in ("judge", "reflection", "curator"):
        assert llm_config.optimizer_lm_config(role).model == "llm-default"

def test_optimizer_role_env_vars_do_not_leak_across_roles(monkeypatch):
    monkeypatch.setenv("OPTIMIZER_MODEL", "reflect-beta")
    monkeypatch.setenv("CURATOR_MODEL", "curate-gamma")
    assert llm_config.optimizer_lm_config("reflection").model == "reflect-beta"
    assert llm_config.optimizer_lm_config("curator").model == "curate-gamma"
    assert llm_config.optimizer_lm_config("judge").model == "llm-default"

def test_optimizer_explicit_model_beats_role_env(monkeypatch):
    monkeypatch.setenv("OPTIMIZER_MODEL", "reflect-beta")
    assert llm_config.optimizer_lm_config("reflection", "explicit").model == "explicit"

def test_optimizer_unknown_role_raises():
    with pytest.raises(ValueError):
        llm_config.optimizer_lm_config("nonsense")

def test_optimizer_gate_defaults():
    assert llm_config.optimizer_min_validation() == 2
    assert llm_config.optimizer_min_holdout() == 2
    assert llm_config.optimizer_context_char_budget() == 48000

def test_optimizer_gate_overrides(monkeypatch):
    monkeypatch.setenv("OPTIMIZER_MIN_VALIDATION", "1")
    monkeypatch.setenv("OPTIMIZER_MIN_HOLDOUT", "3")
    monkeypatch.setenv("OPTIMIZER_CONTEXT_CHAR_BUDGET", "1000")
    assert llm_config.optimizer_min_validation() == 1
    assert llm_config.optimizer_min_holdout() == 3
    assert llm_config.optimizer_context_char_budget() == 1000

def test_panel_matches_consumers():
    from bin import judgement, optimize

    assert judgement.FRONTIER_OPENROUTER_MODELS is llm_config.FRONTIER_OPENROUTER_MODELS
    assert optimize.FRONTIER_OPENROUTER_MODELS is llm_config.FRONTIER_OPENROUTER_MODELS

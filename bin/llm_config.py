"""Single source of truth for LLM/generation environment configuration.

Every environment variable that shapes an LLM request in this repo is read
here, at call time (never at import time), so `.env` loading order and test
monkeypatching behave predictably. Callers (`bin/generate_cards.py`,
`bin/judgement.py`, `bin/optimize.py`) read through the typed accessors
below instead of touching ``os.environ`` directly.

Variables, defaults, and precedence
===================================

Provider / credentials (resolved per request by ``role_lm_config``):

``OPENROUTER_API_KEY``
    No default. When set: the default base URL becomes OpenRouter, the
    default model panel becomes ``FRONTIER_OPENROUTER_MODELS``, and the key
    is used as the API key unless ``LLM_HJKL_API_KEY`` is set.
``LLM_BASE_URL``
    Default: ``https://openrouter.ai/api/v1`` when ``OPENROUTER_API_KEY``
    is set (non-empty), else ``https://llm.example.com/v1``. Trailing slashes
    are stripped. An empty string falls through to the default (``or``
    semantics).
``LLM_HJKL_API_KEY``
    Default: unset. Precedence for the request API key:
    ``LLM_HJKL_API_KEY`` > ``OPENROUTER_API_KEY`` > literal ``"none"``.

Model selection (explicit function argument always wins):

``LLM_MODEL``
    Judge/generator role model. Default: ``FRONTIER_OPENROUTER_MODELS[0]``
    when ``OPENROUTER_API_KEY`` is set, else ``llm-default``.
``OPTIMIZER_MODEL``
    Reflection role model. Default: ``FRONTIER_OPENROUTER_MODELS[1]`` when
    ``OPENROUTER_API_KEY`` is set, else ``llm-default``.
``CURATOR_MODEL``
    Curator role model. Default: ``FRONTIER_OPENROUTER_MODELS[2]`` when
    ``OPENROUTER_API_KEY`` is set, else ``llm-default``.

Request bounds for optimizer roles (judge / reflection / curator):

``LLM_TIMEOUT_SECONDS``
    Default ``300`` (float). Per-request timeout when the caller does not
    pass an explicit timeout. Sized so a full 16K-token reply does not
    become a timeout.
``LLM_NUM_RETRIES``
    Default ``2`` (int). Retries when the caller does not pass an explicit
    value.

Role-fixed values (not env-configurable): judge temperature ``0.0`` with no
max_tokens cap in the role table (``bin/optimize.py``'s ``_build_lm``
applies ``16000``); reflection temperature ``1.0`` / max_tokens ``16000``;
curator temperature ``0.2`` / max_tokens ``16000``.

Card generation (``generator_config``):

``GENERATOR_MODEL``
    Default: unset (``None``) — falls through the judge-role model chain
    (``LLM_MODEL``, then the panel default).
``GENERATOR_MAX_TOKENS``
    Default ``16384`` (int). Reasoning-heavy models spend most of their
    output budget on hidden analysis before the parseable payload; a small
    ceiling turns that into truncation failures. Set lower (e.g. ``4096``)
    for cheap/fast models.
``GENERATOR_TIMEOUT_SECONDS``
    Default: value of ``LLM_TIMEOUT_SECONDS`` if set, else ``180``.
    Precedence: ``GENERATOR_TIMEOUT_SECONDS`` > ``LLM_TIMEOUT_SECONDS`` >
    ``180``. Sized so a full 16K-token generation does not time out.
``GENERATOR_NUM_RETRIES``
    Default ``0`` (int) — generation fails fast; the corpus loop records an
    error and continues.
``GENERATOR_MAX_ITEMS``
    Default ``50`` (int). Per-run corpus item budget when the caller passes
    no explicit ``--limit``. Prevents a large repository from silently
    fanning out into one LLM call per file.

Judge queue seed configuration (``judge_config``) is intentionally *not*
env-driven: rater rows are seeded/synced with model from
``FRONTIER_OPENROUTER_MODELS``, base_url ``https://openrouter.ai/api/v1``,
``api_key_env: OPENROUTER_API_KEY`` (the key is read from that env var at
judge time), temperature ``0.0``, timeout_seconds ``180``, num_retries ``1``.

Optimizer gates (``bin/optimize.py``):

``OPTIMIZER_MIN_VALIDATION``
    Default ``2`` (int). Minimum validation examples to run GEPA.
``OPTIMIZER_MIN_HOLDOUT``
    Default ``2`` (int). Minimum holdout examples to run GEPA.
``OPTIMIZER_CONTEXT_CHAR_BUDGET``
    Default ``48000`` (int). Max curated-prompt length in characters.

Precedence summary: explicit function argument > role-specific env var >
generic env var > built-in default. ``bin/judgement.py`` loads ``.env`` into
``os.environ`` on import *without* overriding already-set variables, so the
real environment always wins over ``.env``.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Optional

FRONTIER_OPENROUTER_MODELS = (
    "moonshotai/kimi-k3",
    "z-ai/glm-5.2",
    "anthropic/claude-opus-5",
)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
HJKL_BASE_URL = "https://llm.example.com/v1"

def default_model(index: int) -> str:
    """Panel model for a role index when OPENROUTER_API_KEY is set, else llm-default."""
    return FRONTIER_OPENROUTER_MODELS[index] if os.environ.get("OPENROUTER_API_KEY") else "llm-default"

@dataclass(frozen=True)
class LMConfig:
    """Fully resolved parameters for one dspy.LM construction."""

    model: str
    base_url: str
    api_key: str
    temperature: float
    timeout: float
    num_retries: int
    max_tokens: Optional[int] = None

def optimizer_concurrency() -> int:
    return max(1, int(os.environ.get("OPTIMIZER_NUM_THREADS", "1")))

def optimizer_max_tokens() -> int:
    return int(os.environ.get("OPTIMIZER_MAX_TOKENS", "16000"))

_OPTIMIZER_ROLES = {
    "judge": ("LLM_MODEL", 0, 0.0, None),
    "reflection": ("OPTIMIZER_MODEL", 1, 1.0, optimizer_max_tokens),
    "curator": ("CURATOR_MODEL", 2, 0.2, optimizer_max_tokens),
}

def role_lm_config(
    model: Optional[str] = None,
    *,
    temperature: float,
    max_tokens: Optional[int] = None,
    timeout: Optional[float] = None,
    num_retries: Optional[int] = None,
) -> LMConfig:
    """Resolve provider, credentials, and bounds for an arbitrary LM role.

    Explicit arguments win; unset bounds fall back to LLM_TIMEOUT_SECONDS /
    LLM_NUM_RETRIES; an unset model falls back to LLM_MODEL, then the panel
    default.
    """
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    base_url = (
        os.environ.get("LLM_BASE_URL")
        or (OPENROUTER_BASE_URL if openrouter_key else HJKL_BASE_URL)
    ).rstrip("/")
    api_key = os.environ.get("LLM_HJKL_API_KEY", "") or openrouter_key or "none"
    chosen = model or os.environ.get("LLM_MODEL") or default_model(0)
    return LMConfig(
        model=chosen,
        base_url=base_url,
        api_key=api_key,
        temperature=temperature,
        timeout=(
            float(os.environ.get("LLM_TIMEOUT_SECONDS", "300"))
            if timeout is None
            else timeout
        ),
        num_retries=(
            int(os.environ.get("LLM_NUM_RETRIES", "2"))
            if num_retries is None
            else num_retries
        ),
        max_tokens=max_tokens,
    )

def optimizer_lm_config(role: str, model: Optional[str] = None) -> LMConfig:
    """Resolved LM config for an optimizer role: judge, reflection, or curator."""
    try:
        model_env, index, temperature, max_tokens = _OPTIMIZER_ROLES[role]
    except KeyError:
        raise ValueError(
            f"unknown optimizer LM role: {role!r} (expected one of {sorted(_OPTIMIZER_ROLES)})"
        ) from None
    if callable(max_tokens):
        max_tokens = max_tokens()
    chosen = model or os.environ.get(model_env) or default_model(index)
    timeout = None
    if max_tokens:
        timeout = float(os.environ.get("OPTIMIZER_TIMEOUT_SECONDS", "1800"))
    return role_lm_config(
        chosen, temperature=temperature, max_tokens=max_tokens, timeout=timeout
    )

@dataclass(frozen=True)
class GeneratorConfig:
    """Bounds for the interactive corpus fan-out generation role."""

    model: Optional[str]
    temperature: float
    max_tokens: int
    timeout: float
    num_retries: int

def generator_explore() -> bool:
    return os.environ.get("GENERATOR_EXPLORE", "1").strip().lower() not in (
        "0", "false", "no", ""
    )

def generator_target_cards() -> int:
    return int(os.environ.get("GENERATOR_TARGET_CARDS", "12"))

def generator_config() -> GeneratorConfig:
    return GeneratorConfig(
        model=os.environ.get("GENERATOR_MODEL"),
        temperature=0.0,
        max_tokens=int(os.environ.get("GENERATOR_MAX_TOKENS", "16384")),
        timeout=float(
            os.environ.get(
                "GENERATOR_TIMEOUT_SECONDS",
                os.environ.get("LLM_TIMEOUT_SECONDS", "180"),
            )
        ),
        num_retries=int(os.environ.get("GENERATOR_NUM_RETRIES", "0")),
    )

def generator_max_items() -> int:
    """Per-run corpus item budget when no explicit limit is given."""
    return int(os.environ.get("GENERATOR_MAX_ITEMS", "50"))

@dataclass(frozen=True)
class JudgeConfig:
    """Seed rater_config for an LLM judge queue row (not env-driven)."""

    model: str
    base_url: str
    api_key_env: str
    temperature: float
    timeout_seconds: int
    num_retries: int

    def as_dict(self) -> dict:
        return asdict(self)

def judge_config(model: Optional[str] = None) -> JudgeConfig:
    return JudgeConfig(
        model=model or FRONTIER_OPENROUTER_MODELS[0],
        base_url=OPENROUTER_BASE_URL,
        api_key_env="OPENROUTER_API_KEY",
        temperature=0.0,
        timeout_seconds=180,
        num_retries=1,
    )

def optimizer_min_validation() -> int:
    return int(os.environ.get("OPTIMIZER_MIN_VALIDATION", "2"))

def optimizer_min_holdout() -> int:
    return int(os.environ.get("OPTIMIZER_MIN_HOLDOUT", "2"))

def optimizer_context_char_budget() -> int:
    return int(os.environ.get("OPTIMIZER_CONTEXT_CHAR_BUDGET", "48000"))

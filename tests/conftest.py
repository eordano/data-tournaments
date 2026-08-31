"""Pytest fixtures for the data-tournaments test suite."""
from __future__ import annotations
import os
import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

from tests.fixtures.fake_langfuse import FakeLangfuse  # noqa: E402

def git_bin_dir() -> str:
    """The directory holding the real git, for tests that shell out to it.

    A hardcoded FHS PATH is not portable -- there is no /usr/bin/git on NixOS --
    and hardcoding one turns "this machine puts git elsewhere" into a test
    failure that reads like a code defect.
    """
    found = shutil.which("git")
    if found is None:
        pytest.skip("git is not on PATH")
    return str(Path(found).resolve().parent)

def hermetic_git_env(home: Path) -> dict:
    """Minimal environment for a throwaway repo: real git, no user config."""
    return {
        "PATH": git_bin_dir(),
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }

_FORBIDDEN_ENV_VARS = ("DATA_TOURNAMENTS_HOME", "PROMPT_BACKEND")
_LEAKED_ENV_VARS = [v for v in _FORBIDDEN_ENV_VARS if v in os.environ]

@pytest.fixture(autouse=True, scope="session")
def _refuse_leaked_environment():
    """HARD-FAIL the run when isolation-breaking env vars are exported
    (wave-11 W5 guard).

    An exported DATA_TOURNAMENTS_HOME or PROMPT_BACKEND leaks into every
    subprocess the tests spawn and points parts of the suite at real data
    homes / real prompt backends — historically 23 failures + 9 errors.
    Opt in explicitly with DT_TESTS_ALLOW_ENV=1 if you really mean it.
    """
    if _LEAKED_ENV_VARS and os.environ.get("DT_TESTS_ALLOW_ENV") != "1":
        pytest.exit(
            "REFUSING TO RUN: the following environment variables are "
            f"exported and break test isolation: {', '.join(_LEAKED_ENV_VARS)}. "
            "Unset them before running the suite (e.g. "
            f"`unset {' '.join(_LEAKED_ENV_VARS)}`), or set "
            "DT_TESTS_ALLOW_ENV=1 to explicitly opt in.",
            returncode=3,
        )
    yield

@pytest.fixture
def fake_langfuse(monkeypatch):
    fake = FakeLangfuse()
    monkeypatch.setenv("LANGFUSE_HOST", "http://fake-langfuse.test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-fake")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-fake")
    return fake

@pytest.fixture
def tmp_data_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "data-tournaments"
    home.mkdir()
    monkeypatch.setenv("DATA_TOURNAMENTS_HOME", str(home))
    return home

@pytest.fixture(autouse=True)
def silence_dspy_cache(monkeypatch, tmp_path):
    """Point DSPy's on-disk cache at a per-test tmp dir.

    Applies suite-wide so no test (or DSPy import side effect) can read from or
    write to the developer's real cache directory.
    """
    monkeypatch.setenv("DSPY_CACHEDIR", str(tmp_path / "dspy_cache"))

def _scripted_lm(*responses):
    """Wrap a list of canned outputs into a DSPy DummyLM.

    Shared plain helper (not a fixture) so test call sites stay unchanged:
    ``from tests.conftest import _scripted_lm``.
    """
    import dspy

    return dspy.utils.DummyLM(list(responses))

def make_evaluation_summary(examples, score):
    """Build a stub ``bin.optimize.EvaluationSummary``: exact match iff score == 1.0.

    Shared by test_optimize.py (as ``_stub_summary``) and
    test_optimize_persistence.py (as ``_summary``).
    """
    from bin.optimize import EvaluationSummary, ExampleOutcome

    exact = score == 1.0
    outcomes = [
        ExampleOutcome(
            example_id=example.example_id,
            gold=example.verdict,
            predicted=example.verdict if exact else "skip",
            score=score,
        )
        for example in examples
    ]
    return EvaluationSummary(
        score=score,
        exact_accuracy=1.0 if exact else 0.0,
        side_accuracy=1.0 if exact else 0.0,
        invalid_rate=0.0,
        examples=len(examples),
        outcomes=outcomes,
    )

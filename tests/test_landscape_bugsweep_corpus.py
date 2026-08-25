"""Tests for bin/landscape/adapters/bugsweep_corpus.py — campaign ledgers
and review rulesets as TIER2 evidence. Fixtures are INVENTED (never copied
from the real corpus); safety rails (path escape, redaction) tested
explicitly."""
from __future__ import annotations

import pytest

from bin.landscape.adapters import adapter_kinds, bugsweep_corpus, get_adapter
from bin.landscape.evidence import SourceType, TrustTier

INDEX_MD = """# invented-campaign — ledger (INDEX)

Pin: example-repo `dev` @ `abc123`.

| Slug | Source | Tracking | Root cause (one line) | Review | v |
|---|---|---|---|---|---|
| fake-null-deref | sentry | Fixes #1 | guard reads IsInit not Succeeded | CONFIRM (2 lenses) | VALIDATED (RED 1/1, GREEN 2/2) |
| fake-leak-loop | slack | Fixes #2 | rent/release pair spans two classes | CONFIRM (3 lenses) | VALIDATED (RED 2/2, GREEN 2/2) |
"""

RULES_MD = """# invented rules

## perf

### 1. Keep hot paths allocation-free — pool and reuse  — ≈29, **B** [unwritten]
Pool arrays, cache strings.
- "never allocate per frame" — reviewer-a <@U123ABC456> xoxb-000-fake-token

### 2. No LINQ in hot paths — ≈3, **B** [unwritten]
Cited by name.
"""


@pytest.fixture
def corpus_root(tmp_path):
    root = tmp_path / "corpus"
    (root / "workspaces" / "camp").mkdir(parents=True)
    (root / "workspaces" / "rules").mkdir(parents=True)
    (root / "workspaces" / "camp" / "INDEX.md").write_text(INDEX_MD)
    (root / "workspaces" / "rules" / "REVIEW-RULES.md").write_text(RULES_MD)
    return root


def test_registered_in_registry():
    assert "bugsweep_corpus" in adapter_kinds()
    assert get_adapter("bugsweep_corpus") is bugsweep_corpus


def test_campaign_index_rows_become_tier2_refs(corpus_root):
    refs = bugsweep_corpus.collect(
        {"root": str(corpus_root), "campaigns": {"aug": "workspaces/camp/INDEX.md"}},
        why="prior campaign evidence",
    )
    assert [r.canonical_uri for r in refs] == [
        "campaign://aug/finding/fake-null-deref",
        "campaign://aug/finding/fake-leak-loop",
    ]
    for r in refs:
        assert r.trust_tier is TrustTier.TIER2_INTERNAL
        assert r.source_type is SourceType.DOC
    assert "IsInit not Succeeded" in refs[0].excerpt
    assert "RED 2/2" in refs[1].excerpt


def test_review_rules_become_per_rule_refs(corpus_root):
    refs = bugsweep_corpus.collect(
        {"root": str(corpus_root), "rulesets": {"aug16": "workspaces/rules/REVIEW-RULES.md"}},
        why="judge context",
    )
    assert [r.canonical_uri for r in refs] == [
        "review-rule://aug16/1",
        "review-rule://aug16/2",
    ]
    assert "allocation-free" in refs[0].excerpt
    assert "Pool arrays" in refs[0].excerpt  # body attached
    assert "No LINQ" in refs[1].excerpt


def test_excerpts_are_redacted(corpus_root):
    refs = bugsweep_corpus.collect(
        {"root": str(corpus_root), "rulesets": {"aug16": "workspaces/rules/REVIEW-RULES.md"}},
        why="w",
    )
    joined = "\n".join(r.excerpt for r in refs)
    assert "xoxb-000-fake-token" not in joined
    assert "U123ABC456" not in joined
    assert "<@user>" in joined or "[REDACTED]" in joined


def test_symlink_escape_refused(corpus_root, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("| evil-slug | x | x | x | x | x |")
    link = corpus_root / "workspaces" / "camp" / "link.md"
    link.symlink_to(outside)
    with pytest.raises(bugsweep_corpus.BugsweepCorpusError, match="outside the corpus root"):
        bugsweep_corpus.collect(
            {"root": str(corpus_root), "campaigns": {"x": "workspaces/camp/link.md"}},
            why="w",
        )


def test_dotdot_escape_refused(corpus_root):
    with pytest.raises(bugsweep_corpus.BugsweepCorpusError, match="outside the corpus root"):
        bugsweep_corpus.collect(
            {"root": str(corpus_root), "campaigns": {"x": "../outside.md"}},
            why="w",
        )


def test_wrong_file_raises_not_silently_empty(corpus_root):
    (corpus_root / "empty.md").write_text("# nothing tabular here\n")
    with pytest.raises(bugsweep_corpus.BugsweepCorpusError, match="no ledger rows"):
        bugsweep_corpus.collect(
            {"root": str(corpus_root), "campaigns": {"x": "empty.md"}}, why="w"
        )
    with pytest.raises(bugsweep_corpus.BugsweepCorpusError, match="no '### N.'"):
        bugsweep_corpus.collect(
            {"root": str(corpus_root), "rulesets": {"x": "empty.md"}}, why="w"
        )


def test_missing_root_and_bad_root_raise(tmp_path):
    with pytest.raises(bugsweep_corpus.BugsweepCorpusError, match="requires 'root'"):
        bugsweep_corpus.collect({}, why="w")
    with pytest.raises(bugsweep_corpus.BugsweepCorpusError, match="not a directory"):
        bugsweep_corpus.collect({"root": str(tmp_path / "nope")}, why="w")


def test_max_items_limit(corpus_root):
    refs = bugsweep_corpus.collect(
        {"root": str(corpus_root), "campaigns": {"aug": "workspaces/camp/INDEX.md"}},
        why="w",
        limits={"max_items": 1},
    )
    assert len(refs) == 1


def test_digest_deterministic(corpus_root):
    cfg = {"root": str(corpus_root), "rulesets": {"aug16": "workspaces/rules/REVIEW-RULES.md"}}
    a = bugsweep_corpus.collect(cfg, why="w")
    b = bugsweep_corpus.collect(cfg, why="w")
    assert [r.digest for r in a] == [r.digest for r in b]

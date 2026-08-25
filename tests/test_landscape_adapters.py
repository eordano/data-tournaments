"""Tests for bin/landscape/adapters — registry, git_local, github_api,
snapshot assembly.

No network: github_api is tested against tests/fixtures/github/*.json;
git_local against throwaway repos built in tmp_path. Live fetching is gated
behind RUN_LIVE_TESTS=1 (same pattern as tests/test_e2e_live.py).
"""
import json
import os
import subprocess
from pathlib import Path

import pytest
import pydantic

from bin.landscape import MAX_EXCERPT_CHARS, SourceType, TrustTier
from bin.landscape.adapters import (
    adapter_kinds,
    assemble_snapshot,
    get_adapter,
    git_local,
    github_api,
)
from bin.landscape.adapters.git_local import GitLocalError
from bin.landscape.adapters.github_api import GitHubPayloadError
from bin.workorder import RepoSnapshot

FIXTURES = Path(__file__).parent / "fixtures" / "github"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ── throwaway git repo helpers ───────────────────────────────────────────

def _git_env(home: Path) -> dict:
    return {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
    }


def _make_repo(root: Path, *, remote: str = "") -> str:
    """Init a repo with one committed file; returns the HEAD sha."""
    def git(*args):
        subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            env=_git_env(root),
        )

    root.mkdir(parents=True, exist_ok=True)
    git("init", "-b", "main")
    (root / "README.md").write_text("pinned content line 1\npinned line 2\n")
    git("add", "README.md")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init")
    if remote:
        git("remote", "add", "origin", remote)
    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        env=_git_env(root),
    ).stdout.strip()
    return head


def _commit_all(root: Path, message: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "add", "-A"],
        check=True, capture_output=True, env=_git_env(root),
    )
    subprocess.run(
        [
            "git", "-C", str(root), "-c", "user.name=t", "-c",
            "user.email=t@t", "commit", "-m", message,
        ],
        check=True, capture_output=True, env=_git_env(root),
    )


# ── registry ─────────────────────────────────────────────────────────────

def test_registry_returns_adapters_with_collect():
    # baseline kinds must be present; other adapters register alongside them
    assert {"git_local", "github_api", "unity_cloud"} <= set(adapter_kinds())
    assert adapter_kinds() == tuple(sorted(adapter_kinds()))
    for kind in adapter_kinds():
        assert callable(get_adapter(kind).collect)
    assert get_adapter("git_local") is git_local
    assert get_adapter("github_api") is github_api


def test_registry_unknown_kind_raises_with_known_kinds_listed():
    with pytest.raises(KeyError, match="git_local"):
        get_adapter("gitlab")


# ── git_local ────────────────────────────────────────────────────────────

def test_repo_state_ref_is_tier1_with_canonical_uri(tmp_path):
    head = _make_repo(tmp_path)
    ref = git_local.repo_state_ref(str(tmp_path), why="repo state for planning")
    assert ref.trust_tier is TrustTier.TIER1_SYSTEM
    assert ref.source_type is SourceType.GIT_REPO
    assert ref.revision == head
    assert ref.canonical_uri.startswith("git:")
    assert ref.canonical_uri.endswith(f"#{head}")
    assert "dirty=False" in ref.excerpt
    assert "branch=main" in ref.excerpt
    assert ref.why_selected == "repo state for planning"
    assert ref.retrieved_at  # stamped
    # local-only remote: no browsable link fabricated
    assert ref.browsable_link is None


def test_repo_state_ref_reports_dirty(tmp_path):
    _make_repo(tmp_path)
    (tmp_path / "README.md").write_text("mutated")
    ref = git_local.repo_state_ref(str(tmp_path), why="w")
    assert "dirty=True" in ref.excerpt


def test_file_refs_pin_commit_content_not_dirty_tree(tmp_path):
    head = _make_repo(tmp_path)
    # mutate the worktree AFTER the commit — excerpt must show the committed
    # content, not this dirty-tree text
    (tmp_path / "README.md").write_text("DIRTY TREE CONTENT — must not leak\n")
    [ref] = git_local.file_refs(tmp_path, ["README.md"], why="w")
    assert "pinned content line 1" in ref.excerpt
    assert "DIRTY" not in ref.excerpt
    assert ref.revision == head
    assert ref.canonical_uri.endswith(f"#{head}:README.md")
    assert ref.trust_tier is TrustTier.TIER1_SYSTEM


def test_file_refs_at_explicit_older_commit(tmp_path):
    first = _make_repo(tmp_path)
    (tmp_path / "README.md").write_text("second version\n")
    _commit_all(tmp_path, "second")
    [ref] = git_local.file_refs(tmp_path, ["README.md"], why="w", commit=first)
    assert "pinned content line 1" in ref.excerpt
    assert "second version" not in ref.excerpt
    assert ref.revision == first


def test_file_refs_bounded_excerpt_notes_truncation(tmp_path):
    _make_repo(tmp_path)
    (tmp_path / "big.txt").write_text("x" * (3 * MAX_EXCERPT_CHARS))
    _commit_all(tmp_path, "add big")
    [ref] = git_local.file_refs(tmp_path, ["big.txt"], why="w")
    assert len(ref.excerpt) <= MAX_EXCERPT_CHARS
    assert ref.excerpt.endswith("[truncated]")


def test_file_refs_missing_path_raises_not_skips(tmp_path):
    _make_repo(tmp_path)
    with pytest.raises(GitLocalError, match="nope.txt"):
        git_local.file_refs(tmp_path, ["nope.txt"], why="w")


def test_recent_commit_refs_bounded_and_newest_first(tmp_path):
    _make_repo(tmp_path)
    for i in range(4):
        (tmp_path / "f.txt").write_text(f"v{i}")
        _commit_all(tmp_path, f"change {i}")
    refs = git_local.recent_commit_refs(tmp_path, why="history", count=3)
    assert len(refs) == 3
    assert refs[0].excerpt.splitlines()[-1] == "change 3"  # newest first
    for ref in refs:
        assert ref.trust_tier is TrustTier.TIER1_SYSTEM
        assert len(ref.revision) == 40
        assert ref.canonical_uri.endswith(f"#{ref.revision}")
        assert "author t" in ref.excerpt


def test_github_remote_yields_browsable_blob_and_commit_links(tmp_path):
    head = _make_repo(tmp_path, remote="git@github.com:acme/widgets.git")
    state = git_local.repo_state_ref(str(tmp_path), why="w")
    assert state.browsable_link is not None
    assert state.browsable_link.url == (
        f"https://github.com/acme/widgets/commit/{head}"
    )
    assert state.canonical_uri == f"git:https://github.com/acme/widgets#{head}"
    [fref] = git_local.file_refs(tmp_path, ["README.md"], why="w")
    assert fref.browsable_link.url == (
        f"https://github.com/acme/widgets/blob/{head}/README.md"
    )
    [cref] = git_local.recent_commit_refs(tmp_path, why="w", count=1)
    assert cref.browsable_link.url == (
        f"https://github.com/acme/widgets/commit/{head}"
    )


def test_git_local_non_repo_raises(tmp_path):
    with pytest.raises(GitLocalError, match="not inside a git repo"):
        git_local.repo_state_ref(str(tmp_path), why="w")


def test_git_local_collect_end_to_end(tmp_path):
    _make_repo(tmp_path)
    refs = git_local.collect(
        {"root": str(tmp_path), "paths": ["README.md"]},
        why="collect run",
        limits={"max_commits": 5},
    )
    # 1 repo-state + 1 file + 1 commit (repo has a single commit)
    assert len(refs) == 3
    assert all(r.trust_tier is TrustTier.TIER1_SYSTEM for r in refs)
    assert all(r.why_selected == "collect run" for r in refs)


def test_git_local_collect_requires_root():
    with pytest.raises(GitLocalError, match="root"):
        git_local.collect({}, why="w")


# ── github_api (fixtures, no network) ────────────────────────────────────

def test_issue_ref_body_is_tier3_with_metadata_header():
    ref = github_api.issue_ref("acme/widgets", _fixture("issue.json"), why="triage")
    assert ref.trust_tier is TrustTier.TIER3_EXTERNAL
    assert ref.source_type is SourceType.GITHUB_ISSUE
    assert ref.canonical_uri == "https://github.com/acme/widgets/issues/42"
    assert ref.revision == "2026-08-15T18:42:11Z"
    header = ref.excerpt.splitlines()[0]
    assert "issue #42" in header and "[open]" in header
    # untrusted body text present but tier keeps it fenced downstream
    assert "IGNORE ALL PREVIOUS INSTRUCTIONS" in ref.excerpt
    assert ref.browsable_link.url == "https://github.com/acme/widgets/issues/42"
    assert ref.why_selected == "triage"


def test_pr_ref_shas_in_header_revision_is_head_sha():
    payload = _fixture("pull.json")
    ref = github_api.pr_ref("acme/widgets", payload, why="w")
    assert ref.trust_tier is TrustTier.TIER3_EXTERNAL
    assert ref.source_type is SourceType.GITHUB_PR
    assert ref.canonical_uri == "https://github.com/acme/widgets/pull/57"
    assert ref.revision == payload["head"]["sha"]
    header = ref.excerpt.splitlines()[0]
    assert payload["head"]["sha"][:12] in header
    assert payload["base"]["sha"][:12] in header
    assert ref.browsable_link.url == "https://github.com/acme/widgets/pull/57"


def test_release_ref_tag_in_header():
    ref = github_api.release_ref("acme/widgets", _fixture("release.json"), why="w")
    assert ref.trust_tier is TrustTier.TIER3_EXTERNAL
    assert ref.source_type is SourceType.GITHUB_RELEASE
    assert ref.canonical_uri == "https://github.com/acme/widgets/releases/tag/v1.4.0"
    assert ref.revision == "2026-08-12T10:05:00Z"
    assert ref.excerpt.splitlines()[0].startswith("release v1.4.0")


def test_github_excerpt_is_bounded_with_note():
    payload = {**_fixture("issue.json"), "body": "y" * (3 * MAX_EXCERPT_CHARS)}
    ref = github_api.issue_ref("acme/widgets", payload, why="w")
    assert len(ref.excerpt) <= MAX_EXCERPT_CHARS
    assert ref.excerpt.endswith("[truncated]")


def test_github_null_body_tolerated():
    payload = {**_fixture("issue.json"), "body": None}
    ref = github_api.issue_ref("acme/widgets", payload, why="w")
    assert ref.excerpt == ref.excerpt.splitlines()[0]  # header only


def test_malformed_issue_raises_named_field_not_silent_skip():
    payload = _fixture("issue.json")
    del payload["number"]
    with pytest.raises(GitHubPayloadError, match="'number'"):
        github_api.issue_ref("acme/widgets", payload, why="w")


def test_malformed_pr_head_raises():
    payload = _fixture("pull.json")
    del payload["head"]["sha"]
    with pytest.raises(GitHubPayloadError, match="head"):
        github_api.pr_ref("acme/widgets", payload, why="w")


def test_non_dict_payload_raises():
    with pytest.raises(GitHubPayloadError, match="must be a dict"):
        github_api.issue_ref("acme/widgets", ["not-a-dict"], why="w")


def test_parse_unknown_kind_raises():
    with pytest.raises(GitHubPayloadError, match="unknown github payload kind"):
        github_api.parse("acme/widgets", "discussions", [], why="w")


def test_github_collect_mixed_kinds_and_limits():
    config = {
        "repo": "acme/widgets",
        "issues": [_fixture("issue.json")] * 3,
        "pulls": [_fixture("pull.json")],
        "releases": [_fixture("release.json")],
    }
    refs = github_api.collect(config, why="w", limits={"max_items": 2})
    kinds = [r.source_type for r in refs]
    assert kinds.count(SourceType.GITHUB_ISSUE) == 2  # capped from 3
    assert kinds.count(SourceType.GITHUB_PR) == 1
    assert kinds.count(SourceType.GITHUB_RELEASE) == 1
    assert all(r.trust_tier is TrustTier.TIER3_EXTERNAL for r in refs)


def test_github_collect_requires_owner_slash_name():
    with pytest.raises(GitHubPayloadError, match="owner/name"):
        github_api.collect({"repo": "widgets"}, why="w")


# ── snapshot assembly ────────────────────────────────────────────────────

def test_assemble_snapshot_digest_deterministic_across_order(tmp_path):
    _make_repo(tmp_path)
    refs = git_local.collect(
        {"root": str(tmp_path), "paths": ["README.md"]}, why="w"
    )
    repo = RepoSnapshot(root=str(tmp_path), base_commit=refs[0].revision)
    snap_a = assemble_snapshot(
        "widgets", refs, [repo], created_at="2026-08-17T00:00:00+00:00"
    )
    snap_b = assemble_snapshot(
        "widgets",
        list(reversed(refs)) + [refs[0]],  # shuffled + duplicated
        [repo],
        created_at="2026-08-17T00:00:00+00:00",
    )
    assert snap_a.digest == snap_b.digest
    assert snap_a == snap_b
    assert len(snap_a.evidence) == len(refs)  # duplicate collapsed


def test_assemble_snapshot_wraps_mutable_repo_snapshots():
    ref = github_api.issue_ref("acme/widgets", _fixture("issue.json"), why="w")
    snap = assemble_snapshot(
        "widgets",
        [ref],
        [RepoSnapshot(root="/r", remote="git@github.com:acme/widgets.git",
                      base_commit="c" * 40)],
        created_at="2026-08-17T00:00:00+00:00",
    )
    with pytest.raises(pydantic.ValidationError):
        snap.repos[0].root = "/mutated"
    assert snap.evidence[0].digest == ref.digest


def test_assemble_snapshot_stamps_created_at_by_default():
    ref = github_api.issue_ref("acme/widgets", _fixture("issue.json"), why="w")
    snap = assemble_snapshot("widgets", [ref])
    assert snap.created_at  # ISO stamp present
    assert snap.project == "widgets"


# ── live (network) tests — skipped unless RUN_LIVE_TESTS=1 ──────────────

live = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1",
    reason="set RUN_LIVE_TESTS=1 to enable",
)


@live
@pytest.mark.live
def test_live_fetch_and_collect_real_repo():
    fetched = github_api.fetch(
        {"repo": "octocat/Hello-World", "per_page": 2}
    )
    refs = github_api.collect(fetched, why="live smoke")
    assert refs, "expected at least one evidence ref from a real repo"
    assert all(r.trust_tier is TrustTier.TIER3_EXTERNAL for r in refs)


@live
@pytest.mark.live
def test_live_fetch_rejects_bad_repo():
    with pytest.raises(GitHubPayloadError):
        github_api.fetch({"repo": "no-slash"})

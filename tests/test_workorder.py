"""Tests for bin/workorder.py — schema, provenance stamping, markdown."""
import subprocess

import pytest

from bin.workorder import (
    RepoSnapshot,
    WorkOrder,
    WorkOrderDraft,
    capture_repo_snapshot,
    finalize_work_order,
    to_markdown,
)


def _draft(**overrides):
    base = dict(
        title="divide() crashes on b == 0",
        goal="Guard the division so callers get a defined error.",
        plan="1. Add a zero check\n2. Raise ValueError with context\n3. Add tests",
        work_type="bug-fix",
        priority="P1",
        priority_rationale="crashes any caller on a common input",
        evidence="`return a / b` at math_utils.py:12 has no guard.",
        files=["src/math_utils.py", "tests/test_math_utils.py"],
        acceptance_criteria=["divide(1, 0) raises ValueError", "tests pass"],
        risks=["callers relying on ZeroDivisionError"],
    )
    base.update(overrides)
    return WorkOrderDraft(**base)


def test_model_cannot_override_system_provenance():
    # A hostile/confused model emitting provenance keys must not influence
    # the finalized record: WorkOrderDraft has no such fields and pydantic
    # ignores unknown keys.
    draft = WorkOrderDraft(
        **{
            "title": "t",
            "goal": "g",
            "plan": "p",
            "domain": "model-injected-domain",
            "created_at": "1999-01-01",
            "models": ["model-injected"],
            "requester": "model-injected-human",
            "links": ["https://model-invented.example"],
        }
    )
    wo = finalize_work_order(
        draft,
        domain="real-domain",
        created_at="2026-08-17T00:00:00Z",
        models=["real/model"],
        repos=[],
        source_ref="a.py",
    )
    assert wo.domain == "real-domain"
    assert wo.created_at == "2026-08-17T00:00:00Z"
    assert wo.models == ["real/model"]
    # Human/integration-only fields stay empty rather than hallucinated.
    assert wo.links == []
    assert wo.requester == ""
    assert wo.reviewers == []


def test_unknown_work_type_and_priority_normalize():
    draft = _draft(work_type="epic saga", priority="urgent!!")
    assert draft.work_type == "change-request"
    assert draft.priority == "P2"
    assert _draft(work_type="Feature").work_type == "feature"
    assert _draft(priority="p0").priority == "P0"


def test_markdown_is_deterministic_and_complete():
    wo = finalize_work_order(
        _draft(),
        domain="code-correctness",
        created_at="2026-08-17T02:00:00Z",
        models=["moonshotai/kimi-k3"],
        repos=[
            RepoSnapshot(
                root="/repo",
                remote="git@github.com:org/repo.git",
                base_commit="abcdef1234567890",
                dirty=True,
            )
        ],
        source_ref="src/math_utils.py",
    )
    md1, md2 = to_markdown(wo), to_markdown(wo)
    assert md1 == md2
    assert "**Domain:** code-correctness" in md1
    assert "P1 — crashes any caller" in md1
    assert "git@github.com:org/repo.git @ `abcdef123456`" in md1
    assert "*(dirty working tree)*" in md1
    # System-derived links render as clickable markdown right after the header.
    assert "[Repository](https://github.com/org/repo)" in md1
    assert "[Base commit abcdef123456](https://github.com/org/repo/commit/abcdef1234567890)" in md1
    assert "## Goal" in md1 and "## Implementation plan" in md1
    assert "## Acceptance criteria" in md1 and "- divide(1, 0) raises ValueError" in md1
    assert "## Risks and open questions" in md1
    # Empty human fields are omitted entirely, not rendered as blanks.
    assert "Requester" not in md1 and "Reviewers" not in md1


def test_markdown_omits_empty_sections():
    wo = finalize_work_order(
        _draft(evidence="", acceptance_criteria=[], risks=[], files=[]),
        domain="d",
        created_at="2026-08-17",
        models=[],
        repos=[],
    )
    md = to_markdown(wo)
    assert "Context and evidence" not in md
    assert "Acceptance criteria" not in md
    assert "Risks" not in md
    assert "Files" not in md


def test_capture_repo_snapshot_real_git(tmp_path):
    def git(*args):
        subprocess.run(
            ["git", "-C", str(tmp_path), *args],
            check=True,
            capture_output=True,
            env={
                "PATH": "/usr/bin:/bin:/usr/local/bin",
                "HOME": str(tmp_path),
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
            },
        )

    git("init")
    (tmp_path / "f.txt").write_text("hello")
    git("add", "f.txt")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init")

    snap = capture_repo_snapshot(str(tmp_path))
    assert snap is not None
    assert len(snap.base_commit) == 40
    assert snap.dirty is False

    (tmp_path / "f.txt").write_text("changed")
    snap2 = capture_repo_snapshot(str(tmp_path))
    assert snap2.dirty is True


def test_capture_repo_snapshot_non_repo(tmp_path):
    # tmp_path lives under the system temp dir, outside any git repo.
    assert capture_repo_snapshot(str(tmp_path)) is None


def test_workorder_roundtrips_through_json():
    wo = finalize_work_order(
        _draft(),
        domain="d",
        created_at="2026-08-17",
        models=["m"],
        repos=[RepoSnapshot(root="/r", base_commit="c" * 40)],
        source_ref="s.py",
    )
    restored = WorkOrder(**wo.model_dump())
    assert restored == wo
    assert restored.schema_version == 1


# ── link derivation (2026-08-17: "links are more important for context") ─

def test_normalize_remote_url_forms():
    from bin.workorder import normalize_remote_url

    assert normalize_remote_url("git@github.com:org/repo.git") == "https://github.com/org/repo"
    # The exact form recorded for unity-explorer (scp form, no git@):
    assert normalize_remote_url("github.com:decentraland/unity-explorer") == \
        "https://github.com/decentraland/unity-explorer"
    assert normalize_remote_url("https://github.com/org/repo.git") == "https://github.com/org/repo"
    assert normalize_remote_url("ssh://git@github.com/org/repo.git") == "https://github.com/org/repo"
    assert normalize_remote_url("") == ""
    assert normalize_remote_url("/local/bare/repo") == ""


def test_derive_links_github_repo_commit_and_source(tmp_path):
    from bin.workorder import RepoSnapshot, derive_links

    src = tmp_path / "scripts" / "build.py"
    src.parent.mkdir(parents=True)
    src.write_text("x")
    snap = RepoSnapshot(
        root=str(tmp_path),
        remote="git@github.com:decentraland/unity-explorer.git",
        base_commit="8be52b3847f7" + "0" * 28,
    )
    links = derive_links([snap], source_ref=str(src))
    by_kind = {l.kind: l.url for l in links}
    assert by_kind["repository"] == "https://github.com/decentraland/unity-explorer"
    assert by_kind["commit"].endswith("/commit/" + snap.base_commit)
    assert by_kind["source"].endswith(f"/blob/{snap.base_commit}/scripts/build.py")


def test_derive_links_source_outside_repo_gets_no_permalink(tmp_path):
    from bin.workorder import RepoSnapshot, derive_links

    snap = RepoSnapshot(
        root=str(tmp_path / "repo"),
        remote="git@github.com:org/repo.git",
        base_commit="a" * 40,
    )
    links = derive_links([snap], source_ref="/etc/passwd")
    assert {l.kind for l in links} == {"repository", "commit"}


def test_workorder_link_rejects_non_https():
    from bin.workorder import WorkOrderLink

    with pytest.raises(Exception):
        WorkOrderLink(label="bad", url="javascript:alert(1)")
    with pytest.raises(Exception):
        WorkOrderLink(label="bad", url="http://insecure.example")

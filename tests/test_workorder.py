"""Tests for bin/workorder.py — schema, provenance stamping, markdown."""
import json
import subprocess

import pytest

from tests.conftest import hermetic_git_env

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
    assert "P1 — crashes any caller" not in md1, (
        "the judge card body carries no self-assessed score"
    )
    assert "git@github.com:org/repo.git @ `abcdef123456`" in md1
    assert "*(dirty working tree)*" in md1
    assert "[Repository](https://github.com/org/repo)" in md1
    assert "[Base commit abcdef123456](https://github.com/org/repo/commit/abcdef1234567890)" in md1
    assert "## Goal" in md1 and "## Implementation plan" in md1
    assert "## Acceptance criteria" in md1 and "- divide(1, 0) raises ValueError" in md1
    assert "## Risks and open questions" in md1
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
            env=hermetic_git_env(tmp_path),
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

def test_normalize_remote_url_forms():
    from bin.workorder import normalize_remote_url

    assert normalize_remote_url("git@github.com:org/repo.git") == "https://github.com/org/repo"
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

def _standing(**overrides):
    from bin.workorder import TournamentStanding

    base = dict(
        points=10,
        played=4,
        rank=2,
        rounds=6,
        pool_id="wave-13",
        pair_keys=["a" * 64, "b" * 64, "c" * 64, "d" * 64],
    )
    base.update(overrides)
    return TournamentStanding(**base)

def test_standing_is_stamped_by_the_system_only():
    wo = finalize_work_order(
        _draft(),
        domain="d",
        created_at="2026-08-27",
        models=[],
        repos=[],
        standing=_standing(),
    )
    assert wo.standing is not None
    assert (wo.standing.points, wo.standing.played, wo.standing.rank) == (10, 4, 2)
    assert wo.standing.pool_id == "wave-13"
    assert len(wo.standing.pair_keys) == 4

def test_standing_on_a_draft_cannot_influence_the_work_order():
    draft = WorkOrderDraft(
        **{
            "title": "t",
            "goal": "g",
            "plan": "p",
            "standing": {"points": 99, "played": 33, "rank": 1},
        }
    )
    assert not hasattr(draft, "standing")
    wo = finalize_work_order(
        draft, domain="d", created_at="2026-08-27", models=[], repos=[]
    )
    assert wo.standing is None
    stamped = finalize_work_order(
        draft,
        domain="d",
        created_at="2026-08-27",
        models=[],
        repos=[],
        standing=_standing(points=3, played=1, rank=7, pair_keys=["e" * 64]),
    )
    assert stamped.standing.points == 3
    assert stamped.standing.rank == 7

def test_standing_rejects_impossible_swiss_scores():
    from bin.workorder import TournamentStanding

    with pytest.raises(Exception, match="exceeds the maximum 3 per match"):
        TournamentStanding(points=13, played=4)
    with pytest.raises(Exception, match="cannot be negative"):
        TournamentStanding(points=-1, played=1)
    with pytest.raises(Exception, match="at most one pair key"):
        TournamentStanding(points=3, played=1, pair_keys=["a" * 64, "b" * 64])
    with pytest.raises(Exception, match="rematch"):
        TournamentStanding(points=4, played=2, pair_keys=["a" * 64, "a" * 64])
    with pytest.raises(Exception, match="leave rank 0"):
        TournamentStanding(points=0, played=0, rank=1)
    assert TournamentStanding(points=3, played=1, rank=1).pair_keys == []

def wo_fields_still_carry_priority(draft) -> bool:
    """The operator views read the object, so the field must stay populated."""
    return draft.priority in ("P0", "P1", "P2", "P3")

def test_standing_never_reaches_the_markdown_a_judge_is_shown():
    """to_markdown is the judge card body. A judge who can see points is
    comparing standings instead of items."""
    kwargs = dict(domain="code-correctness", created_at="2026-08-27",
                  models=[], repos=[])
    ranked = to_markdown(finalize_work_order(_draft(), standing=_standing(), **kwargs))
    unranked = to_markdown(finalize_work_order(_draft(), **kwargs))
    assert ranked == unranked
    assert "Standing" not in ranked
    assert "10 pts" not in ranked
    assert "Priority" not in ranked and "P1" not in ranked, (
        "priority is a self-assessed absolute score and this markdown is the "
        "judge card body; scrubbing the payload key cannot reach a score "
        "written into prose, so it must not be composed here in the first place"
    )
    assert wo_fields_still_carry_priority(_draft())

def test_standing_absent_renders_and_validates_unchanged():
    wo = finalize_work_order(
        _draft(), domain="d", created_at="2026-08-27", models=[], repos=[]
    )
    md = to_markdown(wo)
    assert "Standing" not in md
    assert "Priority" not in md
    assert wo.priority == "P1", "the field survives; only the rendering drops it"
    assert WorkOrder(**wo.model_dump()) == wo

def test_standing_survives_a_json_roundtrip():
    wo = finalize_work_order(
        _draft(),
        domain="d",
        created_at="2026-08-27",
        models=[],
        repos=[],
        standing=_standing(),
    )
    restored = WorkOrder(**json.loads(json.dumps(wo.model_dump())))
    assert restored == wo
    assert restored.standing.pair_keys == wo.standing.pair_keys

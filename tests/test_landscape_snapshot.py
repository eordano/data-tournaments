"""Tests for bin.landscape.snapshot — LandscapeSnapshot determinism."""
from __future__ import annotations

import pydantic
import pytest

from bin.landscape.evidence import EvidenceRef, SourceType, TrustTier
from bin.landscape.snapshot import FrozenRepoSnapshot, LandscapeSnapshot
from bin.workorder import RepoSnapshot


def ref(uri: str, tier: TrustTier = TrustTier.TIER2_INTERNAL, **kw) -> EvidenceRef:
    return EvidenceRef(
        source_type=SourceType.DOC,
        canonical_uri=uri,
        trust_tier=tier,
        **kw,
    )


def repo(**overrides) -> FrozenRepoSnapshot:
    base = dict(
        root="/repos/widget",
        remote="git@github.com:acme/widget.git",
        base_commit="a" * 40,
        dirty=False,
    )
    base.update(overrides)
    return FrozenRepoSnapshot(**base)


def snap(evidence, repos=(), **kw) -> LandscapeSnapshot:
    fields = dict(
        project="unity-explorer",
        created_at="2026-08-17T12:00:00Z",
        evidence=tuple(evidence),
        repos=tuple(repos),
    )
    fields.update(kw)
    return LandscapeSnapshot(**fields)


class TestDigestDeterminism:
    def test_insertion_order_does_not_matter(self):
        a, b, c = ref("doc://a"), ref("doc://b"), ref("doc://c")
        s1 = snap([a, b, c])
        s2 = snap([c, a, b])
        assert s1.digest == s2.digest
        assert s1 == s2  # normalized order makes them the same object

    def test_repo_order_does_not_matter(self):
        r1, r2 = repo(root="/repos/a"), repo(root="/repos/b")
        assert snap([], [r1, r2]).digest == snap([], [r2, r1]).digest

    def test_duplicate_evidence_deduplicated(self):
        a = ref("doc://a")
        s = snap([a, ref("doc://a"), a])
        assert len(s.evidence) == 1
        assert s.digest == snap([a]).digest

    def test_changed_excerpt_changes_snapshot_digest(self):
        s1 = snap([ref("doc://a", excerpt="v1")])
        s2 = snap([ref("doc://a", excerpt="v2")])
        assert s1.digest != s2.digest

    def test_project_and_created_at_in_digest(self):
        base = snap([ref("doc://a")])
        assert base.digest != snap([ref("doc://a")], project="other").digest
        assert (
            base.digest
            != snap([ref("doc://a")], created_at="2026-08-18T00:00:00Z").digest
        )

    def test_digest_is_sha256_hex(self):
        d = snap([]).digest
        assert len(d) == 64
        assert set(d) <= set("0123456789abcdef")


class TestImmutability:
    def test_snapshot_mutation_raises(self):
        s = snap([ref("doc://a")])
        with pytest.raises(pydantic.ValidationError):
            s.project = "hijacked"
        with pytest.raises(pydantic.ValidationError):
            s.evidence = ()

    def test_evidence_is_tuple_not_list(self):
        s = snap([ref("doc://a")])
        assert isinstance(s.evidence, tuple)
        assert isinstance(s.repos, tuple)

    def test_frozen_repo_snapshot_mutation_raises(self):
        r = repo()
        with pytest.raises(pydantic.ValidationError):
            r.base_commit = "b" * 40

    def test_frozen_repo_is_a_workorder_repo_snapshot(self):
        # Reuse, not duplication: the landscape repo model IS the workorder one.
        assert issubclass(FrozenRepoSnapshot, RepoSnapshot)


class TestRoundTrip:
    def test_model_dump_validate_round_trip(self):
        s = snap([ref("doc://a"), ref("chat://x", TrustTier.TIER3_EXTERNAL)], [repo()])
        again = LandscapeSnapshot.model_validate(s.model_dump())
        assert again == s
        assert again.digest == s.digest

    def test_json_round_trip(self):
        s = snap([ref("doc://a")], [repo(dirty=True)])
        again = LandscapeSnapshot.model_validate_json(s.model_dump_json())
        assert again.digest == s.digest


class TestValidation:
    def test_empty_project_rejected(self):
        with pytest.raises(pydantic.ValidationError):
            LandscapeSnapshot(project="  ")

    def test_no_approval_or_authorization_fields(self):
        forbidden = {
            "approved", "approval", "approvals", "approved_by",
            "authorized", "authorization", "auth", "approval_state",
        }
        assert not forbidden & set(LandscapeSnapshot.model_fields)

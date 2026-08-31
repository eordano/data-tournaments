"""Tests for bin.landscape.evidence — EvidenceRef, trust tiers, links."""
from __future__ import annotations

import pydantic
import pytest

from bin.landscape.evidence import (
    MAX_EXCERPT_CHARS,
    BrowsableLink,
    EvidenceRef,
    SourceType,
    TrustTier,
)

def make_ref(**overrides) -> EvidenceRef:
    base = dict(
        source_type=SourceType.GITHUB_ISSUE,
        canonical_uri="github://acme/widget/issues/42",
        revision="2026-08-17T10:00:00Z",
        retrieved_at="2026-08-17T10:05:00Z",
        trust_tier=TrustTier.TIER2_INTERNAL,
        excerpt="Widget crashes on load",
        browsable_link=BrowsableLink(
            label="Issue #42",
            url="https://github.com/acme/widget/issues/42",
            kind="issue",
        ),
        why_selected="matches crash signature in work order",
    )
    base.update(overrides)
    return EvidenceRef(**base)

class TestEnums:
    def test_source_type_values(self):
        assert {s.value for s in SourceType} == {
            "git_repo", "github_issue", "github_pr", "github_release",
            "ci_build", "doc", "chat", "api", "mcp_resource",
        }

    def test_trust_tier_values(self):
        assert [t.value for t in TrustTier] == [
            "tier1_system", "tier2_internal", "tier3_external",
        ]

class TestEvidenceRef:
    def test_id_is_content_derived(self):
        ref = make_ref()
        assert ref.id == "ev-" + ref.digest[:16]
        assert ref.digest == make_ref().digest

    def test_changed_excerpt_changes_digest_and_id(self):
        a, b = make_ref(), make_ref(excerpt="different text")
        assert a.digest != b.digest
        assert a.id != b.id

    def test_every_content_field_affects_digest(self):
        base = make_ref()
        variants = [
            make_ref(source_type=SourceType.DOC),
            make_ref(canonical_uri="github://acme/widget/issues/43"),
            make_ref(revision="other"),
            make_ref(retrieved_at="2026-08-18T00:00:00Z"),
            make_ref(trust_tier=TrustTier.TIER3_EXTERNAL),
            make_ref(browsable_link=None),
            make_ref(why_selected="other reason"),
        ]
        digests = {v.digest for v in variants}
        assert base.digest not in digests
        assert len(digests) == len(variants)

    def test_frozen_mutation_raises(self):
        ref = make_ref()
        with pytest.raises(pydantic.ValidationError):
            ref.excerpt = "tampered"
        with pytest.raises(pydantic.ValidationError):
            ref.trust_tier = TrustTier.TIER1_SYSTEM

    def test_excerpt_and_why_selected_bounded(self):
        ref = make_ref(
            excerpt="x" * (MAX_EXCERPT_CHARS + 500),
            why_selected="y" * (MAX_EXCERPT_CHARS + 500),
        )
        assert len(ref.excerpt) == MAX_EXCERPT_CHARS
        assert len(ref.why_selected) == MAX_EXCERPT_CHARS

    def test_empty_canonical_uri_rejected(self):
        with pytest.raises(pydantic.ValidationError):
            make_ref(canonical_uri="   ")

    def test_round_trip(self):
        ref = make_ref()
        again = EvidenceRef.model_validate(ref.model_dump())
        assert again == ref
        assert again.digest == ref.digest
        json_again = EvidenceRef.model_validate_json(ref.model_dump_json())
        assert json_again.digest == ref.digest

    def test_no_approval_or_authorization_fields(self):
        forbidden = {
            "approved", "approval", "approvals", "approved_by",
            "authorized", "authorization", "auth", "approval_state",
        }
        assert not forbidden & set(EvidenceRef.model_fields)

class TestBrowsableLink:
    def test_https_only(self):
        for bad in ("http://example.com/x", "ftp://x", "javascript:alert(1)"):
            with pytest.raises(pydantic.ValidationError):
                BrowsableLink(label="bad", url=bad)

    def test_https_accepted_and_frozen(self):
        link = BrowsableLink(label="ok", url="https://example.com/x")
        assert link.url == "https://example.com/x"
        with pytest.raises(pydantic.ValidationError):
            link.url = "https://elsewhere.com"

    def test_link_url_validated_when_nested(self):
        with pytest.raises(pydantic.ValidationError):
            make_ref(
                browsable_link={"label": "x", "url": "http://insecure.example"}
            )

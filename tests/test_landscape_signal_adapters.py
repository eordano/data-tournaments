"""Tests for the four August-bugsweep signal-source adapters:
sentry_csv, slack_csv, github_autoclosed, dedup_lists.

Fixture-driven (tests/fixtures/signals/ — small INVENTED data replicating the
real campaign file shapes), no network.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from bin.landscape.adapters import (
    adapter_kinds,
    dedup_lists,
    get_adapter,
    github_autoclosed,
    sentry_csv,
    slack_csv,
)
from bin.landscape.evidence import SourceType, TrustTier

FIXTURES = Path(__file__).parent / "fixtures" / "signals"

LEAKED = (
    "xoxb-000000-FAKEFAKEFAKE",
    "xoxb-111111111-FAKETOKENFAKE",
    "ghp_FAKEFAKEFAKEFAKEFAKE1234",
    "ghp_AUTOFAKEFAKEFAKEFAKE99",
    "<@U0FAKE123>",
    "<@U9FAKE987>",
)

def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")

def _assert_no_leaks(refs):
    for ref in refs:
        for secret in LEAKED:
            assert secret not in ref.excerpt, (ref.canonical_uri, secret)

def test_all_four_registered_round_trip():
    for kind, module in (
        ("sentry_csv", sentry_csv),
        ("slack_csv", slack_csv),
        ("github_autoclosed", github_autoclosed),
        ("dedup_lists", dedup_lists),
    ):
        assert kind in adapter_kinds()
        assert get_adapter(kind) is module

def test_redact_text_covers_mentions_and_token_shapes():
    dirty = (
        "ping <@U0FAKE123> and <@W2FAKE456>; keys: xoxb-1-FAKE sk-FAKEKEY12345 "
        "ghp_FAKETOKEN99 " + "a1b2c3d4" * 4 + " " + "A" * 44
    )
    clean = slack_csv.redact_text(dirty)
    assert "<@user>" in clean
    assert "[REDACTED]" in clean
    for bad in ("<@U0FAKE123>", "<@W2FAKE456>", "xoxb-", "sk-FAKE", "ghp_", "a1b2c3d4a1b2"):
        assert bad not in clean
    assert "A" * 44 not in clean

def test_sentry_happy_path_field_mapping():
    refs = sentry_csv.parse_issues(_fixture("sentry-week.csv"), why="weekly volume signal")
    assert len(refs) == 3
    ref = refs[0]
    assert ref.source_type is SourceType.API
    assert ref.trust_tier is TrustTier.TIER3_EXTERNAL
    assert ref.canonical_uri == "sentry:EXPLORER-1A2B"
    assert ref.revision == "2026-08-15T23:41:02Z"
    assert ref.retrieved_at == "2026-08-15T23:41:02Z"
    assert "NullReferenceException in AvatarShapeSystem.Update" in ref.excerpt
    assert "culprit: AvatarShapeSystem.Update(AvatarShapeSystem.cs:214)" in ref.excerpt
    assert "events: week=412 lifetime=9034 users=87" in ref.excerpt
    assert "level=error substatus=ongoing" in ref.excerpt
    assert ref.browsable_link is not None
    assert ref.browsable_link.url == "https://example-org.sentry.io/issues/500001/"
    assert ref.why_selected == "weekly volume signal"

def test_sentry_html_unescaped_and_redacted():
    refs = sentry_csv.parse_issues(_fixture("sentry-week.csv"), why="w")
    assert "&amp;" not in refs[1].excerpt
    assert "retry loop stuck" in refs[1].excerpt
    _assert_no_leaks(refs)

def test_sentry_malformed_rows_raise():
    header = (
        "short_id,week_events,user_count,level,substatus,lifetime_events,"
        "first_seen,last_seen,title,culprit,permalink\n"
    )
    for row, missing in (
        (",1,1,error,ongoing,1,a,b,T,c,https://x.sentry.io/1/", "short_id"),
        ("S-1,1,1,error,ongoing,1,a,b,,c,https://x.sentry.io/1/", "title"),
        ("S-1,1,1,error,ongoing,1,a,b,T,c,", "permalink"),
    ):
        with pytest.raises(sentry_csv.SentryPayloadError, match=missing):
            sentry_csv.parse_issues(header + row, why="w")

def test_sentry_empty_input_and_missing_config():
    assert sentry_csv.parse_issues("", why="w") == []
    assert sentry_csv.parse_issues("   \n", why="w") == []
    assert sentry_csv.collect({"csv_text": ""}, why="w") == []
    with pytest.raises(sentry_csv.SentryPayloadError, match="csv_text"):
        sentry_csv.collect({}, why="w")

def test_sentry_limits_and_digest_determinism():
    text = _fixture("sentry-week.csv")
    refs = sentry_csv.collect(
        {"csv_text": text}, why="w", limits={"max_items": 2, "max_chars": 120}
    )
    assert len(refs) == 2
    assert all(len(r.excerpt) <= 120 for r in refs)
    again = sentry_csv.collect(
        {"csv_text": text}, why="w", limits={"max_items": 2, "max_chars": 120}
    )
    assert [r.digest for r in refs] == [r.digest for r in again]
    assert [r.id for r in refs] == [r.id for r in again]

def test_slack_happy_path_template_parsing():
    refs = slack_csv.parse_reports(_fixture("slack-bugs.csv"), why="human reports w/ STR")
    assert len(refs) == 2
    ref = refs[0]
    assert ref.source_type is SourceType.CHAT
    assert ref.trust_tier is TrustTier.TIER3_EXTERNAL
    assert ref.canonical_uri == "slack:1755012345.000100"
    assert ref.revision == "1755012345.000100"
    assert ref.retrieved_at == "2026-08-12"
    assert "replies=4" in ref.excerpt
    assert "TITLE: Portable experience crashes on teleport" in ref.excerpt
    assert "STR: 1. Launch a portable experience" in ref.excerpt
    assert "REPRODUCTION INDEX: 5/5" in ref.excerpt
    assert "REPORTER: <@user>" in ref.excerpt
    assert ref.browsable_link is None

def test_slack_free_form_text_tolerated():
    refs = slack_csv.parse_reports(_fixture("slack-bugs.csv"), why="w")
    ref = refs[1]
    assert ref.canonical_uri == "slack:1755098765.000200"
    assert "replies=0" in ref.excerpt
    assert "minimap goes black" in ref.excerpt

def test_slack_redaction_of_mentions_and_tokens():
    refs = slack_csv.parse_reports(_fixture("slack-bugs.csv"), why="w")
    _assert_no_leaks(refs)
    assert "<@user>" in refs[0].excerpt
    assert "[REDACTED]" in refs[0].excerpt
    assert "[REDACTED]" in refs[1].excerpt

def test_slack_malformed_rows_raise():
    with pytest.raises(slack_csv.SlackPayloadError, match="ts"):
        slack_csv.parse_reports("ts,date,replies,text\n,2026-08-12,0,hello\n", why="w")
    with pytest.raises(slack_csv.SlackPayloadError, match="text"):
        slack_csv.parse_reports("ts,date,replies,text\n1.0,2026-08-12,0,\n", why="w")

def test_slack_empty_limits_digest_and_config():
    assert slack_csv.parse_reports("", why="w") == []
    with pytest.raises(slack_csv.SlackPayloadError, match="csv_text"):
        slack_csv.collect({}, why="w")
    text = _fixture("slack-bugs.csv")
    refs = slack_csv.collect(
        {"csv_text": text}, why="w", limits={"max_items": 1, "max_chars": 150}
    )
    assert len(refs) == 1
    assert len(refs[0].excerpt) <= 150
    again = slack_csv.collect(
        {"csv_text": text}, why="w", limits={"max_items": 1, "max_chars": 150}
    )
    assert refs[0].digest == again[0].digest

def test_autoclosed_happy_path_field_mapping():
    refs = github_autoclosed.parse_rows(
        "example-org/example-repo",
        _fixture("autoclosed.csv"),
        why="auto-closed without fix recovery signal",
    )
    assert len(refs) == 2
    ref = refs[0]
    assert ref.source_type is SourceType.GITHUB_ISSUE
    assert ref.trust_tier is TrustTier.TIER3_EXTERNAL
    assert ref.canonical_uri == "github:example-org/example-repo#9001"
    assert ref.revision == "2026-06-20"
    assert "auto-closed without fix" in ref.excerpt
    assert "Emote wheel stays open after death" in ref.excerpt
    assert "created: 2026-01-15" in ref.excerpt
    assert "auto_closed: 2026-06-20" in ref.excerpt
    assert "walk into the void" in ref.excerpt
    assert ref.browsable_link is not None
    assert (
        ref.browsable_link.url
        == "https://github.com/example-org/example-repo/issues/9001"
    )

def test_autoclosed_body_redacted():
    refs = github_autoclosed.parse_rows(
        "example-org/example-repo", _fixture("autoclosed.csv"), why="w"
    )
    _assert_no_leaks(refs)
    assert "[REDACTED]" in refs[0].excerpt

def test_autoclosed_malformed_rows_raise():
    header = "issue,created,auto_closed,title,body\n"
    with pytest.raises(github_autoclosed.GitHubAutoclosedPayloadError, match="issue"):
        github_autoclosed.parse_rows("o/r", header + ",a,b,T,B\n", why="w")
    with pytest.raises(github_autoclosed.GitHubAutoclosedPayloadError, match="title"):
        github_autoclosed.parse_rows("o/r", header + "1,a,b,,B\n", why="w")

def test_autoclosed_config_empty_limits_digest():
    with pytest.raises(
        github_autoclosed.GitHubAutoclosedPayloadError, match="repo"
    ):
        github_autoclosed.collect({"csv_text": "x"}, why="w")
    with pytest.raises(
        github_autoclosed.GitHubAutoclosedPayloadError, match="csv_text"
    ):
        github_autoclosed.collect({"repo": "o/r"}, why="w")
    assert github_autoclosed.collect({"repo": "o/r", "csv_text": ""}, why="w") == []
    text = _fixture("autoclosed.csv")
    cfg = {"repo": "example-org/example-repo", "csv_text": text}
    refs = github_autoclosed.collect(cfg, why="w", limits={"max_items": 1, "max_chars": 100})
    assert len(refs) == 1
    assert len(refs[0].excerpt) <= 100
    assert "truncated" in refs[0].excerpt
    again = github_autoclosed.collect(cfg, why="w", limits={"max_items": 1, "max_chars": 100})
    assert refs[0].digest == again[0].digest

def _dedup_config():
    return {
        "open_prs_tsv": _fixture("open-prs.tsv"),
        "inflight_tsv": _fixture("inflight.tsv"),
        "prior_slugs_text": _fixture("prior-campaign-slugs.txt"),
    }

def test_dedup_one_ref_per_list_tier1():
    refs = dedup_lists.collect(_dedup_config(), why="dedup gate inputs")
    assert len(refs) == 3
    kinds = {r.canonical_uri.split(":")[1].split("@")[0] for r in refs}
    assert kinds == {"open_prs", "inflight", "prior_slugs"}
    for ref in refs:
        assert ref.trust_tier is TrustTier.TIER1_SYSTEM
        assert ref.source_type is SourceType.API
        assert "dedup gate" in ref.why_selected
        assert ref.canonical_uri.endswith("@" + ref.revision)
        assert len(ref.revision) == 12

def test_dedup_excerpt_row_count_and_entries():
    refs = {r.canonical_uri.split(":")[1].split("@")[0]: r for r in dedup_lists.collect(_dedup_config(), why="w")}
    assert "open_prs: 3 rows" in refs["open_prs"].excerpt
    assert "9750 | fix/avatar-shape-nre" in refs["open_prs"].excerpt
    assert "prior_slugs: 4 rows" in refs["prior_slugs"].excerpt
    assert "avatar-shape-nre" in refs["prior_slugs"].excerpt

def test_dedup_uri_stability_and_content_sensitivity():
    a = dedup_lists.collect(_dedup_config(), why="w")
    b = dedup_lists.collect(_dedup_config(), why="w")
    assert [r.canonical_uri for r in a] == [r.canonical_uri for r in b]
    assert [r.digest for r in a] == [r.digest for r in b]
    changed = dedup_lists.collect(
        {**_dedup_config(), "open_prs_tsv": "9999\tnew\tNew PR\n"}, why="w"
    )
    changed_open = next(r for r in changed if ":open_prs@" in r.canonical_uri)
    orig_open = next(r for r in a if ":open_prs@" in r.canonical_uri)
    assert changed_open.canonical_uri != orig_open.canonical_uri

def test_dedup_limits_and_missing_config():
    refs = dedup_lists.collect(
        {"prior_slugs_text": _fixture("prior-campaign-slugs.txt")},
        why="w",
        limits={"max_items": 2, "max_chars": 200},
    )
    assert len(refs) == 1
    assert "… and 2 more" in refs[0].excerpt
    assert len(refs[0].excerpt) <= 200
    with pytest.raises(dedup_lists.DedupPayloadError, match="at least one"):
        dedup_lists.collect({}, why="w")

def test_dedup_empty_list_is_zero_row_ref():
    refs = dedup_lists.collect({"open_prs_tsv": ""}, why="w")
    assert len(refs) == 1
    assert "0 rows" in refs[0].excerpt

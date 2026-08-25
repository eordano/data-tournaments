"""Tests for bin/campaign_intake.py — signals -> dedup gate -> findings."""
from __future__ import annotations

import pytest

from bin import campaign_intake, campaigns, catalog


SENTRY_CSV = """short_id,week_events,user_count,level,substatus,lifetime_events,first_seen,last_seen,title,culprit,permalink
FAKE-1,10,3,error,ongoing,50,2026-08-01,2026-08-16,NullReference in EmoteLoader,EmoteSystem in MoveNext,https://org.sentry.io/issues/111/
FAKE-2,5,2,error,ongoing,20,2026-08-02,2026-08-15,Semaphore leak in SyncBuffer,CrdtSync in Rent,https://org.sentry.io/issues/222/
"""

SLACK_CSV = """ts,date,replies,text
1755400000.1,2026-08-10,4,"<@U0FAKE> submitted | :pencil2: *TITLE:* | Minimap pixelated in fullscreen | :notebook: *DESCRIPTION:* | Map stays low res"
"""


@pytest.fixture
def seeded_campaign(tmp_data_home):
    catalog.init()
    catalog.create_project(name="unity-explorer")
    campaigns.create_campaign(
        project="unity-explorer",
        name="sweep-1",
        kind="bugsweep",
        objective="test sweep",
    )
    return "sweep-1"


def _signals():
    return {
        "sentry": {"kind": "sentry-csv", "config": {"csv_text": SENTRY_CSV}},
        "slack": {"kind": "slack-csv", "config": {"csv_text": SLACK_CSV}},
    }


def test_ingest_creates_findings_with_signal_evidence(seeded_campaign):
    out = campaign_intake.ingest(seeded_campaign, signals=_signals())
    assert len(out["created"]) == 3
    assert out["per_source"] == {"sentry": 2, "slack": 1}
    assert out["evidence_count"] == 3

    ledger = campaigns.campaign_ledger(seeded_campaign)
    assert len(ledger["findings"]) == 3
    finding = campaigns.get_finding(seeded_campaign, out["created"][0])
    assert finding["state"] == "candidate"
    roles = {e["role"] for e in finding["evidence"]}
    assert "signal" in roles


def test_ingest_is_idempotent(seeded_campaign):
    first = campaign_intake.ingest(seeded_campaign, signals=_signals())
    second = campaign_intake.ingest(seeded_campaign, signals=_signals())
    assert second["created"] == []
    assert sorted(second["skipped_existing"]) == sorted(first["created"])
    assert len(campaigns.campaign_ledger(seeded_campaign)["findings"]) == 3


def test_dedup_gate_suppresses_matching_signals(seeded_campaign):
    # A prior-campaign slug matching the sentry NullReference finding's
    # slug words must suppress it, with the reason recorded.
    out = campaign_intake.ingest(
        seeded_campaign,
        signals={"sentry": {"kind": "sentry-csv", "config": {"csv_text": SENTRY_CSV}}},
        dedup={"prior_slugs_text": "nullreference\nsomething-else\n"},
    )
    assert len(out["deduped"]) == 1
    assert "prior" in out["deduped"][0]["reason"] or "matched" in out["deduped"][0]["reason"]
    # The suppressed one never became a finding.
    slugs = [f["slug"] for f in campaigns.campaign_ledger(seeded_campaign)["findings"]]
    assert all("nullreference" not in s for s in slugs)


def test_unknown_signal_kind_raises(seeded_campaign):
    with pytest.raises(campaign_intake.IntakeError, match="unknown signal kind"):
        campaign_intake.ingest(
            seeded_campaign,
            signals={"x": {"kind": "carrier-pigeon", "config": {}}},
        )


def test_unknown_campaign_raises(tmp_data_home):
    catalog.init()
    with pytest.raises(LookupError):
        campaign_intake.ingest("no-such-campaign", signals={})


def test_sources_registered_in_catalog(seeded_campaign):
    campaign_intake.ingest(seeded_campaign, signals=_signals())
    src = catalog.get_source("unity-explorer", "sentry")
    assert src["kind"] == "sentry-csv"
    assert src["trust_tier"] == 3


# ── L1 regression: dedup tokens from STRUCTURED identifiers only ─────────
# Baseline showcase run (2026-08-17): the PR title 'Rework ban dialog copy
# (does NOT fix timestamp parsing)' produced tokens 'does'/'timestamp' and
# wrongly deduped two autoclosed-issue findings.

OPEN_PRS_TSV = "pr\ttitle\thead\n9911\tRework ban dialog copy (does NOT fix timestamp parsing)\tfeature/ban-dialog-copy\n"


def test_dedup_tokens_never_from_free_text_titles():
    toks = _tokens_for({"open_prs_tsv": OPEN_PRS_TSV})
    assert "does" not in toks
    assert "timestamp" not in toks
    assert "parsing" not in toks
    # Structured identifiers ARE kept: PR number + branch head segment.
    assert "9911" in toks
    assert "ban-dialog-copy" in toks


def _tokens_for(dedup_cfg):
    out = {}
    for key, kind in campaign_intake._DEDUP_KINDS.items():
        if key not in dedup_cfg:
            continue
        for tok in campaign_intake._identifier_tokens(kind, dedup_cfg[key]):
            if campaign_intake._keep_token(tok):
                out.setdefault(tok, kind)
    return out


def test_title_words_no_longer_suppress_findings(seeded_campaign):
    # The sentry findings (nullreference…, semaphore…) share no identifier
    # with the PR list — with title words excluded, nothing is deduped.
    out = campaign_intake.ingest(
        seeded_campaign,
        signals={"sentry": {"kind": "sentry-csv", "config": {"csv_text": SENTRY_CSV}}},
        dedup={"open_prs_tsv": OPEN_PRS_TSV},
    )
    assert out["deduped"] == []
    assert len(out["created"]) == 2


def test_branch_identifier_still_dedupes_true_positive(seeded_campaign):
    # A signal whose slug carries the branch identifier IS suppressed.
    csv = (
        "short_id,week_events,user_count,level,substatus,lifetime_events,"
        "first_seen,last_seen,title,culprit,permalink\n"
        "FAKE-9,4,1,error,ongoing,9,2026-08-01,2026-08-16,"
        "Ban-dialog-copy regression persists,BanDialog in Render,"
        "https://org.sentry.io/issues/999/\n"
    )
    out = campaign_intake.ingest(
        seeded_campaign,
        signals={"sentry2": {"kind": "sentry-csv", "config": {"csv_text": csv}}},
        dedup={"open_prs_tsv": OPEN_PRS_TSV},
    )
    assert len(out["deduped"]) == 1
    assert "ban-dialog-copy" in out["deduped"][0]["reason"]

"""Tests for bin/approvals.py — the only sanctioned approval-Signal path.

Wave 7 RBAC + audit: fail-closed authorization against policy allowlists,
append-only audit written BEFORE delivery, injected signal sender so no
temporalio is needed here.
"""
from __future__ import annotations

import sqlite3

import pytest

from bin import approvals, catalog

@pytest.fixture
def gateway(tmp_data_home):
    catalog.init()
    return approvals

def _policy(approvers=("changeme",), scope="release:*", status="active"):
    name = f"approval-{'-'.join(approvers)}-{scope}"
    pid = catalog.create_policy(
        name=name,
        kind="approval",
        rule={"approvers": list(approvers), "scope": scope},
    )
    if status != "active":
        catalog.archive_policy(name)
    return pid

WFID = "release:unity-explorer:abc123"

def test_no_policy_means_fail_closed(gateway):
    with pytest.raises(approvals.ApprovalDenied, match="fail closed"):
        gateway.authorize("changeme", WFID)

def test_blank_principal_denied(gateway):
    _policy()
    with pytest.raises(approvals.ApprovalDenied, match="no authenticated principal"):
        gateway.authorize("", WFID)
    with pytest.raises(approvals.ApprovalDenied):
        gateway.authorize("   ", WFID)

def test_unlisted_principal_denied(gateway):
    _policy(approvers=("changeme",))
    with pytest.raises(approvals.ApprovalDenied, match="not an allowlisted approver"):
        gateway.authorize("mallory", WFID)

def test_scope_glob_must_match(gateway):
    _policy(approvers=("changeme",), scope="release:other-repo:*")
    with pytest.raises(approvals.ApprovalDenied):
        gateway.authorize("changeme", WFID)

def test_allowlisted_principal_authorized(gateway):
    pid = _policy(approvers=("changeme", "colleague"))
    assert gateway.authorize("changeme", WFID) == pid

def test_archived_policy_grants_nothing(gateway):
    _policy(status="archived")
    with pytest.raises(approvals.ApprovalDenied):
        gateway.authorize("changeme", WFID)

def test_malformed_rule_never_grants(gateway):
    catalog.create_policy(name="broken", kind="approval", rule="not-json{")
    with pytest.raises(approvals.ApprovalDenied):
        gateway.authorize("changeme", WFID)

def test_submit_records_audit_then_delivers(gateway):
    _policy()
    sent = []
    result = gateway.submit_decision(
        workflow_id=WFID,
        approved=True,
        principal="changeme",
        reason="canary verified",
        signal_sender=lambda wf, ok, who, why: sent.append((wf, ok, who, why)),
    )
    assert sent == [(WFID, True, "changeme", "canary verified")]
    events = gateway.list_events(WFID)
    assert len(events) == 1
    assert events[0]["decision"] == "approved"
    assert events[0]["approver"] == "changeme"
    assert events[0]["id"] == result["event_id"]

def test_denied_submission_sends_nothing_records_nothing(gateway):
    _policy(approvers=("changeme",))
    sent = []
    with pytest.raises(approvals.ApprovalDenied):
        gateway.submit_decision(
            workflow_id=WFID,
            approved=True,
            principal="mallory",
            signal_sender=lambda *a: sent.append(a),
        )
    assert sent == []
    assert gateway.list_events(WFID) == []

def test_failed_delivery_preserves_audit_intent(gateway):
    """Audit is written BEFORE the Signal: a failed send leaves the recorded
    intent for operator reconciliation instead of losing the decision."""
    _policy()

    def exploding_sender(*_a):
        raise ConnectionError("temporal unreachable")

    with pytest.raises(ConnectionError):
        gateway.submit_decision(
            workflow_id=WFID,
            approved=False,
            principal="changeme",
            reason="known regression",
            signal_sender=exploding_sender,
        )
    events = gateway.list_events(WFID)
    assert len(events) == 1
    assert events[0]["decision"] == "rejected"

def test_rejection_decision_recorded_as_rejected(gateway):
    _policy()
    gateway.submit_decision(
        workflow_id=WFID, approved=False, principal="changeme",
        signal_sender=lambda *a: None,
    )
    assert gateway.list_events(WFID)[0]["decision"] == "rejected"

def test_audit_rows_cannot_be_updated_or_deleted(gateway, tmp_data_home):
    _policy()
    gateway.submit_decision(
        workflow_id=WFID, approved=True, principal="changeme",
        signal_sender=lambda *a: None,
    )
    conn = sqlite3.connect(str(tmp_data_home / "judgements.db"))
    try:
        with pytest.raises(sqlite3.DatabaseError, match="immutable"):
            conn.execute("UPDATE approval_event SET decision='rejected' WHERE id=1")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM approval_event WHERE id=1")
    finally:
        conn.close()

def _raw_policy(name, rule_json):
    """Insert a policy row with raw (possibly malformed) JSON text."""
    import sqlite3 as _sq
    import os as _os
    conn = _sq.connect(str(_os.environ["DATA_TOURNAMENTS_HOME"] + "/judgements.db"))
    try:
        conn.execute(
            "INSERT INTO policy(name, kind, rule) VALUES (?, 'approval', ?)",
            (name, rule_json),
        )
        conn.commit()
    finally:
        conn.close()

def test_string_approvers_never_substring_grant(gateway):
    """{"approvers": "changeme"} + `in` = substring matching: 'chan' (and even
    'changeme') must be DENIED because the shape is malformed."""
    _raw_policy("bad-str", '{"approvers": "changeme", "scope": "release:*"}')
    with pytest.raises(approvals.ApprovalDenied):
        gateway.authorize("chan", WFID)
    with pytest.raises(approvals.ApprovalDenied):
        gateway.authorize("changeme", WFID)

def test_non_string_approver_entries_deny(gateway):
    _raw_policy("bad-mixed", '{"approvers": ["changeme", 42], "scope": "*"}')
    with pytest.raises(approvals.ApprovalDenied):
        gateway.authorize("changeme", WFID)

def test_empty_string_approver_entries_deny(gateway):
    _raw_policy("bad-empty", '{"approvers": ["changeme", " "], "scope": "*"}')
    with pytest.raises(approvals.ApprovalDenied):
        gateway.authorize("changeme", WFID)

def test_non_string_scope_denies_not_crashes(gateway):
    """Python previously raised TypeError on non-string scope; the Elixir
    mirror previously widened it to '*'. Both must simply DENY."""
    shapes = {
        "bad-scope-int": '{"approvers": ["changeme"], "scope": 42}',
        "bad-scope-null": '{"approvers": ["changeme"], "scope": null}',
        "bad-scope-obj": '{"approvers": ["changeme"], "scope": {"glob": "*"}}',
    }
    for name, rule_json in shapes.items():
        _raw_policy(name, rule_json)
    with pytest.raises(approvals.ApprovalDenied):
        gateway.authorize("changeme", WFID)

def test_charclass_scope_rejected(gateway):
    """[seq] glob syntax is outside the documented contract — deny, don't
    let fnmatch semantics widen the scope."""
    _raw_policy("bad-class", '{"approvers": ["changeme"], "scope": "release:[au]*"}')
    with pytest.raises(approvals.ApprovalDenied):
        gateway.authorize("changeme", "release:unity-explorer:x")

def test_missing_scope_defaults_to_star(gateway):
    _raw_policy("no-scope", '{"approvers": ["changeme"]}')
    assert gateway.authorize("changeme", WFID) > 0

def test_question_mark_glob_still_works(gateway):
    _raw_policy("qmark", '{"approvers": ["changeme"], "scope": "release:unity-?xplorer:*"}')
    assert gateway.authorize("changeme", "release:unity-explorer:abc") > 0
    with pytest.raises(approvals.ApprovalDenied):
        gateway.authorize("changeme", "release:unity-exxplorer:abc")

"""Subprocess round-trip tests for bin/landscape_mcp.py (landscape MCP v1).

Each test drives the real server binary over stdin/stdout JSON-RPC against a
tmp DATA_TOURNAMENTS_HOME seeded through bin.catalog — no network, no LM.

Spec coverage (docs/specs/landscape-mcp-v1.md):
  R1 — digest resources immutable / mutable entities carry updated_at
  R2 — trust_tier never stripped from evidence payloads
  R3 — no unfiltered-pack resource; packs digest-addressed only
  T1/T2 — capability allowlist, deny-by-default
  T3 — signal_approval is human-only, hard error for any caller
  S1 — no secret values in any response payload
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER = REPO_ROOT / "bin" / "landscape_mcp.py"
ASSEMBLE_PACK_SCRIPT = REPO_ROOT / "bin" / "assemble_pack.py"

# Planted secret value: must NEVER appear in any server response (S1).
PLANTED_SECRET = "sk-SUPERSECRET-b6f2c9e1"


# ── seeded catalog ────────────────────────────────────────────────────────


@pytest.fixture
def seeded(tmp_data_home):
    """Seed a project + source + evidence + snapshot + pack via bin.catalog."""
    from bin import catalog
    from bin.landscape import (
        EvidenceRef,
        LandscapeSnapshot,
        Role,
        SourceType,
        TrustTier,
        build_pack,
    )

    catalog.init()
    pid = catalog.create_project(name="unity-explorer", description="release platform")
    catalog.create_component(project="unity-explorer", name="ui", kind="phoenix")
    sid = catalog.create_source(
        project="unity-explorer",
        name="repo",
        kind="git_repo",
        locator="https://github.com/example/unity-explorer",
        trust_tier=1,
        # S1 bait: a secret-looking config value that must never be served.
        config={"api_key": PLANTED_SECRET, "branch": "main"},
    )

    ev_internal = EvidenceRef(
        source_type=SourceType.DOC,
        canonical_uri="doc://unity-explorer/release-notes",
        trust_tier=TrustTier.TIER2_INTERNAL,
        excerpt="internal release notes excerpt",
        why_selected="release context",
    )
    ev_external = EvidenceRef(
        source_type=SourceType.GITHUB_ISSUE,
        canonical_uri="https://github.com/example/unity-explorer/issues/7",
        trust_tier=TrustTier.TIER3_EXTERNAL,
        excerpt="third-party issue text (untrusted)",
        why_selected="user-reported bug",
    )
    catalog.insert_evidence_ref(ev_internal, source_id=sid)
    catalog.insert_evidence_ref(ev_external, source_id=sid)

    snapshot = LandscapeSnapshot(
        project="unity-explorer",
        created_at="2026-08-17T00:00:00Z",
        evidence=(ev_internal, ev_external),
    )
    snap_digest = catalog.insert_landscape_snapshot(snapshot, project_id=pid)
    for ref in snapshot.evidence:
        catalog.link_snapshot_evidence(snap_digest, ref.digest)

    pack = build_pack(snapshot, Role.CREATOR)
    pack_digest = catalog.insert_context_pack(pack)

    return {
        "project_id": pid,
        "source_id": sid,
        "evidence_internal": ev_internal.digest,
        "evidence_external": ev_external.digest,
        "snapshot_digest": snap_digest,
        "pack_digest": pack_digest,
        "pack_role": "creator",
    }


# ── subprocess JSON-RPC client ────────────────────────────────────────────


class McpClient:
    def __init__(self, capabilities: str = ""):
        cmd = [sys.executable, str(SERVER)]
        if capabilities:
            cmd += ["--capabilities", capabilities]
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=os.environ.copy(),  # carries the monkeypatched DATA_TOURNAMENTS_HOME
            cwd=str(REPO_ROOT),
        )
        self._id = 0
        self.raw_responses: list[str] = []

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        frame = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            frame["params"] = params
        self.proc.stdin.write(json.dumps(frame) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, f"server closed stdout on {method} (stderr: {self.proc.stderr.read()[:500]})"
        self.raw_responses.append(line)
        resp = json.loads(line)  # every stdout line must be a clean JSON frame
        assert resp["jsonrpc"] == "2.0"
        assert resp["id"] == self._id
        return resp

    def initialize(self) -> dict:
        return self.request("initialize", {"protocolVersion": "2024-11-05"})

    def read(self, uri: str) -> dict:
        return self.request("resources/read", {"uri": uri})

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments or {}})

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


@pytest.fixture
def client(seeded):
    c = McpClient()
    yield c
    c.close()


def _payload(resp: dict) -> dict:
    """Decode the JSON body of a resources/read result."""
    assert "result" in resp, f"expected result, got: {resp.get('error')}"
    item = resp["result"]["contents"][0]
    assert item["mimeType"] == "application/json"
    return json.loads(item["text"])


# ── handshake ─────────────────────────────────────────────────────────────


class TestHandshake:
    def test_initialize(self, client):
        resp = client.initialize()
        result = resp["result"]
        assert result["protocolVersion"] == "2024-11-05"
        assert result["serverInfo"]["name"] == "landscape"
        assert "resources" in result["capabilities"]
        assert "tools" in result["capabilities"]

    def test_unknown_method_is_clean_error(self, client):
        client.initialize()
        resp = client.request("bogus/method")
        assert resp["error"]["code"] == -32601
        assert "Traceback" not in json.dumps(resp)


# ── resources ─────────────────────────────────────────────────────────────


class TestResources:
    def test_list_enumerates_indexes_and_mutable_entities(self, client, seeded):
        client.initialize()
        resp = client.request("resources/list")
        uris = [r["uri"] for r in resp["result"]["resources"]]
        assert "landscape://projects" in uris
        assert "landscape://skills" in uris
        assert f"landscape://projects/{seeded['project_id']}" in uris
        assert f"landscape://sources/{seeded['source_id']}" in uris
        # R3: packs are never enumerated.
        assert not any(u.startswith("landscape://packs") for u in uris)

    def test_read_project_index(self, client, seeded):
        client.initialize()
        body = _payload(client.read("landscape://projects"))
        [proj] = body["projects"]
        assert proj["name"] == "unity-explorer"
        assert proj["components"] == ["ui"]
        assert proj["source_count"] == 1

    def test_read_project_full_entry_carries_updated_at(self, client, seeded):
        client.initialize()
        body = _payload(client.read(f"landscape://projects/{seeded['project_id']}"))
        assert body["name"] == "unity-explorer"
        assert body["updated_at"]  # R1: mutable entity carries updated_at
        assert [s["name"] for s in body["sources"]] == ["repo"]
        assert [c["name"] for c in body["components"]] == ["ui"]

    def test_read_source_carries_updated_at_and_trust_tier(self, client, seeded):
        client.initialize()
        body = _payload(client.read(f"landscape://sources/{seeded['source_id']}"))
        assert body["name"] == "repo"
        assert body["project"] == "unity-explorer"
        assert body["trust_tier"] == 1
        assert body["updated_at"]  # R1: mutable entity carries updated_at

    def test_read_snapshot_by_digest(self, client, seeded):
        client.initialize()
        body = _payload(client.read(f"landscape://snapshots/{seeded['snapshot_digest']}"))
        assert body["digest"] == seeded["snapshot_digest"]
        assert body["snapshot"]["project"] == "unity-explorer"
        assert sorted(body["evidence_digests"]) == sorted(
            [seeded["evidence_internal"], seeded["evidence_external"]]
        )

    def test_read_pack_by_digest_as_assembled(self, client, seeded):
        client.initialize()
        body = _payload(client.read(f"landscape://packs/{seeded['pack_digest']}"))
        assert body["digest"] == seeded["pack_digest"]
        assert body["role"] == seeded["pack_role"]
        assert body["snapshot_digest"] == seeded["snapshot_digest"]
        # R1: served exactly as assembled — evidence listed by digest.
        assert sorted(body["pack"]["evidence"]) == sorted(
            [seeded["evidence_internal"], seeded["evidence_external"]]
        )

    def test_read_evidence_r2_trust_tier_present(self, client, seeded):
        """R2: trust_tier must survive serving, at row AND payload level."""
        client.initialize()
        for digest, tier_int, tier_str in [
            (seeded["evidence_internal"], 2, "tier2_internal"),
            (seeded["evidence_external"], 3, "tier3_external"),
        ]:
            body = _payload(client.read(f"landscape://evidence/{digest}"))
            assert body["trust_tier"] == tier_int
            assert body["evidence"]["trust_tier"] == tier_str

    def test_read_skills_index(self, client):
        client.initialize()
        body = _payload(client.read("landscape://skills"))
        names = {s["name"] for s in body["skills"]}
        assert "assemble-project-context" in names
        for s in body["skills"]:
            assert s["version"], f"skill {s['name']} missing version"
            assert s["description"], f"skill {s['name']} missing description"

    def test_r3_no_unfiltered_pack_resource(self, client, seeded):
        """R3: bare landscape://packs (a would-be index / unfiltered read)
        must be refused; packs exist only by digest."""
        client.initialize()
        resp = client.read("landscape://packs")
        assert resp["error"]["code"] == -32002
        assert "digest" in resp["error"]["message"]

    def test_unknown_uri_error_shape(self, client):
        client.initialize()
        for uri in [
            "landscape://nonsense/1",
            "landscape://snapshots/deadbeef",
            "landscape://projects/999",
            "other://scheme",
        ]:
            resp = client.read(uri)
            assert "error" in resp, uri
            assert resp["error"]["code"] in (-32002,), uri
            assert "Traceback" not in json.dumps(resp), uri


# ── tools: capability gating (T1/T2) ─────────────────────────────────────


class TestCapabilityGating:
    def test_tools_list_empty_without_capabilities(self, client):
        client.initialize()  # default session: no --capabilities
        resp = client.request("tools/list")
        assert resp["result"]["tools"] == []  # T2 deny-by-default

    def test_tools_list_shows_only_granted(self, seeded):
        c = McpClient(capabilities="assemble_pack")
        try:
            c.initialize()
            names = [t["name"] for t in c.request("tools/list")["result"]["tools"]]
            assert names == ["assemble_pack"]
        finally:
            c.close()

    def test_call_without_capability_is_denied(self, client):
        client.initialize()
        resp = client.call_tool(
            "assemble_pack",
            {"project_id": "unity-explorer", "role": "creator", "objective": "x"},
        )
        assert resp["error"]["code"] == -32010
        assert "capability" in resp["error"]["message"].lower()

    def test_call_with_capability_dispatches(self, seeded):
        """With the capability granted the call passes the gate and reaches
        the handler (a result frame, never a CAPABILITY_DENIED error)."""
        c = McpClient(capabilities="assemble_pack")
        try:
            c.initialize()
            resp = c.call_tool(
                "assemble_pack",
                {"project_id": "unity-explorer", "role": "creator", "objective": "x"},
            )
            assert "result" in resp  # dispatched; backend availability may vary
        finally:
            c.close()

    @pytest.mark.skipif(
        not ASSEMBLE_PACK_SCRIPT.exists(),
        reason="bin/assemble_pack.py not on disk yet (built in parallel wave)",
    )
    def test_assemble_pack_integration_returns_digests(self, seeded, tmp_path):
        # Give the project a source the git_local adapter can actually
        # collect from: a real throwaway git repo with one commit.
        from bin import catalog

        repo = tmp_path / "toy-repo"
        repo.mkdir()
        (repo / "README.md").write_text("# toy\n", encoding="utf-8")
        env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        for cmd in (["git", "init", "-q"], ["git", "add", "."], ["git", "commit", "-qm", "init"]):
            subprocess.run(cmd, cwd=repo, env=env, check=True, capture_output=True)
        catalog.create_source(
            project="unity-explorer",
            name="toy-git",
            kind="git",
            locator=str(repo),
            trust_tier=1,
        )

        c = McpClient(capabilities="assemble_pack")
        try:
            c.initialize()
            resp = c.call_tool(
                "assemble_pack",
                {
                    "project_id": "unity-explorer",
                    "role": "creator",
                    "objective": "test objective",
                },
            )
            result = resp["result"]
            assert not result.get("isError"), result
            body = json.loads(result["content"][0]["text"])
            assert body["snapshot_digest"]
            assert body["pack_digests"]["creator"]
            # Returned digests resolve as immutable resources (R1).
            pack = _payload(c.read(f"landscape://packs/{body['pack_digests']['creator']}"))
            assert pack["snapshot_digest"] == body["snapshot_digest"]
        finally:
            c.close()

    def test_unknown_tool_error_shape(self, seeded):
        c = McpClient(capabilities="assemble_pack,launch_rockets")
        try:
            c.initialize()
            resp = c.call_tool("launch_rockets", {})
            assert resp["error"]["code"] == -32601
            assert "unknown tool" in resp["error"]["message"]
        finally:
            c.close()


# ── tools: signal_approval (T3) and inspect_run ──────────────────────────


class TestSignalApproval:
    @pytest.mark.parametrize("caps", ["", "signal_approval"])
    def test_always_hard_error_human_only(self, seeded, caps):
        """T3: hard error for ANY caller — with or without the capability —
        because this server cannot authenticate a human Phoenix principal."""
        c = McpClient(capabilities=caps)
        try:
            c.initialize()
            resp = c.call_tool(
                "signal_approval",
                {"workflow_id": "wf-1", "decision": "approve", "reason": "lgtm"},
            )
            assert "error" in resp
            msg = resp["error"]["message"]
            assert resp["error"]["code"] == -32011
            assert "human" in msg.lower()
            assert "T3" in msg
        finally:
            c.close()


class TestInspectRun:
    def test_not_implemented_stub(self, seeded):
        c = McpClient(capabilities="inspect_run")
        try:
            c.initialize()
            resp = c.call_tool("inspect_run", {"workflow_id": "wf-1"})
            assert resp["error"]["code"] == -32012
            assert "not implemented" in resp["error"]["message"].lower()
        finally:
            c.close()


# ── S1: no secrets anywhere ──────────────────────────────────────────────


class TestSecretHygiene:
    def test_no_secret_values_in_any_response(self, seeded):
        """Drive every endpoint and scan every raw stdout frame for the
        planted secret value (S1)."""
        c = McpClient(capabilities="assemble_pack,inspect_run,signal_approval")
        try:
            c.initialize()
            c.request("resources/list")
            c.read("landscape://projects")
            c.read(f"landscape://projects/{seeded['project_id']}")
            c.read(f"landscape://sources/{seeded['source_id']}")
            c.read(f"landscape://snapshots/{seeded['snapshot_digest']}")
            c.read(f"landscape://packs/{seeded['pack_digest']}")
            c.read(f"landscape://evidence/{seeded['evidence_internal']}")
            c.read("landscape://skills")
            c.request("tools/list")
            c.call_tool(
                "assemble_pack",
                {"project_id": "unity-explorer", "role": "creator", "objective": "x"},
            )
            c.call_tool("inspect_run", {"workflow_id": "wf-1"})
            c.call_tool("signal_approval", {"workflow_id": "wf-1", "decision": "approve"})

            everything = "".join(c.raw_responses)
            assert PLANTED_SECRET not in everything, "S1 violation: secret leaked"
            assert "SUPERSECRET" not in everything
        finally:
            c.close()

    def test_source_secret_field_is_redacted_by_name(self, client, seeded):
        client.initialize()
        body = _payload(client.read(f"landscape://sources/{seeded['source_id']}"))
        assert body["config"]["api_key"].startswith("secret://")  # name-only ref
        assert body["config"]["branch"] == "main"  # non-secret survives

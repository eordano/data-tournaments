#!/usr/bin/env python3
"""Landscape MCP server v1 — Resources + Tools subset (stdio JSON-RPC).

Implements the v1 surface of docs/specs/landscape-mcp-v1.md: the
``initialize`` handshake, ``resources/list`` / ``resources/read`` for the
landscape:// resource set, and ``tools/list`` / ``tools/call`` for the
assemble_pack / inspect_run / signal_approval subset.

PROTOCOL IMPLEMENTATION PATH: the python ``mcp`` package is NOT importable
in this dev shell, so this module speaks the MCP protocol subset directly
over stdin/stdout using a stdlib JSON-RPC 2.0 loop (json + sys), following
the convention set by bin/hermes_mcp_server.py. stdout carries ONLY
JSON-RPC frames; diagnostics go to stderr. No network, no LM.

Spec rules enforced here
------------------------
R1  Digest-addressed resources (snapshots, packs, evidence) are immutable
    and served verbatim as stored; mutable entities (projects, sources)
    carry ``updated_at`` in their payloads.
R2  Evidence payloads always carry ``trust_tier`` — never stripped, present
    both at the row level (int) and inside the canonical payload (enum str).
R3  Packs are served ONLY by digest, exactly as assembled (role-shaped at
    assembly time). There is no pack index and no "unfiltered pack" URI.
T1/T2  Tools are capability-scoped and deny-by-default: the session's
    capability set comes from ``--capabilities`` (comma-separated allowlist,
    default empty). ``tools/list`` shows only in-capability tools;
    ``tools/call`` outside the set is rejected with CAPABILITY_DENIED.
T3  ``signal_approval`` is HUMAN-ONLY: approvals authenticate a human
    Phoenix principal (session token in ``_meta``), which this v1 server
    cannot do — every call gets a hard error, capability or not.
T4  ``launch_sandbox`` is not part of the v1 tool subset; its pack-role
    validation lands with the tool itself.
S1  No secret values in any response: dict values whose keys look
    secret-like are redacted recursively before serialization; secrets are
    referenced by name only.

All catalog reads go through bin.catalog (ADR 0001 §2: Python owns the
schema; this server is a reader, never a writer).

Usage:  python3 bin/landscape_mcp.py [--capabilities assemble_pack,inspect_run]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bin import catalog  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "landscape", "version": "1.0.0"}
SKILLS_DIR = _REPO_ROOT / "skills"
ASSEMBLE_PACK_SCRIPT = _REPO_ROOT / "bin" / "assemble_pack.py"

assemble_pack_hook: Optional[Callable[[str, str, str], dict]] = None

METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
RESOURCE_NOT_FOUND = -32002
CAPABILITY_DENIED = -32010
HUMAN_ONLY = -32011
NOT_IMPLEMENTED = -32012

_SECRET_KEY_RE = re.compile(
    r"(?:^|_|-)(?:secret|token|password|passwd|api_?key|credential|"
    r"authorization|auth|bearer|private_?key|signing_?key)s?(?:$|_|-)",
    re.IGNORECASE,
)

def _scrub(obj: Any) -> Any:
    """Recursively redact values whose keys look secret-like (S1).

    Values are replaced with a name-only reference; nothing resembling the
    secret value ever reaches stdout.
    """
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and _SECRET_KEY_RE.search(k) and isinstance(
                v, (str, int, float)
            ):
                out[k] = f"secret://{k} (value redacted per S1)"
            else:
                out[k] = _scrub(v)
        return out
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    if isinstance(obj, tuple):
        return [_scrub(v) for v in obj]
    return obj

def send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()

def ok(rid: Any, result: dict) -> None:
    send({"jsonrpc": "2.0", "id": rid, "result": result})

def err(rid: Any, code: int, message: str, data: Optional[dict] = None) -> None:
    e: dict = {"code": code, "message": message}
    if data is not None:
        e["data"] = data
    send({"jsonrpc": "2.0", "id": rid, "error": e})

def _json_contents(uri: str, payload: Any) -> dict:
    """resources/read result body: one JSON text content item, S1-scrubbed."""
    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": "application/json",
                "text": json.dumps(_scrub(payload), default=str),
            }
        ]
    }

def _text_result(payload: Any, is_error: bool = False) -> dict:
    """tools/call result body (MCP content shape), S1-scrubbed."""
    text = payload if isinstance(payload, str) else json.dumps(_scrub(payload), default=str)
    r: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        r["isError"] = True
    return r

class ResourceNotFound(LookupError):
    pass

def _project_by_ref(ref: str) -> dict:
    """Resolve {id} as numeric row id or project name."""
    if ref.isdigit():
        for p in catalog.list_projects("active") + catalog.list_projects("archived"):
            if p["id"] == int(ref):
                return p
        raise ResourceNotFound(f"no project with id {ref}")
    try:
        return catalog.get_project(ref)
    except LookupError as e:
        raise ResourceNotFound(str(e))

def read_projects_index(_rest: str) -> Any:
    out = []
    for p in catalog.list_projects("active"):
        components = catalog.list_components(p["name"])
        sources = catalog.list_sources(p["name"])
        out.append(
            {
                "id": p["id"],
                "name": p["name"],
                "components": [c["name"] for c in components],
                "source_count": len(sources),
                "updated_at": p.get("updated_at"),
            }
        )
    return {"projects": out}

def read_project(ref: str) -> Any:
    p = _project_by_ref(ref)
    name = p["name"]
    entry = dict(p)
    entry["components"] = catalog.list_components(name)
    entry["sources"] = catalog.list_sources(name)
    entry["capabilities"] = catalog.list_capabilities()
    entry["environments"] = catalog.list_environments()
    entry["policies"] = catalog.list_policies()
    return entry

def read_source(ref: str) -> Any:
    if not ref.isdigit():
        raise ResourceNotFound(f"source id must be numeric, got {ref!r}")
    sid = int(ref)
    for p in catalog.list_projects("active") + catalog.list_projects("archived"):
        for s in catalog.list_sources(p["name"]) + catalog.list_sources(
            p["name"], "archived"
        ):
            if s["id"] == sid:
                s = dict(s)
                s["project"] = p["name"]
                return s
    raise ResourceNotFound(f"no source with id {sid}")

def read_snapshot(digest: str) -> Any:
    try:
        row = catalog.get_landscape_snapshot(digest)
    except LookupError as e:
        raise ResourceNotFound(str(e))
    return {
        "digest": row["digest"],
        "project_id": row["project_id"],
        "schema_version": row["schema_version"],
        "snapshot": json.loads(row["manifest"]),
        "evidence_digests": catalog.list_snapshot_evidence(digest),
    }

def read_pack(digest: str) -> Any:
    try:
        row = catalog.get_context_pack(digest)
    except LookupError as e:
        raise ResourceNotFound(str(e))
    return {
        "digest": row["digest"],
        "role": row["role"],
        "snapshot_digest": row["snapshot_digest"],
        "schema_version": row["schema_version"],
        "pack": json.loads(row["manifest"]),
    }

def read_evidence(ref: str) -> Any:
    try:
        row = catalog.get_evidence_ref(ref)
    except LookupError as e:
        raise ResourceNotFound(str(e))
    payload = json.loads(row["body"])
    assert "trust_tier" in payload, "R2 violation: evidence payload lost trust_tier"
    return {
        "digest": row["digest"],
        "source_id": row["source_id"],
        "kind": row["kind"],
        "locator": row["locator"],
        "trust_tier": row["trust_tier"],
        "summary": row["summary"],
        "evidence": payload,
    }

def _parse_frontmatter(text: str) -> dict:
    """Minimal YAML frontmatter reader: `key: value` lines between --- fences."""
    meta: dict = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return meta
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    return meta

def read_skills_index(_rest: str) -> Any:
    skills = []
    if SKILLS_DIR.is_dir():
        for skill_md in sorted(SKILLS_DIR.glob("*/SKILL.md")):
            meta = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
            skills.append(
                {
                    "name": meta.get("name", skill_md.parent.name),
                    "version": meta.get("version", ""),
                    "description": meta.get("description", ""),
                }
            )
    return {"skills": skills}

def read_resource(uri: str) -> dict:
    """Dispatch a landscape:// URI to its reader. Raises ResourceNotFound."""
    if not uri.startswith("landscape://"):
        raise ResourceNotFound(f"unknown URI scheme: {uri!r}")
    rest = uri[len("landscape://"):]
    entity, _, ref = rest.partition("/")
    if entity == "projects" and not ref:
        return _json_contents(uri, read_projects_index(ref))
    if entity == "projects":
        return _json_contents(uri, read_project(ref))
    if entity == "sources" and ref:
        return _json_contents(uri, read_source(ref))
    if entity == "snapshots" and ref:
        return _json_contents(uri, read_snapshot(ref))
    if entity == "packs" and ref:
        return _json_contents(uri, read_pack(ref))
    if entity == "packs":
        raise ResourceNotFound(
            "packs are digest-addressed only (landscape://packs/{digest}); "
            "there is no pack index or unfiltered-pack resource (R3)"
        )
    if entity == "evidence" and ref:
        return _json_contents(uri, read_evidence(ref))
    if entity == "skills" and not ref:
        return _json_contents(uri, read_skills_index(ref))
    raise ResourceNotFound(f"unknown resource URI: {uri!r}")

def list_resources() -> list[dict]:
    """Enumerable resources: indexes + mutable catalog entities.

    Digest-addressed artifacts (snapshots, packs, evidence) are reachable
    only through digests obtained from other reads/tools — packs in
    particular are never enumerated (R3).
    """
    resources = [
        {
            "uri": "landscape://projects",
            "name": "Project index",
            "description": "All active projects: id, name, components, source count",
            "mimeType": "application/json",
        },
        {
            "uri": "landscape://skills",
            "name": "Skill index",
            "description": "Available Agent Skills (name, version, description)",
            "mimeType": "application/json",
        },
    ]
    for p in catalog.list_projects("active"):
        resources.append(
            {
                "uri": f"landscape://projects/{p['id']}",
                "name": f"Project: {p['name']}",
                "description": p.get("description") or "",
                "mimeType": "application/json",
            }
        )
        for s in catalog.list_sources(p["name"]):
            resources.append(
                {
                    "uri": f"landscape://sources/{s['id']}",
                    "name": f"Source: {p['name']}/{s['name']}",
                    "description": f"{s['kind']} source (trust tier {s['trust_tier']})",
                    "mimeType": "application/json",
                }
            )
    return resources

TOOLS: dict[str, dict] = {
    "assemble_pack": {
        "name": "assemble_pack",
        "description": (
            "Build an immutable LandscapeSnapshot and role-shaped ContextPack "
            "for a project objective; returns their digests."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "role": {"type": "string", "enum": ["creator", "judge", "executor"]},
                "objective": {"type": "string"},
            },
            "required": ["project_id", "role", "objective"],
        },
    },
    "inspect_run": {
        "name": "inspect_run",
        "description": "Stage history + artifacts index for a workflow run.",
        "inputSchema": {
            "type": "object",
            "properties": {"workflow_id": {"type": "string"}},
            "required": ["workflow_id"],
        },
    },
    "signal_approval": {
        "name": "signal_approval",
        "description": (
            "Send an approval/rejection Signal to a workflow. HUMAN-ONLY (T3): "
            "requires an authenticated human Phoenix principal."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
                "decision": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["workflow_id", "decision"],
        },
    },
}

def tool_assemble_pack(args: dict) -> dict:
    project_id = args.get("project_id")
    role = args.get("role")
    objective = args.get("objective")
    if not all(isinstance(v, str) and v for v in (project_id, role, objective)):
        return _text_result(
            "ERROR: assemble_pack requires non-empty string project_id, role, objective",
            is_error=True,
        )
    if ASSEMBLE_PACK_SCRIPT.exists():
        proc = subprocess.run(
            [
                sys.executable,
                str(ASSEMBLE_PACK_SCRIPT),
                "--project",
                project_id,
                "--roles",
                role,
                "--objective",
                objective,
            ],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
        )
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            return _text_result(
                f"ERROR: assemble_pack exited {proc.returncode}: " + " | ".join(tail),
                is_error=True,
            )
        for out_line in reversed(proc.stdout.strip().splitlines()):
            if out_line.startswith("PACK_JSON: "):
                return _text_result(json.loads(out_line[len("PACK_JSON: "):]))
        return _text_result(proc.stdout.strip())
    if assemble_pack_hook is not None:
        return _text_result(assemble_pack_hook(project_id, role, objective))
    return _text_result(
        "ERROR: assemble_pack backend unavailable: bin/assemble_pack.py not "
        "present and no in-process hook registered",
        is_error=True,
    )

TOOL_HANDLERS = {"assemble_pack": tool_assemble_pack}

def handle_tools_call(rid: Any, params: dict, capabilities: frozenset) -> None:
    name = params.get("name")
    args = params.get("arguments") or {}
    if name not in TOOLS:
        err(rid, METHOD_NOT_FOUND, f"unknown tool: {name!r}")
        return
    if name == "signal_approval":
        err(
            rid,
            HUMAN_ONLY,
            "signal_approval is human-only (T3): approvals must authenticate "
            "a human Phoenix principal via a Phoenix session token in _meta, "
            "which this v1 server cannot do. Agent sessions can never "
            "approve; untrusted text never approves anything.",
        )
        return
    if name not in capabilities:
        err(
            rid,
            CAPABILITY_DENIED,
            f"tool {name!r} is not in this session's capability set "
            "(T1 capability scoping; T2 deny-by-default)",
        )
        return
    if name == "inspect_run":
        err(
            rid,
            NOT_IMPLEMENTED,
            "inspect_run is not implemented in landscape/v1: workflow runs "
            "land with the Temporal integration (wave 4).",
        )
        return
    handler = TOOL_HANDLERS[name]
    ok(rid, handler(args))

def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="landscape_mcp.py", description=__doc__.splitlines()[0])
    p.add_argument(
        "--capabilities",
        default="",
        help="Comma-separated tool allowlist for this session (T1). "
        "Default: empty = deny all (T2).",
    )
    args = p.parse_args(argv)
    capabilities = frozenset(
        c.strip() for c in args.capabilities.split(",") if c.strip()
    )

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            print(f"landscape_mcp: bad json frame: {line[:200]!r}", file=sys.stderr)
            continue
        method = req.get("method")
        rid = req.get("id")
        params = req.get("params") or {}
        try:
            if method == "initialize":
                ok(
                    rid,
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"resources": {}, "tools": {}},
                        "serverInfo": SERVER_INFO,
                    },
                )
            elif method == "notifications/initialized":
                pass
            elif method == "resources/list":
                ok(rid, {"resources": list_resources()})
            elif method == "resources/read":
                uri = params.get("uri")
                if not isinstance(uri, str) or not uri:
                    err(rid, INVALID_PARAMS, "resources/read requires a 'uri' string")
                else:
                    try:
                        ok(rid, read_resource(uri))
                    except ResourceNotFound as e:
                        err(rid, RESOURCE_NOT_FOUND, str(e))
            elif method == "tools/list":
                ok(rid, {"tools": [TOOLS[n] for n in sorted(TOOLS) if n in capabilities]})
            elif method == "tools/call":
                handle_tools_call(rid, params, capabilities)
            elif method == "ping":
                ok(rid, {})
            elif method == "shutdown":
                ok(rid, {})
                return 0
            else:
                if rid is not None:
                    err(rid, METHOD_NOT_FOUND, f"method not found: {method!r}")
        except Exception as e:  # noqa: BLE001 — no stack traces on stdout, ever
            print(f"landscape_mcp: {method} raised: {e!r}", file=sys.stderr)
            if rid is not None:
                err(rid, INTERNAL_ERROR, f"{type(e).__name__}: {e}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

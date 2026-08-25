#!/usr/bin/env python3
"""
Minimal stdio MCP server for the tournament harness — Langfuse edition.

Exposes exactly two tools:
  - read_file(path: str)
        -> file contents (no sandbox); also recorded as a child observation
           under the active trace.
  - pick_winner(winner_id: int, reasoning: str, markdown: str)
        -> declares which of the two inputs wins this match, and submits
           a synthesis markdown. After this call the agent must stop.

Config: a JSON file at $HERMES_HARNESS_CONFIG with the shape
  {
    "trace_id":              "<32-hex-otel-trace-id>",  // required for tracing
    "parent_observation_id": "<16-hex-otel-span-id>",   // optional; child spans hang off this
    "match_label":           "R1-3",                    // for span names
    "n_inputs":              2,                          // 1 (bye) or 2
    "outfile":               "/abs/synthesis.md",       // synthesis IPC channel
    "winner_file":           "/abs/winner.json"         // winner_id IPC channel
  }

The orchestrator (run-tournament.py) opens the trace inside Langfuse.run_experiment
and writes this file BEFORE invoking the agent, then blanks it after.
Trace_id is the unique handle now — old per-token isolation is gone.

If trace_id is missing or LANGFUSE_PUBLIC_KEY is unset, the server still works
in degraded mode: tools behave as before, telemetry is skipped.
"""
from __future__ import annotations

import fcntl
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

CONFIG_PATH = os.environ.get("HERMES_HARNESS_CONFIG", "")
PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "tournament-harness", "version": "0.4.0-langfuse"}

TOOLS = [
    {
        "name": "read_file",
        "description": "Read a file from disk and return its UTF-8 contents.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Absolute path to a file"}},
            "required": ["path"],
        },
    },
    {
        "name": "pick_winner",
        "description": (
            "Declare the winner of this match and submit a synthesis markdown. "
            "winner_id is 1 or 2 — the position of the chosen input in the prompt. "
            "reasoning is a short justification (<=400 chars). markdown is the full "
            "synthesis answer. After calling pick_winner, STOP — do not emit further "
            "text or tool calls."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "winner_id": {
                    "type": "integer",
                    "enum": [1, 2],
                    "description": "Position of the winning input (1 or 2).",
                },
                "reasoning": {
                    "type": "string",
                    "description": "Why this input won. Short — surfaces in the trace UI.",
                },
                "markdown": {
                    "type": "string",
                    "description": "Complete markdown synthesis answer for this match.",
                },
            },
            "required": ["winner_id", "reasoning", "markdown"],
        },
    },
]


# ── stderr-only logging (stdout is reserved for MCP JSON-RPC) ─────────────
_LOG_PATH = os.environ.get("HERMES_HARNESS_SERVER_LOG", "/tmp/data-tournaments/mcp-server.log")
try:
    Path(_LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
except Exception:
    # Fall back to /tmp if the configured path's parent isn't writable.
    _LOG_PATH = "/tmp/tournament-mcp-server.log"
LOGF = open(_LOG_PATH, "a", buffering=1)


def log(msg: str) -> None:
    try:
        LOGF.write(f"[pid {os.getpid()}] {msg}\n")
    except Exception:
        pass


# ── Langfuse client (lazy, optional) ──────────────────────────────────────
_LF = None  # langfuse.Langfuse instance, or None if unavailable
_LF_TRACE_CTX: Optional[dict] = None  # {"trace_id": ..., "parent_span_id": ...}


def _init_langfuse(cfg: dict) -> None:
    """Initialize the Langfuse client + trace context once. Failures are non-fatal."""
    global _LF, _LF_TRACE_CTX
    if _LF is not None or _LF_TRACE_CTX is not None:
        return
    trace_id = cfg.get("trace_id")
    if not trace_id:
        log("langfuse: no trace_id in config; tracing disabled")
        return
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        log("langfuse: env keys missing; tracing disabled")
        return
    try:
        from langfuse import get_client  # type: ignore

        _LF = get_client()
    except Exception as e:
        log(f"langfuse: client init failed: {e!r}; tracing disabled")
        _LF = None
        return
    _LF_TRACE_CTX = {"trace_id": trace_id}
    parent = cfg.get("parent_observation_id")
    if parent:
        _LF_TRACE_CTX["parent_span_id"] = parent
    log(f"langfuse: tracing enabled, trace_id={trace_id[:8]}…")


def _emit_observation(name: str, *, as_type: str, input: Any = None,
                      output: Any = None, level: str = "DEFAULT",
                      status_message: Optional[str] = None) -> None:
    """Emit one finished observation under the active trace."""
    if _LF is None or _LF_TRACE_CTX is None:
        return
    try:
        with _LF.start_as_current_observation(
            trace_context=_LF_TRACE_CTX,
            name=name,
            as_type=as_type,
            input=input,
            output=output,
            level=level,  # type: ignore[arg-type]
            status_message=status_message,
        ):
            pass
    except Exception as e:
        log(f"langfuse: emit {name} failed: {e!r}")


def _emit_score(name: str, value: Any, *, comment: Optional[str] = None,
                data_type: Optional[str] = None) -> None:
    """Attach a score to the current trace. Used to surface winner_id."""
    if _LF is None or _LF_TRACE_CTX is None:
        return
    try:
        kwargs: dict = {"name": name, "value": value, "trace_id": _LF_TRACE_CTX["trace_id"]}
        if comment is not None:
            kwargs["comment"] = comment
        if data_type is not None:
            kwargs["data_type"] = data_type
        _LF.create_score(**kwargs)
    except Exception as e:
        log(f"langfuse: score {name} failed: {e!r}")


# ── MCP wire helpers ──────────────────────────────────────────────────────
def send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def err(rid: Any, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def ok(rid: Any, result: dict) -> None:
    send({"jsonrpc": "2.0", "id": rid, "result": result})


def text_result(text: str, is_error: bool = False) -> dict:
    r: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        r["isError"] = True
    return r


def load_config() -> dict:
    if not CONFIG_PATH or not os.path.exists(CONFIG_PATH):
        return {}
    with open(CONFIG_PATH) as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_SH)
        try:
            return json.load(f)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


# ── Tool handlers ─────────────────────────────────────────────────────────
def handle_read_file(args: dict) -> dict:
    path = args.get("path", "")
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        _emit_observation(
            f"read_file:{Path(path).name or 'invalid'}",
            as_type="tool",
            input={"path": path},
            output=None,
            level="ERROR",
            status_message=f"{type(e).__name__}: {e}",
        )
        return text_result(f"ERROR: {e}", is_error=True)

    snippet = text if len(text) <= 4000 else text[:4000] + f"\n…[truncated {len(text) - 4000} bytes]"
    _emit_observation(
        f"read_file:{Path(path).name}",
        as_type="tool",
        input={"path": path},
        output={"bytes": len(text), "preview": snippet},
    )
    return text_result(text)


def handle_pick_winner(args: dict) -> dict:
    winner_id = args.get("winner_id")
    reasoning = args.get("reasoning", "")
    md = args.get("markdown", "")

    # ── Validate ──────────────────────────────────────────────────────────
    cfg = load_config()
    n_inputs = int(cfg.get("n_inputs") or 2)
    valid_ids = list(range(1, n_inputs + 1)) if n_inputs >= 1 else [1, 2]

    if not isinstance(winner_id, int) or winner_id not in valid_ids:
        msg = f"winner_id must be an integer in {valid_ids}, got {winner_id!r}"
        _emit_observation(
            "pick_winner",
            as_type="tool",
            input={"winner_id": winner_id, "reasoning": reasoning, "markdown_bytes": len(md) if isinstance(md, str) else 0},
            level="ERROR",
            status_message=msg,
        )
        return text_result(f"ERROR: {msg}", is_error=True)
    if not isinstance(reasoning, str) or not reasoning.strip():
        return text_result("ERROR: reasoning must be a non-empty string", is_error=True)
    if not isinstance(md, str) or not md.strip():
        return text_result("ERROR: markdown must be a non-empty string", is_error=True)

    outfile = cfg.get("outfile")
    winner_file = cfg.get("winner_file")
    if not outfile or not winner_file:
        _emit_observation(
            "pick_winner",
            as_type="tool",
            input={"winner_id": winner_id, "markdown_bytes": len(md)},
            level="ERROR",
            status_message="outfile/winner_file not configured",
        )
        return text_result("ERROR: outfile/winner_file not configured", is_error=True)

    # ── Persist via the IPC files ─────────────────────────────────────────
    Path(outfile).write_text(md, encoding="utf-8")
    Path(winner_file).write_text(
        json.dumps({"winner_id": winner_id, "reasoning": reasoning}, indent=2),
        encoding="utf-8",
    )

    # ── Telemetry ─────────────────────────────────────────────────────────
    _emit_observation(
        "pick_winner",
        as_type="tool",
        input={"winner_id": winner_id, "reasoning": reasoning, "markdown_bytes": len(md)},
        output={"markdown": md, "outfile": outfile, "winner_file": winner_file},
    )
    # Surface winner_id as a categorical score so it's filterable in the UI.
    _emit_score(
        "winner_id",
        f"input_{winner_id}",
        comment=reasoning[:200],
        data_type="CATEGORICAL",
    )
    return text_result(f"submitted, winner=input_{winner_id}")


HANDLERS = {"read_file": handle_read_file, "pick_winner": handle_pick_winner}


# ── main loop ─────────────────────────────────────────────────────────────
def main() -> None:
    log(f"startup: CONFIG_PATH={CONFIG_PATH!r}")
    try:
        _init_langfuse(load_config())
    except Exception as e:
        log(f"startup: _init_langfuse raised: {e!r}")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            log(f"bad json: {e}: {line[:200]!r}")
            continue
        log(f"req: {line[:300]}")
        method = req.get("method")
        rid = req.get("id")
        params = req.get("params") or {}

        if method == "initialize":
            ok(rid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            })
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            ok(rid, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            handler = HANDLERS.get(name)
            if handler is None:
                err(rid, -32601, f"unknown tool: {name}")
            else:
                try:
                    ok(rid, handler(args))
                except Exception as e:
                    log(f"handler {name} raised: {e!r}")
                    err(rid, -32603, f"{type(e).__name__}: {e}")
        elif method == "ping":
            ok(rid, {})
        elif method == "shutdown":
            try:
                if _LF is not None:
                    _LF.flush()
            except Exception as e:
                log(f"shutdown flush failed: {e!r}")
            ok(rid, {})
            return
        else:
            if rid is not None:
                err(rid, -32601, f"method not found: {method}")


if __name__ == "__main__":
    try:
        main()
    finally:
        try:
            if _LF is not None:
                _LF.flush()
        except Exception:
            pass

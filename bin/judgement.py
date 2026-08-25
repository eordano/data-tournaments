#!/usr/bin/env python3
"""
judgement.py — judgement-fabric v0 Python side.

Responsibilities:
  1. Schema bootstrap (`init_db`) — creates the central fabric DB at
     $DATA_TOURNAMENTS_HOME/judgements.db and seeds the
     `code-style-tournament` rubric on first run.
  2. Pending-queue helpers (`enqueue_for_match`, `list_pending`).
  3. Score writer (`write_judgement`) — writes 2 Score rows per
     judgement, tagged with a shared rating_id UUID. Used by both the
     LLM-judge worker and the LiveView UI (via a JSON-RPC bridge or
     direct SQLite write — v0 takes the direct path since the LiveView
     and Python both write to the same file).
  4. LLM-judge worker (`run_llm_judge_for_pending`) — claims pending
     rows whose config is rater_type='llm', calls llm-default with the
     `express_judgement` tool, writes scores.

The schema lives at bin/judgement_schema.sql so Python and Elixir can
both reference it as the single source of truth.

Usage as a CLI:
  judgement.py init                           — bootstrap DB + seed rubric
  judgement.py list-pending [--rater-type X]  — show pending rows
  judgement.py run-llm                        — drain the LLM-judge queue
  judgement.py enqueue --tournament-db P --match-id M
                                              — manual enqueue (debug)
  judgement.py export --rubric R [--rater-type X]
                                              — JSONL export to stdout

CLI is a debug aid; the real entry points are the importable functions.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# Direct execution (``python bin/judgement.py``) puts only ``bin/`` on
# sys.path, which breaks later ``from bin import ...`` imports. Keep the CLI
# and module entry points equivalent.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bin import llm_config as _llm_config  # noqa: E402  (needs _REPO_ROOT on sys.path)
from bin.env_loader import load_dotenv as _load_dotenv  # noqa: E402


# ── .env loader ─────────────────────────────────────────────────────────
# Shared loader lives in bin/env_loader.py; every CLI entry point calls it
# explicitly. judgement keeps loading on import for backward compatibility
# (its module import has always implied env setup).
_load_dotenv()

# ── Paths ────────────────────────────────────────────────────────────────
DATA_HOME = Path(os.environ.get("DATA_TOURNAMENTS_HOME", "/tmp/data-tournaments"))
DB_PATH = DATA_HOME / "judgements.db"
SCHEMA_PATH = Path(__file__).parent / "judgement_schema.sql"

# ── Seed rubric: card-prioritizer-v0 ────────────────────────────────────
# Domain-neutral "given two cards, decide which is more worth surfacing."
# Specific domains (code findings, memory extraction, actionables, …)
# share this rubric and select their judge prompt by Langfuse Prompt name.
SEED_TEMPLATE_NAME = "card-prioritizer-v0"
SEED_TEMPLATE_VERSION = 1
SEED_LANGFUSE_PROMPT_NAME = "judge-instructions"
SEED_TEMPLATE_DEFINITION = {
    "verdict_enum": [
        "a-clearly-better",
        "a-marginally-better",
        "tie-both-strong",
        "tie-both-weak",
        "b-marginally-better",
        "b-clearly-better",
        "incoherent",
        "skip",
    ],
    "confidence_enum": ["low", "mid", "high"],
    "rationale_required": False,
    "description": (
        "Pick the card more worth surfacing to the user. Domain-specific "
        "guidance (code-finding vs memory vs actionable, etc.) is selected "
        "via Langfuse Prompt name."
    ),
}

SEED_JUDGE_INSTRUCTIONS = (
    "You are a triage judge in a card-elimination tournament. Each match "
    "shows you two cards (A and B) drawn from the same corpus. Your job is "
    "to pick the one more worth surfacing to a human user.\n\n"
    "Use these criteria, weighted in this order:\n"
    "  1. Specificity — concrete > vague. A card pointing at a real file "
    "and line beats a generic platitude.\n"
    "  2. Novelty — surprising > obvious. A finding the user couldn't have "
    "guessed wins over restating the well-known.\n"
    "  3. Actionability — leads to a clear next step > leaves the user "
    "shrugging.\n"
    "  4. Risk/impact — flags a real problem > nice-to-have.\n\n"
    "Verdicts:\n"
    "  - a-clearly-better / a-marginally-better — card A wins\n"
    "  - tie-both-strong — both deserve to be surfaced; pick A by convention\n"
    "  - tie-both-weak — neither is great; pick A by convention\n"
    "  - b-marginally-better / b-clearly-better — card B wins\n"
    "  - incoherent — one or both cards are malformed (missing title/body, "
    "off-topic, contradictory)\n"
    "  - skip — you genuinely cannot judge (insufficient context)\n\n"
    "Confidence: how sure you are. Default 'mid'."
)

SEED_DOMAIN_BUILDER_INSTRUCTIONS = (
    "You design new card-prioritization domains. Given a one-line "
    "description of what a user wants to extract from a corpus, plus "
    "metadata about the corpus (kind + a few sample items), draft three "
    "things:\n\n"
    "  1. domain_name — short, lowercase, hyphen-separated, ≤64 chars. "
    "Reflects the *content domain*, not the corpus mechanism. Good: "
    "'memory-extraction', 'code-review-findings', 'inbox-actionables'. "
    "Bad: 'sqlite-thing', 'my-domain-1'.\n\n"
    "  2. generator_prompt — the system prompt that turns one corpus item "
    "into 0–N cards. Cards are observation/finding-shaped: each has a "
    "concise title (≤80 chars) and a body (≤400 chars) explaining the "
    "specific thing. Be explicit about what counts as a card-worthy "
    "observation in this domain, and what doesn't (so the LLM doesn't "
    "extract noise). Reference the corpus_samples so the prompt fits the "
    "actual data shape.\n\n"
    "  3. judge_prompt — the system prompt that, given two cards, picks "
    "the one more worth surfacing. Be explicit about what 'more worth "
    "surfacing' means in this specific domain (e.g. memory-extraction "
    "favors durable + generally-applicable; inbox-actionables favors "
    "time-sensitive + blocks-others). Reference the same verdict enum "
    "the seed judge uses (a-clearly-better, a-marginally-better, "
    "tie-both-strong, tie-both-weak, b-marginally-better, "
    "b-clearly-better, incoherent, skip).\n\n"
    "Both prompts should be stand-alone — they will be saved as Langfuse "
    "Prompts and used directly. Don't reference other prompts or external "
    "context the runtime won't have."
)
SEED_DOMAIN_BUILDER_PROMPT_NAME = "domain-builder"

# ── output_definition v2 (docs/design/judgement-wheel-v2.md) ────────────
# Template-JSON only — no schema change. New keys:
#   judgement_kind: 'pair' | 'single'          (absent => 'pair')
#   subjects: ['idea']|['execution']|both      (absent => ['execution'])
#   wheel: {position: verdict}                 (positions n/ne/e/se/s/sw/w/nw;
#                                               every verdict must be in
#                                               verdict_enum; validated at
#                                               template registration)
WHEEL_POSITIONS = ("n", "ne", "e", "se", "s", "sw", "w", "nw")
JUDGEMENT_KINDS = ("pair", "single")
JUDGEMENT_SUBJECTS = ("idea", "execution")


def normalize_output_definition(outdef: dict) -> dict:
    """Return the v2 shape of an output_definition with defaults applied.

    THE single code path for legacy templates: absent judgement_kind means
    'pair', absent subjects means ['execution'], absent wheel means {} —
    exactly the semantics every pre-v2 template always had. Never mutates
    the input; stored JSON stays byte-identical (old rubric versions keep
    their exact stored semantics forever).
    """
    out = dict(outdef or {})
    out["judgement_kind"] = out.get("judgement_kind") or "pair"
    out["subjects"] = list(out.get("subjects") or ["execution"])
    out["wheel"] = dict(out.get("wheel") or {})
    return out


def validate_output_definition(outdef: dict) -> dict:
    """Validate an output_definition at template registration time.

    Returns the normalized v2 shape. Raises ValueError on: unknown
    judgement_kind, unknown/empty subjects, unknown wheel positions, or a
    wheel verdict that is not in verdict_enum.
    """
    norm = normalize_output_definition(outdef)
    if norm["judgement_kind"] not in JUDGEMENT_KINDS:
        raise ValueError(
            f"judgement_kind {norm['judgement_kind']!r} not in {list(JUDGEMENT_KINDS)}"
        )
    if not norm["subjects"]:
        raise ValueError("subjects must be non-empty")
    unknown_subjects = [s for s in norm["subjects"] if s not in JUDGEMENT_SUBJECTS]
    if unknown_subjects:
        raise ValueError(
            f"unknown subjects {unknown_subjects}; allowed: {list(JUDGEMENT_SUBJECTS)}"
        )
    if len(set(norm["subjects"])) != len(norm["subjects"]):
        raise ValueError(f"duplicate subjects: {norm['subjects']}")
    verdict_enum = norm.get("verdict_enum") or []
    for position, verdict in norm["wheel"].items():
        if position not in WHEEL_POSITIONS:
            raise ValueError(
                f"unknown wheel position {position!r}; allowed: {list(WHEEL_POSITIONS)}"
            )
        if verdict not in verdict_enum:
            raise ValueError(
                f"wheel verdict {verdict!r} (position {position!r}) not in "
                f"verdict_enum {verdict_enum}"
            )
    return norm


def register_template(
    *,
    name: str,
    version: int,
    output_definition: dict,
    langfuse_prompt_name: Optional[str] = None,
    is_draft: bool = False,
) -> int:
    """Insert a validated eval_template row; returns its id.

    Validates the output_definition (wheel verdicts/positions, kind,
    subjects) before writing — a template with an invalid wheel is refused.
    Stores the definition exactly as given (no normalization on disk).
    """
    validate_output_definition(output_definition)
    with _connect() as conn:
        tpl_id = conn.execute(
            "INSERT INTO eval_template(name, version, output_definition, "
            "                          langfuse_prompt_name, is_draft) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                name,
                version,
                json.dumps(output_definition),
                langfuse_prompt_name,
                1 if is_draft else 0,
            ),
        ).lastrowid
        conn.commit()
    return tpl_id


# ── Wheel seed templates (wave-12): pair-wheel-v1 + the two singles ─────
# Registered alongside card-prioritizer-v0, which stays untouched forever.
PAIR_WHEEL_TEMPLATE_NAME = "pair-wheel-v1"
PAIR_WHEEL_PROMPT_NAME = "judge-instructions:pair-wheel-v1"
PAIR_WHEEL_TEMPLATE_DEFINITION = {
    "judgement_kind": "pair",
    "subjects": ["execution"],
    "verdict_enum": [
        "tie-both-important",
        "b-slightly-better",
        "b-strongly-better",
        "b-lean-both-invalid",
        "neither-good",
        "a-lean-both-invalid",
        "a-strongly-better",
        "a-slightly-better",
        "incoherent",
        "skip",
    ],
    "confidence_enum": ["low", "mid", "high"],
    "wheel": {
        "n": "tie-both-important",
        "ne": "b-slightly-better",
        "e": "b-strongly-better",
        "se": "b-lean-both-invalid",
        "s": "neither-good",
        "sw": "a-lean-both-invalid",
        "w": "a-strongly-better",
        "nw": "a-slightly-better",
    },
    "rationale_required": False,
    "description": (
        "Semantic-wheel pair judgement: vertical axis is joint quality "
        "(up: both matter, down: both bad), horizontal is preference "
        "direction (left A, right B), diagonals mix them."
    ),
}

PAIR_WHEEL_JUDGE_INSTRUCTIONS = (
    "You compare two artifacts (A left, B right) on a semantic wheel. The "
    "verdict is relational: geometry signifies. Vertical axis = joint "
    "quality (up: both matter, down: both bad); horizontal axis = "
    "preference direction (left favors A, right favors B); diagonals mix "
    "them.\n\n"
    "Verdicts:\n"
    "  - tie-both-important — a tie worth flagging: both deserve to survive\n"
    "  - a-slightly-better / b-slightly-better — slight preference, both acceptable\n"
    "  - a-strongly-better / b-strongly-better — strong preference\n"
    "  - a-lean-both-invalid / b-lean-both-invalid — the pair is weak or "
    "invalid, but one side is closer to salvageable\n"
    "  - neither-good — reject both\n"
    "  - incoherent — one or both artifacts are malformed (off-wheel)\n"
    "  - skip — you genuinely cannot judge (off-wheel)\n\n"
    "Confidence: how sure you are. Default 'mid'."
)

# Same wheel geometry, IDEA subject: compares two PROPOSALS (is this worth
# pursuing?) rather than two executed artifacts. Needed so pipelines can
# declare idea-compare stages without misusing the execution rubric
# (stage subject must be among the rubric's subjects — fail-closed).
PAIR_IDEA_WHEEL_TEMPLATE_NAME = "pair-idea-wheel-v1"
PAIR_IDEA_WHEEL_PROMPT_NAME = "judge-instructions:pair-idea-wheel-v1"
PAIR_IDEA_WHEEL_TEMPLATE_DEFINITION = {
    **PAIR_WHEEL_TEMPLATE_DEFINITION,
    "subjects": ["idea"],
    "description": (
        "Semantic-wheel pair judgement over IDEAS (proposals/work orders): "
        "same geometry as pair-wheel-v1, but the question is which idea is "
        "more worth pursuing, not which execution is better."
    ),
}

PAIR_IDEA_WHEEL_JUDGE_INSTRUCTIONS = (
    "You compare two PROPOSALS (A left, B right) on a semantic wheel — "
    "judge the IDEA (worth pursuing?), not any execution. Geometry "
    "signifies: vertical = joint quality (up: both matter, down: both "
    "bad); horizontal = preference (left A, right B); diagonals mix "
    "them.\n\n"
    "Verdicts: as pair-wheel-v1 (tie-both-important, a/b-slightly-better, "
    "a/b-strongly-better, a/b-lean-both-invalid, neither-good; incoherent "
    "and skip off-wheel).\n\n"
    "Confidence: how sure you are. Default 'mid'."
)

SINGLE_IDEA_TEMPLATE_NAME = "single-idea-v1"
SINGLE_IDEA_PROMPT_NAME = "judge-instructions:single-idea-v1"
SINGLE_IDEA_TEMPLATE_DEFINITION = {
    "judgement_kind": "single",
    "subjects": ["idea"],
    "verdict_enum": [
        "important",
        "promising",
        "needs-evidence",
        "not-worth-pursuing",
        "invalid",
        "skip",
    ],
    "confidence_enum": ["low", "mid", "high"],
    "wheel": {
        "n": "important",
        "ne": "promising",
        "se": "not-worth-pursuing",
        "s": "invalid",
    },
    "rationale_required": False,
    "description": (
        "Absolute judgement of ONE artifact's IDEA: is this proposal worth "
        "pursuing? Vertical-axis wheel (needs-evidence and skip off-wheel)."
    ),
}

SINGLE_IDEA_JUDGE_INSTRUCTIONS = (
    "You judge ONE artifact on its IDEA: is the underlying proposal worth "
    "pursuing, independent of how well this exact artifact executes it?\n\n"
    "Verdicts:\n"
    "  - important — pursuing this clearly matters\n"
    "  - promising — worth pursuing, not obviously critical\n"
    "  - needs-evidence — cannot call it without more supporting evidence\n"
    "  - not-worth-pursuing — a real idea, but not worth the effort\n"
    "  - invalid — the premise is wrong or incoherent\n"
    "  - skip — you genuinely cannot judge (insufficient context)\n\n"
    "Confidence: how sure you are. Default 'mid'."
)

SINGLE_EXECUTION_TEMPLATE_NAME = "single-execution-v1"
SINGLE_EXECUTION_PROMPT_NAME = "judge-instructions:single-execution-v1"
SINGLE_EXECUTION_TEMPLATE_DEFINITION = {
    "judgement_kind": "single",
    "subjects": ["execution"],
    "verdict_enum": [
        "approve",
        "approve-with-notes",
        "revise",
        "reject-invalid",
        "skip",
    ],
    "confidence_enum": ["low", "mid", "high"],
    "wheel": {
        "n": "approve",
        "ne": "approve-with-notes",
        "se": "revise",
        "s": "reject-invalid",
    },
    "rationale_required": False,
    "description": (
        "Absolute judgement of ONE artifact's EXECUTION: is this exact "
        "artifact good? Vertical-axis wheel (skip off-wheel)."
    ),
}

SINGLE_EXECUTION_JUDGE_INSTRUCTIONS = (
    "You judge ONE artifact on its EXECUTION: is this exact artifact good, "
    "taking the idea behind it as given?\n\n"
    "Verdicts:\n"
    "  - approve — ship it as-is\n"
    "  - approve-with-notes — acceptable; note the caveats in your rationale\n"
    "  - revise — the direction is right but this execution needs rework\n"
    "  - reject-invalid — this execution is wrong or broken\n"
    "  - skip — you genuinely cannot judge (insufficient context)\n\n"
    "Confidence: how sure you are. Default 'mid'."
)

# (name, version, definition, prompt_name, instructions) — seeded by init_db
# the same way the v0 seed works: idempotent, version 1, prompt push is
# text-equality idempotent.
WHEEL_SEED_TEMPLATES = (
    (
        PAIR_WHEEL_TEMPLATE_NAME,
        1,
        PAIR_WHEEL_TEMPLATE_DEFINITION,
        PAIR_WHEEL_PROMPT_NAME,
        PAIR_WHEEL_JUDGE_INSTRUCTIONS,
    ),
    (
        PAIR_IDEA_WHEEL_TEMPLATE_NAME,
        1,
        PAIR_IDEA_WHEEL_TEMPLATE_DEFINITION,
        PAIR_IDEA_WHEEL_PROMPT_NAME,
        PAIR_IDEA_WHEEL_JUDGE_INSTRUCTIONS,
    ),
    (
        SINGLE_IDEA_TEMPLATE_NAME,
        1,
        SINGLE_IDEA_TEMPLATE_DEFINITION,
        SINGLE_IDEA_PROMPT_NAME,
        SINGLE_IDEA_JUDGE_INSTRUCTIONS,
    ),
    (
        SINGLE_EXECUTION_TEMPLATE_NAME,
        1,
        SINGLE_EXECUTION_TEMPLATE_DEFINITION,
        SINGLE_EXECUTION_PROMPT_NAME,
        SINGLE_EXECUTION_JUDGE_INSTRUCTIONS,
    ),
)

# Re-exported from bin.llm_config (single source of truth for the panel).
FRONTIER_OPENROUTER_MODELS = _llm_config.FRONTIER_OPENROUTER_MODELS


def _openrouter_config(model: str) -> dict:
    return _llm_config.judge_config(model).as_dict()


DEFAULT_LLM_CONFIG = _openrouter_config(FRONTIER_OPENROUTER_MODELS[0])


# ── Connection helper ───────────────────────────────────────────────────
def _connect(readonly: bool = False) -> sqlite3.Connection:
    DATA_HOME.mkdir(parents=True, exist_ok=True)
    if readonly:
        # SQLite URI mode for true read-only.
        uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # ADR 0001 §2 concurrency hygiene: wait instead of failing SQLITE_BUSY
    # when the other runtime holds a write transaction.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── Schema bootstrap ────────────────────────────────────────────────────
def init_db() -> None:
    """Create the fabric DB and seed the default rubric. Idempotent.

    Side effect: pushes the seed judge instructions to Langfuse Prompts
    (as `judge-instructions:production` v1) on first run. Re-runs are no-ops
    on both the SQLite seed and the Langfuse push (push is text-equality
    idempotent — see bin/prompts.push)."""
    DATA_HOME.mkdir(parents=True, exist_ok=True)
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with _connect() as conn:
        conn.executescript(schema_sql)
        _migrate_pending_judgement(conn)
        # Optimizer runs share the fabric DB; ensure its table too.
        from bin import optimizer_runs as _optimizer_runs
        _optimizer_runs.init()
        existing = conn.execute(
            "SELECT id FROM eval_template WHERE name=? AND version=?",
            (SEED_TEMPLATE_NAME, SEED_TEMPLATE_VERSION),
        ).fetchone()
        if existing is not None:
            _sync_default_llm_configs(conn, existing["id"])
            _seed_wheel_templates(conn)
            return
        from bin import prompts as _prompts
        _prompts.push(
            SEED_LANGFUSE_PROMPT_NAME,
            SEED_JUDGE_INSTRUCTIONS,
            labels=["production"],
        )
        _prompts.push(
            SEED_DOMAIN_BUILDER_PROMPT_NAME,
            SEED_DOMAIN_BUILDER_INSTRUCTIONS,
            labels=["production"],
        )
        tpl_id = conn.execute(
            "INSERT INTO eval_template(name, version, output_definition, "
            "                          langfuse_prompt_name) "
            "VALUES (?, ?, ?, ?)",
            (
                SEED_TEMPLATE_NAME,
                SEED_TEMPLATE_VERSION,
                json.dumps(SEED_TEMPLATE_DEFINITION),
                SEED_LANGFUSE_PROMPT_NAME,
            ),
        ).lastrowid
        for model in FRONTIER_OPENROUTER_MODELS:
            conn.execute(
                "INSERT INTO job_configuration(template_id, rater_type, rater_config) "
                "VALUES (?, 'llm', ?)",
                (tpl_id, json.dumps(_openrouter_config(model))),
            )
        conn.execute(
            "INSERT INTO job_configuration(template_id, rater_type, rater_config) "
            "VALUES (?, 'human', '{}')",
            (tpl_id,),
        )
        _seed_wheel_templates(conn)
        conn.commit()


def _seed_wheel_templates(conn: sqlite3.Connection) -> None:
    """Seed the wave-12 wheel templates (idempotent, version 1).

    Mirrors the v0 seed pattern: push the matching judge-instruction prompt
    (text-equality idempotent) and insert the eval_template row plus ONE
    active human job_configuration. No LLM panel configs here — domain
    wheel judgements are reviewed by humans (the L6 review bar), and the
    LLM drain path stays scoped to the v0 rubric's own configs.
    Commits its own work so both init_db paths (fresh + existing) persist.
    """
    from bin import prompts as _prompts

    for name, version, definition, prompt_name, instructions in WHEEL_SEED_TEMPLATES:
        existing = conn.execute(
            "SELECT id FROM eval_template WHERE name=? AND version=?",
            (name, version),
        ).fetchone()
        if existing is not None:
            continue
        validate_output_definition(definition)
        _prompts.push(prompt_name, instructions, labels=["production"])
        tpl_id = conn.execute(
            "INSERT INTO eval_template(name, version, output_definition, "
            "                          langfuse_prompt_name) "
            "VALUES (?, ?, ?, ?)",
            (name, version, json.dumps(definition), prompt_name),
        ).lastrowid
        conn.execute(
            "INSERT INTO job_configuration(template_id, rater_type, rater_config) "
            "VALUES (?, 'human', '{}')",
            (tpl_id,),
        )
    conn.commit()


def _sync_default_llm_configs(conn: sqlite3.Connection, template_id: int) -> None:
    """Keep the seed rubric on the configured frontier OpenRouter panel.

    Reuse one legacy active row for the primary model so already-enqueued
    judgements immediately pick up the new configuration. Add the other two
    models and archive any remaining legacy defaults.
    """
    rows = conn.execute(
        "SELECT id, rater_config FROM job_configuration "
        "WHERE template_id=? AND rater_type='llm' AND status='active' ORDER BY id",
        (template_id,),
    ).fetchall()
    by_model = {}
    legacy = []
    for row in rows:
        try:
            model = json.loads(row["rater_config"]).get("model")
        except (TypeError, json.JSONDecodeError):
            model = None
        if model in FRONTIER_OPENROUTER_MODELS and model not in by_model:
            by_model[model] = row["id"]
        else:
            legacy.append(row["id"])

    for model in FRONTIER_OPENROUTER_MODELS:
        config = json.dumps(_openrouter_config(model))
        if model in by_model:
            conn.execute(
                "UPDATE job_configuration SET rater_config=? WHERE id=?",
                (config, by_model[model]),
            )
        elif legacy:
            row_id = legacy.pop(0)
            conn.execute(
                "UPDATE job_configuration SET rater_config=? WHERE id=?",
                (config, row_id),
            )
        else:
            conn.execute(
                "INSERT INTO job_configuration(template_id, rater_type, rater_config) "
                "VALUES (?, 'llm', ?)",
                (template_id, config),
            )

    if legacy:
        placeholders = ",".join("?" for _ in legacy)
        conn.execute(
            f"UPDATE job_configuration SET status='archived' WHERE id IN ({placeholders})",
            legacy,
        )
    conn.commit()


def _migrate_pending_judgement(conn) -> None:
    """Idempotent ALTER TABLE for new columns added after v0."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pending_judgement)")}
    if "domain_id" not in cols:
        conn.execute(
            "ALTER TABLE pending_judgement ADD COLUMN domain_id INTEGER REFERENCES domain(id)"
        )
        conn.commit()


def get_template(name: str, version: Optional[int] = None) -> dict:
    """Fetch a template by name (latest non-draft version if version is None)."""
    with _connect(readonly=True) as conn:
        if version is None:
            row = conn.execute(
                "SELECT id, name, version, output_definition "
                "FROM eval_template WHERE name=? AND is_draft=0 "
                "ORDER BY version DESC LIMIT 1",
                (name,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, name, version, output_definition "
                "FROM eval_template WHERE name=? AND version=?",
                (name, version),
            ).fetchone()
        if row is None:
            raise LookupError(f"no template: {name} v{version}")
        return {
            "id": row["id"],
            "name": row["name"],
            "version": row["version"],
            "output_definition": normalize_output_definition(
                json.loads(row["output_definition"])
            ),
        }


def list_active_configs(template_name: str) -> list[dict]:
    """Active job configurations for a given rubric, by name."""
    tpl = get_template(template_name)
    with _connect(readonly=True) as conn:
        rows = conn.execute(
            "SELECT id, template_id, rater_type, rater_config, sampling, status "
            "FROM job_configuration WHERE template_id=? AND status='active'",
            (tpl["id"],),
        ).fetchall()
        return [
            {
                "id": r["id"],
                "template_id": r["template_id"],
                "rater_type": r["rater_type"],
                "rater_config": json.loads(r["rater_config"]),
                "sampling": r["sampling"],
                "status": r["status"],
            }
            for r in rows
        ]


# ── Enqueue / list pending ──────────────────────────────────────────────
def enqueue_for_match(
    *,
    tournament_db_path: str,
    match_id: int,
    template_name: str = SEED_TEMPLATE_NAME,
    trace_id: Optional[str] = None,
) -> list[int]:
    """For each active config on this rubric, insert a pending row.

    Returns the list of inserted pending_judgement IDs. Idempotent on
    (config_id, tournament_db_path, match_id) — won't duplicate.
    """
    payload = _trace_payload(tournament_db_path, match_id)
    if payload is None:
        return []
    configs = list_active_configs(template_name)
    inserted: list[int] = []
    with _connect() as conn:
        for cfg in configs:
            # Idempotency check — skip if a pending row already exists
            # for this (config, trace) pair.
            existing = conn.execute(
                "SELECT id FROM pending_judgement "
                "WHERE config_id=? AND tournament_db_path=? AND match_id=?",
                (cfg["id"], tournament_db_path, match_id),
            ).fetchone()
            if existing is not None:
                continue
            pid = conn.execute(
                "INSERT INTO pending_judgement(config_id, tournament_db_path, "
                "match_id, trace_id, trace_payload) VALUES (?, ?, ?, ?, ?)",
                (
                    cfg["id"],
                    tournament_db_path,
                    match_id,
                    trace_id,
                    json.dumps(payload),
                ),
            ).lastrowid
            inserted.append(pid)
        conn.commit()
    return inserted


def _trace_payload(tournament_db_path: str, match_id: int) -> Optional[dict]:
    """Read the match row from the tournament DB and shape it for raters."""
    if not Path(tournament_db_path).exists():
        return None
    conn = sqlite3.connect(f"file:{tournament_db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT id, round, slot, input_a, input_b, is_bye, conclusion, "
            "synthesis, winner_id, winner_reasoning, trace_id "
            "FROM matches WHERE id=?",
            (match_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        # Old DB shape (pre-langfuse rewrite): no synthesis/winner columns.
        row = conn.execute(
            "SELECT id, round, slot, input_a, input_b, is_bye, conclusion "
            "FROM matches WHERE id=?",
            (match_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    payload = {
        "match_id": row["id"],
        "label": f"R{row['round']}-{row['slot'] + 1}",
        "round": row["round"],
        "slot": row["slot"],
        "input_a": row["input_a"],
        "input_b": row["input_b"],
        "is_bye": bool(row["is_bye"]),
        "conclusion": row["conclusion"],
    }
    # Best-effort fields from the post-Langfuse schema.
    for field in ("synthesis", "winner_id", "winner_reasoning", "trace_id"):
        try:
            payload[field] = row[field]
        except (IndexError, KeyError):
            payload[field] = None
    return payload


def list_pending(rater_type: Optional[str] = None, limit: int = 50) -> list[dict]:
    """Return pending rows, optionally filtered to one rater type."""
    sql = (
        "SELECT p.id, p.config_id, p.tournament_db_path, p.match_id, "
        "       p.trace_id, p.trace_payload, p.created_at, "
        "       c.rater_type, c.rater_config, "
        "       t.name AS template_name, t.version AS template_version, "
        "       t.output_definition, d.name AS domain_name, "
        "       d.description AS domain_description, "
        "       d.judge_prompt AS judge_prompt_name "
        "FROM pending_judgement p "
        "JOIN job_configuration c ON c.id = p.config_id "
        "JOIN eval_template t ON t.id = c.template_id "
        "LEFT JOIN domain d ON d.id = p.domain_id "
        "WHERE p.status='pending' "
    )
    params: list[Any] = []
    if rater_type is not None:
        sql += "AND c.rater_type=? "
        params.append(rater_type)
    sql += "ORDER BY p.created_at ASC LIMIT ?"
    params.append(limit)
    with _connect(readonly=True) as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            {
                "id": r["id"],
                "config_id": r["config_id"],
                "tournament_db_path": r["tournament_db_path"],
                "match_id": r["match_id"],
                "trace_id": r["trace_id"],
                "trace_payload": json.loads(r["trace_payload"]),
                "rater_type": r["rater_type"],
                "rater_config": json.loads(r["rater_config"]),
                "template_name": r["template_name"],
                "template_version": r["template_version"],
                "output_definition": normalize_output_definition(
                    json.loads(r["output_definition"])
                ),
                "domain_name": r["domain_name"],
                "domain_description": r["domain_description"],
                "judge_prompt_name": r["judge_prompt_name"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]


# ── Write a judgement (2 Score rows per subject + flip pending → done) ──
_PENDING_ROW_SQL = (
    "SELECT p.id, p.config_id, p.tournament_db_path, p.match_id, "
    "       p.trace_id, c.template_id, t.version AS template_version, "
    "       t.output_definition "
    "FROM pending_judgement p "
    "JOIN job_configuration c ON c.id = p.config_id "
    "JOIN eval_template t ON t.id = c.template_id "
    "WHERE p.id=?"
)


def _validated_entries(
    prow,
    *,
    verdict: Optional[str],
    confidence: Optional[str],
    rationale: Optional[str],
    subject_verdicts: Optional[dict],
) -> list[tuple]:
    """THE single validation path for score writes (shared by
    write_judgement and revise_judgement). Normalizes the template's
    output_definition, resolves the call shape (legacy single-subject vs
    subject_verdicts), and validates every entry against the rubric.
    Returns [(subject-or-None, verdict, confidence, rationale), ...] in
    declared-subject order. Raises ValueError on any rubric violation.
    """
    outdef = normalize_output_definition(json.loads(prow["output_definition"]))
    subjects = outdef["subjects"]
    verdict_enum = outdef.get("verdict_enum") or []
    confidence_enum = outdef.get("confidence_enum") or ["low", "mid", "high"]
    rationale_required = outdef.get("rationale_required", False)

    if subject_verdicts is None:
        if len(subjects) > 1:
            raise ValueError(
                f"template declares subjects {subjects}; pass "
                "subject_verdicts covering all of them"
            )
        if verdict is None or confidence is None:
            raise ValueError("verdict and confidence are required")
        # Legacy shape: the sole subject, legacy metric names.
        entries = [(None, verdict, confidence, rationale)]
    else:
        if verdict is not None or confidence is not None:
            raise ValueError(
                "pass either verdict/confidence or subject_verdicts, not both"
            )
        missing = [s for s in subjects if s not in subject_verdicts]
        if missing:
            raise ValueError(
                f"subject_verdicts missing required subjects: {missing} "
                f"(template declares {subjects})"
            )
        extra = [s for s in subject_verdicts if s not in subjects]
        if extra:
            raise ValueError(
                f"subject_verdicts has undeclared subjects: {extra} "
                f"(template declares {subjects})"
            )
        entries = [
            (
                s,
                subject_verdicts[s].get("verdict"),
                subject_verdicts[s].get("confidence"),
                subject_verdicts[s].get("rationale"),
            )
            for s in subjects  # declared order, deterministic rows
        ]

    for subj, v, c, r in entries:
        label = f"subject {subj!r}: " if subj is not None else ""
        if v not in verdict_enum:
            raise ValueError(
                f"{label}verdict {v!r} not in rubric enum {verdict_enum}"
            )
        if c not in confidence_enum:
            raise ValueError(
                f"{label}confidence {c!r} not in {confidence_enum}"
            )
        if rationale_required and not (r or "").strip():
            raise ValueError(f"{label}rubric requires rationale; got empty")
    return entries


def _insert_score_rows(conn, prow, rating_id: str, entries, rater: dict) -> None:
    """THE single score-writing path: per-subject verdict + confidence rows
    under one rating_id. Shared by write_judgement and revise_judgement;
    identical row shape either way (old rows are never touched)."""
    common = (
        rating_id,
        prow["id"],
        prow["template_id"],
        prow["template_version"],
    )
    trace = (prow["tournament_db_path"], prow["match_id"], prow["trace_id"])
    for subj, v, c, r in entries:
        prefix = f"judgement.{subj}." if subj is not None else "judgement."
        verdict_meta = {"rater": rater}
        if r:
            verdict_meta["rationale"] = r
        confidence_meta = {"rater": rater}
        conn.execute(
            "INSERT INTO score(rating_id, pending_id, template_id, "
            "  rubric_version, name, data_type, value, metadata, "
            "  tournament_db_path, match_id, trace_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (*common, f"{prefix}verdict", "CATEGORICAL",
             v, json.dumps(verdict_meta), *trace),
        )
        conn.execute(
            "INSERT INTO score(rating_id, pending_id, template_id, "
            "  rubric_version, name, data_type, value, metadata, "
            "  tournament_db_path, match_id, trace_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (*common, f"{prefix}confidence", "CATEGORICAL",
             c, json.dumps(confidence_meta), *trace),
        )


def write_judgement(
    *,
    pending_id: int,
    verdict: Optional[str] = None,
    confidence: Optional[str] = None,
    rationale: Optional[str] = None,
    rater: dict,
    subject_verdicts: Optional[dict] = None,
) -> str:
    """Write Score rows for one judgement, mark the pending row done.

    Returns the rating_id UUID. Validates against the template's
    (normalized) output_definition. `rater` is `{type, ...identity}`.

    Two call shapes:
      - Legacy (single-subject templates only): pass verdict/confidence
        [/rationale]. Metric names stay 'judgement.verdict' /
        'judgement.confidence' — full backward compatibility.
      - Subject-aware: pass subject_verdicts, a dict like
        {'idea': {'verdict': ..., 'confidence': ..., 'rationale': ...},
         'execution': {...}}. Each subject writes its own pair of score
        rows named 'judgement.<subject>.verdict' /
        'judgement.<subject>.confidence' under the SAME rating_id.
        When the template declares multiple subjects this shape is
        REQUIRED and every declared subject must be present.

    The pending row still resolves exactly once regardless of shape.
    """
    rating_id = str(uuid.uuid4())
    with _connect() as conn:
        # Look up the pending row + template definition.
        prow = conn.execute(_PENDING_ROW_SQL, (pending_id,)).fetchone()
        if prow is None:
            raise LookupError(f"pending {pending_id} not found")
        if conn.execute(
            "SELECT status FROM pending_judgement WHERE id=?", (pending_id,)
        ).fetchone()["status"] != "pending":
            raise RuntimeError(f"pending {pending_id} already resolved")
        entries = _validated_entries(
            prow,
            verdict=verdict,
            confidence=confidence,
            rationale=rationale,
            subject_verdicts=subject_verdicts,
        )
        _insert_score_rows(conn, prow, rating_id, entries, rater)
        # Duplicate-rating guard: the pre-check above is advisory only — a
        # concurrent writer can resolve this row between our SELECT and this
        # point (check-then-act race). The status flip is therefore
        # conditional on status='pending'; if another writer won, rowcount
        # is 0 and we raise, rolling back this transaction's score INSERTs
        # so exactly one rating survives per pending row.
        cur = conn.execute(
            "UPDATE pending_judgement SET status='done', rating_id=?, "
            "completed_at=? WHERE id=? AND status='pending'",
            (rating_id, _now(), pending_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"pending {pending_id} already resolved")
        conn.commit()
    return rating_id


# ── Append-only revision (wave-13 slice A; operator-environment-v13 §1) ──
def _revision_chain_rows(conn, pending_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, pending_id, previous_rating_id, new_rating_id, "
        "       revised_by, reason, created_at "
        "FROM judgement_revision WHERE pending_id=? ORDER BY id ASC",
        (pending_id,),
    ).fetchall()


def _effective_rating_id(conn, pending_id: int) -> Optional[str]:
    """Tip of the revision chain: the original pending.rating_id when no
    revisions exist, else the new_rating_id of the LAST revision row."""
    prow = conn.execute(
        "SELECT rating_id FROM pending_judgement WHERE id=?", (pending_id,)
    ).fetchone()
    if prow is None:
        return None
    revisions = _revision_chain_rows(conn, pending_id)
    if revisions:
        return revisions[-1]["new_rating_id"]
    return prow["rating_id"]


def effective_rating_id(pending_id: int) -> Optional[str]:
    """The rating_id currently in effect for a pending: follow the revision
    chain from the original rating to the tip. None for unresolved rows."""
    with _connect(readonly=True) as conn:
        return _effective_rating_id(conn, pending_id)


def get_revision_chain(pending_id: int) -> list[dict]:
    """Full rating chain for a pending, original first, tip last.

    Returns [{rating_id, revised_by, reason, created_at}, ...]. The first
    element is the original rating (revised_by/reason None, created_at is
    the pending row's completed_at); each later element is one revision.
    Empty list when the pending row has no rating yet.
    """
    with _connect(readonly=True) as conn:
        prow = conn.execute(
            "SELECT rating_id, completed_at FROM pending_judgement WHERE id=?",
            (pending_id,),
        ).fetchone()
        if prow is None or prow["rating_id"] is None:
            return []
        chain = [{
            "rating_id": prow["rating_id"],
            "revised_by": None,
            "reason": None,
            "created_at": prow["completed_at"],
        }]
        for rev in _revision_chain_rows(conn, pending_id):
            chain.append({
                "rating_id": rev["new_rating_id"],
                "revised_by": rev["revised_by"],
                "reason": rev["reason"],
                "created_at": rev["created_at"],
            })
        return chain


def revise_judgement(
    pending_id: int,
    *,
    previous_rating_id: str,
    revised_by: str,
    reason: str,
    rater: dict,
    verdict: Optional[str] = None,
    confidence: Optional[str] = None,
    rationale: Optional[str] = None,
    subject_verdicts: Optional[dict] = None,
) -> str:
    """Append-only revision of an already-'done' judgement.

    Writes a BRAND-NEW rating (new rating_id + fresh score rows via the
    exact validation/score-writing path write_judgement uses) against the
    SAME pending row — which stays 'done', no status churn — plus one
    judgement_revision row linking previous_rating_id -> the new rating.
    Old score rows are NEVER touched; the effective verdict becomes the
    tip of the chain. Downstream outcomes already derived from the old
    verdict are NOT rewritten (the UI shows an honest note instead).

    Refuses (ValueError) when:
      - the pending row is not status='done' (only completed judgements
        can be revised — errors/cancellations are different flows);
      - previous_rating_id does not match the LATEST effective rating
        (stale revision: someone revised first — re-read and retry);
      - reason or revised_by is empty.

    Returns the new rating_id.
    """
    if not (revised_by or "").strip():
        raise ValueError("revised_by must be non-empty")
    if not (reason or "").strip():
        raise ValueError("revision reason must be non-empty")
    rating_id = str(uuid.uuid4())
    with _connect() as conn:
        prow = conn.execute(_PENDING_ROW_SQL, (pending_id,)).fetchone()
        if prow is None:
            raise LookupError(f"pending {pending_id} not found")
        status = conn.execute(
            "SELECT status FROM pending_judgement WHERE id=?", (pending_id,)
        ).fetchone()["status"]
        if status != "done":
            raise ValueError(
                f"pending {pending_id} is '{status}', not 'done' — only "
                "completed judgements can be revised"
            )
        effective = _effective_rating_id(conn, pending_id)
        if previous_rating_id != effective:
            raise ValueError(
                f"stale revision: previous_rating_id {previous_rating_id!r} "
                f"is not the effective rating {effective!r} for pending "
                f"{pending_id} — someone revised first; reload and retry"
            )
        entries = _validated_entries(
            prow,
            verdict=verdict,
            confidence=confidence,
            rationale=rationale,
            subject_verdicts=subject_verdicts,
        )
        _insert_score_rows(conn, prow, rating_id, entries, rater)
        # Concurrency guard mirroring write_judgement's conditional flip:
        # the INSERT below re-asserts the chain tip inside the transaction
        # via a WHERE-guarded SELECT — if a concurrent reviser won the
        # race, no row matches, rowcount is 0, and we roll back so the
        # score INSERTs above never survive without their revision row.
        cur = conn.execute(
            "INSERT INTO judgement_revision(pending_id, previous_rating_id, "
            "  new_rating_id, revised_by, reason) "
            "SELECT ?, ?, ?, ?, ? "
            "WHERE (SELECT COALESCE("
            "  (SELECT new_rating_id FROM judgement_revision "
            "     WHERE pending_id=? ORDER BY id DESC LIMIT 1), "
            "  (SELECT rating_id FROM pending_judgement WHERE id=?))) = ?",
            (pending_id, previous_rating_id, rating_id, revised_by.strip(),
             reason.strip(), pending_id, pending_id, previous_rating_id),
        )
        if cur.rowcount != 1:
            raise ValueError(
                f"stale revision: pending {pending_id} was revised "
                "concurrently — reload and retry"
            )
        conn.commit()
    return rating_id


# ── LLM judge worker ────────────────────────────────────────────────────
def _payload_as_card_pair(payload: dict) -> dict:
    """Normalize trace_payload to (card_a, card_b) tuples regardless of whether
    the row was written under the new card-shaped contract or the legacy
    match-shaped contract.
    """
    a = payload.get("card_a") or {}
    b = payload.get("card_b") or {}
    if not a or not b:
        # Legacy match shape — synthesize cards from input paths + synthesis.
        synth = payload.get("synthesis") or payload.get("conclusion") or ""
        a = a or {"title": "Input 1", "body": payload.get("input_a") or "(none)"}
        b = b or {"title": "Input 2", "body": payload.get("input_b") or "(none)"}
        if synth and not a.get("body_extra"):
            a["body_extra"] = synth[:4000]
    return {"a": a, "b": b}


def _build_dspy_lm(cfg: dict) -> "dspy.LM":
    """Build a DSPy LM client from a rater_config row."""
    import dspy as _dspy
    model = cfg.get("model") or DEFAULT_LLM_CONFIG["model"]
    base_url = (cfg.get("base_url") or DEFAULT_LLM_CONFIG["base_url"]).rstrip("/")
    api_key_env = cfg.get("api_key_env") or DEFAULT_LLM_CONFIG["api_key_env"]
    api_key = os.environ.get(api_key_env, "") or "none"
    temperature = float(cfg.get("temperature", DEFAULT_LLM_CONFIG["temperature"]))
    timeout = float(cfg.get("timeout_seconds", DEFAULT_LLM_CONFIG["timeout_seconds"]))
    num_retries = int(cfg.get("num_retries", DEFAULT_LLM_CONFIG["num_retries"]))
    return _dspy.LM(
        model=f"openai/{model}",
        api_base=f"{base_url}",
        api_key=api_key,
        temperature=temperature,
        timeout=timeout,
        num_retries=num_retries,
    )


def run_llm_judge_for_pending(pending_id: int) -> Optional[str]:
    """Drain one pending LLM-judge row via the DSPy MatchJudge module."""
    import dspy as _dspy
    from bin.judges.match_judge import MatchJudge

    pending = [p for p in list_pending(rater_type="llm", limit=1000)
               if p["id"] == pending_id]
    if not pending:
        return None
    p = pending[0]
    if p["rater_type"] != "llm":
        raise ValueError(f"pending {pending_id} is rater_type {p['rater_type']}, not llm")

    cfg = p["rater_config"]
    cards = _payload_as_card_pair(p["trace_payload"])

    # Use whatever LM the caller explicitly configured (DummyLM under tests).
    # Otherwise build this row's configured model. Keep it in a local DSPy
    # context: configuring it globally makes the first queue row's model leak
    # into every later row in the same drain process.
    settings_lm = getattr(_dspy.settings, "lm", None)
    row_lm = settings_lm or _build_dspy_lm(cfg)

    try:
        with _dspy.context(lm=row_lm):
            # Domain-generated pairs must use the domain's own judging brief.
            # Legacy/direct tournament rows have no domain and intentionally
            # fall back to the global production judge prompt.
            judge = MatchJudge(prompt_name=p["judge_prompt_name"] or SEED_LANGFUSE_PROMPT_NAME)
            result = judge(
                card_a_title=cards["a"].get("title", "(no title)"),
                card_a_body=cards["a"].get("body", "(empty)"),
                card_a_source_ref=cards["a"].get("source_ref", ""),
                card_b_title=cards["b"].get("title", "(no title)"),
                card_b_body=cards["b"].get("body", "(empty)"),
                card_b_source_ref=cards["b"].get("source_ref", ""),
            )
        rating_id = write_judgement(
            pending_id=pending_id,
            verdict=result.verdict,
            confidence=result.confidence,
            rationale=result.rationale,
            rater={
                "type": "llm",
                "model": cfg.get("model") or DEFAULT_LLM_CONFIG["model"],
                "base_url": cfg.get("base_url") or DEFAULT_LLM_CONFIG["base_url"],
            },
        )
        return rating_id
    except Exception as e:
        with _connect() as conn:
            # Valid-transition guard: only pending→error. If the row was
            # already resolved (e.g. a concurrent worker or the UI completed
            # it while our LLM call was in flight, making write_judgement
            # raise "already resolved"), stomping it to 'error' would leave
            # error status alongside a live rating_id + committed scores.
            conn.execute(
                "UPDATE pending_judgement SET status='error', "
                "error_message=?, completed_at=? WHERE id=? AND status='pending'",
                (f"{type(e).__name__}: {e}"[:500], _now(), pending_id),
            )
            conn.commit()
        raise


def drain_llm_queue(limit: int = 50) -> dict:
    """Process all pending LLM-judge rows up to `limit`."""
    pending = list_pending(rater_type="llm", limit=limit)
    results = {"ok": 0, "error": 0, "skipped": 0, "errors": []}
    for p in pending:
        try:
            rid = run_llm_judge_for_pending(p["id"])
            if rid:
                results["ok"] += 1
            else:
                results["skipped"] += 1
        except Exception as e:
            results["error"] += 1
            results["errors"].append({"pending_id": p["id"], "error": str(e)[:200]})
    return results


# ── Export ──────────────────────────────────────────────────────────────
def export_jsonl(rubric: str, rater_type: Optional[str] = None) -> list[dict]:
    """Join score rows back into per-judgement JSONL records.

    Two Score rows per judgement (verdict + confidence) collapse into one
    output line via rating_id. Includes the rubric instructions and the
    trace payload so the line is self-contained as training data.
    """
    sql_filter = "WHERE t.name=?"
    params: list[Any] = [rubric]
    if rater_type is not None:
        sql_filter += (
            " AND json_extract(s_v.metadata, '$.rater.type') = ?"
        )
        params.append(rater_type)
    sql = f"""
    SELECT s_v.rating_id, s_v.value AS verdict,
           s_v.metadata AS verdict_meta,
           s_c.value AS confidence, s_c.metadata AS confidence_meta,
           s_v.tournament_db_path, s_v.match_id, s_v.trace_id,
           t.name AS rubric, t.version AS rubric_version,
           t.output_definition,
           s_v.created_at
    FROM score s_v
    JOIN score s_c ON s_c.rating_id = s_v.rating_id
                 AND s_c.name = 'judgement.confidence'
    JOIN eval_template t ON t.id = s_v.template_id
    {sql_filter}
      AND s_v.name = 'judgement.verdict'
    ORDER BY s_v.created_at ASC
    """
    out: list[dict] = []
    with _connect(readonly=True) as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    for r in rows:
        verdict_meta = json.loads(r["verdict_meta"])
        outdef = json.loads(r["output_definition"])
        # Lazy-load trace payload from the tournament DB so we always
        # get the latest version of the synthesis if it ever updates.
        trace = _trace_payload(r["tournament_db_path"], r["match_id"]) or {}
        out.append({
            "ratingId": r["rating_id"],
            "rubric": r["rubric"],
            "rubricVersion": r["rubric_version"],
            "instructions": outdef.get("instructions", ""),
            "trace": {
                "tournamentDbPath": r["tournament_db_path"],
                "matchId": r["match_id"],
                "label": trace.get("label"),
                "input_a": trace.get("input_a"),
                "input_b": trace.get("input_b"),
                "synthesis": trace.get("synthesis") or trace.get("conclusion"),
                "winner_id": trace.get("winner_id"),
                "winner_reasoning": trace.get("winner_reasoning"),
                "langfuseTraceId": r["trace_id"],
            },
            "judgement": {
                "verdict": r["verdict"],
                "confidence": r["confidence"],
                "rationale": verdict_meta.get("rationale"),
            },
            "rater": verdict_meta.get("rater", {}),
            "createdAt": r["created_at"],
        })
    return out


# ── CLI ─────────────────────────────────────────────────────────────────
def _cmd_init(args):
    init_db()
    print(f"initialized {DB_PATH}")


def _cmd_list_pending(args):
    rows = list_pending(rater_type=args.rater_type, limit=args.limit)
    for r in rows:
        print(
            f"#{r['id']:5} [{r['rater_type']:5}] "
            f"{r['template_name']} v{r['template_version']} "
            f"trace={Path(r['tournament_db_path']).stem}#m{r['match_id']} "
            f"({r['created_at']})"
        )
    print(f"{len(rows)} pending")


def _cmd_run_llm(args):
    res = drain_llm_queue(limit=args.limit)
    print(json.dumps(res, indent=2))


def _cmd_enqueue(args):
    pids = enqueue_for_match(
        tournament_db_path=args.tournament_db,
        match_id=args.match_id,
        template_name=args.template,
    )
    print(json.dumps({"enqueued": pids}))


def _cmd_export(args):
    for rec in export_jsonl(rubric=args.rubric, rater_type=args.rater_type):
        print(json.dumps(rec))


def _cmd_revise(args):
    subject_verdicts = (
        json.loads(args.subject_verdicts) if args.subject_verdicts else None
    )
    new_rating_id = revise_judgement(
        args.pending_id,
        previous_rating_id=args.previous_rating_id,
        revised_by=args.revised_by,
        reason=args.reason,
        rater={"type": "human", "userId": args.revised_by},
        verdict=args.verdict,
        confidence=args.confidence,
        rationale=args.rationale,
        subject_verdicts=subject_verdicts,
    )
    print(json.dumps({
        "pending_id": args.pending_id,
        "previous_rating_id": args.previous_rating_id,
        "new_rating_id": new_rating_id,
    }))


def _cmd_seed_demo(args):
    """Walk every tournament DB in DATA_HOME and enqueue every concluded
    match. Useful for demoing the comparison view without hand-rolling."""
    init_db()
    home = Path(args.root or "/tmp")
    total = 0
    for db_path in home.glob("*.db"):
        if db_path.name == "judgements.db":
            continue
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            ids = conn.execute(
                "SELECT id FROM matches WHERE conclusion IS NOT NULL "
                "AND is_bye = 0"
            ).fetchall()
            conn.close()
        except Exception:
            continue
        for (mid,) in ids:
            n = enqueue_for_match(
                tournament_db_path=str(db_path), match_id=mid,
            )
            total += len(n)
    print(f"enqueued {total} pending row(s) across tournament DBs in {home}")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init"); sp.set_defaults(func=_cmd_init)

    sp = sub.add_parser("list-pending")
    sp.add_argument("--rater-type", choices=["llm", "human", "agent", "programmatic"])
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=_cmd_list_pending)

    sp = sub.add_parser("run-llm")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=_cmd_run_llm)

    sp = sub.add_parser("enqueue")
    sp.add_argument("--tournament-db", required=True)
    sp.add_argument("--match-id", type=int, required=True)
    sp.add_argument("--template", default=SEED_TEMPLATE_NAME)
    sp.set_defaults(func=_cmd_enqueue)

    sp = sub.add_parser("export")
    sp.add_argument("--rubric", default=SEED_TEMPLATE_NAME)
    sp.add_argument("--rater-type")
    sp.set_defaults(func=_cmd_export)

    sp = sub.add_parser(
        "revise",
        help="append-only revision of a done judgement (new rating + chain row)",
    )
    sp.add_argument("--pending-id", type=int, required=True)
    sp.add_argument("--previous-rating-id", required=True)
    sp.add_argument("--revised-by", required=True)
    sp.add_argument("--reason", required=True)
    sp.add_argument("--verdict")
    sp.add_argument("--confidence")
    sp.add_argument("--rationale")
    sp.add_argument("--subject-verdicts",
                    help="JSON {subject: {verdict, confidence[, rationale]}}")
    sp.set_defaults(func=_cmd_revise)

    sp = sub.add_parser("seed-demo",
                        help="enqueue all concluded matches from /tmp/*.db")
    sp.add_argument("--root", default="/tmp")
    sp.set_defaults(func=_cmd_seed_demo)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

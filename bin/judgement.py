#!/usr/bin/env python3
"""
judgement.py — judgement-fabric v0 Python side.

Responsibilities:
  1. Schema bootstrap (`init_db`) — creates the central fabric DB at
     $DATA_TOURNAMENTS_HOME/judgements.db and seeds the
     `code-style-tournament` rubric on first run.
  2. Pending-queue helpers (`enqueue_for_match`, `list_pending`).
     There is ONE pair rubric, DEFAULT_TEMPLATE_NAME (pair-wheel-v2), and
     it carries the eight-verdict vocabulary. The card-prioritizer and
     pair-wheel-v1 rubrics are gone; see VOCABULARY_RESET_NOTICE for what
     that does to a database holding their judgements.
     The no-rematch reuse gate is scoped by rater type: a machine verdict
     never satisfies a human queue, a human verdict satisfies a machine
     one.
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
from typing import Any, Optional, Sequence

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bin import llm_config as _llm_config  # noqa: E402  (needs _REPO_ROOT on sys.path)
from bin.env_loader import load_dotenv as _load_dotenv  # noqa: E402
from bin.swiss import DISCARD_VERDICTS  # noqa: E402  (the engine owns the vocabulary)
from bin.swiss import known_verdicts as _engine_scored_verdicts  # noqa: E402
from bin.swiss import pair_key  # noqa: E402  (one definition, shared with the engine)
from bin.swiss import (  # noqa: E402
    EVERY_ENQUEUABLE_RUBRIC_VERDICT_MUST_SCORE_OR_THE_ENGINE_SILENTLY_READS_IT_AS_SKIP,
    A_DISCARD_EJECTS_THE_NAMED_SIDE_ONLY_NEVER_THE_ITEM_BESIDE_IT,
)

_load_dotenv()

DATA_HOME = Path(os.environ.get("DATA_TOURNAMENTS_HOME", "/tmp/data-tournaments"))
DB_PATH = DATA_HOME / "judgements.db"
SCHEMA_PATH = Path(__file__).parent / "judgement_schema.sql"

DEFAULT_JUDGE_PROMPT_NAME = "judge-instructions"

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
    "time-sensitive + blocks-others). Reference the same verdict enum the "
    "judge uses (discard-a, discard-b, a-wins-big, a-wins, tie, b-wins, "
    "b-wins-big, skip), and say what makes a card in this domain worth "
    "discarding outright rather than merely losing.\n\n"
    "Both prompts should be stand-alone — they will be saved as Langfuse "
    "Prompts and used directly. Don't reference other prompts or external "
    "context the runtime won't have."
)
SEED_DOMAIN_BUILDER_PROMPT_NAME = "domain-builder"

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

PAIR_WHEEL_TEMPLATE_NAME = "pair-wheel-v2"
PAIR_WHEEL_TEMPLATE_VERSION = 1
PAIR_WHEEL_PROMPT_NAME = "judge-instructions:pair-wheel-v2"

A_VOCABULARY_CHANGE_RENAMES_THE_RUBRIC_IT_NEVER_BUMPS_THE_VERSION = (
    "pair-wheel-v2 is a NEW rubric at version 1, not pair-wheel-v1 at version "
    "2. The verdict vocabulary is entirely different, and a name that means "
    "two different things depending on the version is discoverable only by "
    "reading eval_template. The rubric name is hashed into pair_key, so the "
    "rename invalidates every stored key -- which is the point, and what "
    "VOCABULARY_RESET_NOTICE announces."
)
PAIR_WHEEL_TEMPLATE_DEFINITION = {
    "judgement_kind": "pair",
    "subjects": ["execution"],
    "verdict_enum": [
        "discard-a",
        "discard-b",
        "a-wins-big",
        "a-wins",
        "tie",
        "b-wins",
        "b-wins-big",
        "skip",
    ],
    "confidence_enum": ["low", "mid", "high"],
    "wheel": {
        "n": "tie",
        "ne": "b-wins",
        "e": "b-wins-big",
        "se": "discard-b",
        "sw": "discard-a",
        "w": "a-wins-big",
        "nw": "a-wins",
    },
    "rationale_required": False,
    "description": (
        "Semantic-wheel pair judgement: seven wheel verdicts plus an "
        "off-wheel skip. The horizontal axis names the side the verdict is "
        "about (left A, right B); the vertical axis says what happens to it "
        "(up: surface it ahead of the other, down: eject it from the pool). "
        "South is empty on purpose -- there is no 'both are bad' verdict. "
        "Skip is deliberately off the wheel: it establishes nothing and "
        "awards no rank."
    ),
}

SKIP_IS_ON_THE_RUBRIC_BUT_OFF_THE_WHEEL_BECAUSE_IT_ESTABLISHES_NOTHING = (
    "a rater who genuinely cannot call a pairing needs somewhere to say so, "
    "or the honest answer becomes a guessed 'tie' that scores points. skip is "
    "therefore judge-facing, but it sits with the operational verdicts rather "
    "than on the wheel, and bin/swiss.py routes it through no_result(): no "
    "result row, no played count, no rank."
)
assert "skip" in PAIR_WHEEL_TEMPLATE_DEFINITION["verdict_enum"], (
    SKIP_IS_ON_THE_RUBRIC_BUT_OFF_THE_WHEEL_BECAUSE_IT_ESTABLISHES_NOTHING
)
assert "skip" not in PAIR_WHEEL_TEMPLATE_DEFINITION["wheel"].values(), (
    SKIP_IS_ON_THE_RUBRIC_BUT_OFF_THE_WHEEL_BECAUSE_IT_ESTABLISHES_NOTHING
)

THERE_IS_DELIBERATELY_NO_BOTH_ARE_BAD_VERDICT_SO_SOUTH_STAYS_EMPTY = (
    "the south position used to hold neither-good, which ejected both cards "
    "at once. A judge facing two bad items now discards ONE, and the other "
    "returns to the pool to be judged on its own next time."
)
assert "s" not in PAIR_WHEEL_TEMPLATE_DEFINITION["wheel"], (
    THERE_IS_DELIBERATELY_NO_BOTH_ARE_BAD_VERDICT_SO_SOUTH_STAYS_EMPTY
)

_PAIR_WHEEL_UNSCORED_VERDICTS = sorted(
    set(PAIR_WHEEL_TEMPLATE_DEFINITION["verdict_enum"]) - _engine_scored_verdicts()
)
assert not _PAIR_WHEEL_UNSCORED_VERDICTS, (
    f"{PAIR_WHEEL_TEMPLATE_NAME} can emit {_PAIR_WHEEL_UNSCORED_VERDICTS}, "
    f"which bin/swiss.py does not score. "
    f"{EVERY_ENQUEUABLE_RUBRIC_VERDICT_MUST_SCORE_OR_THE_ENGINE_SILENTLY_READS_IT_AS_SKIP}"
)

_PAIR_WHEEL_MISSING_DISCARDS = sorted(
    DISCARD_VERDICTS - set(PAIR_WHEEL_TEMPLATE_DEFINITION["verdict_enum"])
)
assert not _PAIR_WHEEL_MISSING_DISCARDS, (
    "the rubric the judge is handed must offer every verdict the swiss "
    "engine treats as a discard, or discard stays unreachable on the page; "
    f"missing: {_PAIR_WHEEL_MISSING_DISCARDS}. "
    f"{A_DISCARD_EJECTS_THE_NAMED_SIDE_ONLY_NEVER_THE_ITEM_BESIDE_IT}"
)

PAIR_WHEEL_JUDGE_INSTRUCTIONS = (
    "You compare two artifacts (A left, B right) and say which is more "
    "worth surfacing to a human. The verdict is relational: you are never "
    "asked how good either one is on its own.\n\n"
    "Use these criteria, weighted in this order:\n"
    "  1. Specificity - concrete > vague. An item pointing at a real file "
    "and line beats a generic platitude.\n"
    "  2. Novelty - surprising > obvious.\n"
    "  3. Actionability - leads to a clear next step > leaves the reader "
    "shrugging.\n"
    "  4. Risk/impact - flags a real problem > nice-to-have.\n\n"
    "Comparison verdicts:\n"
    "  - a-wins-big - A is much more worth surfacing\n"
    "  - a-wins - A is more worth surfacing\n"
    "  - tie - the order between them does not matter for scheduling\n"
    "  - b-wins - B is more worth surfacing\n"
    "  - b-wins-big - B is much more worth surfacing\n\n"
    "Discard verdicts eject ONE side, permanently:\n"
    "  - discard-a - A should never have been generated; it leaves the pool\n"
    "  - discard-b - B should never have been generated; it leaves the pool\n\n"
    "A discard names ONE item and touches only that item. The other one "
    "stays in the pool with nothing recorded about it and is judged again "
    "next round on its own merits - so never discard a good item because "
    "the item beside it is malformed. If BOTH are bad, discard the worse "
    "one; the other comes back and you can discard it then. A discard is "
    "not a loss and not a score of zero: zero is a real position, held by "
    "items that lost honestly.\n\n"
    "One operational verdict, off the wheel:\n"
    "  - skip - you genuinely cannot judge this pairing\n\n"
    "A skip establishes NOTHING. It awards no rank, no points and no played "
    "match to either side; both come back in a later round. Use it only when "
    "the context is missing, never as a soft 'tie' - if you can read both "
    "items and the order between them does not matter, the honest answer is "
    "'tie'.\n\n"
    "Confidence: how sure you are. Default 'mid'."
)

PAIR_IDEA_WHEEL_TEMPLATE_NAME = "pair-idea-wheel-v2"
PAIR_IDEA_WHEEL_TEMPLATE_VERSION = 1
PAIR_IDEA_WHEEL_PROMPT_NAME = "judge-instructions:pair-idea-wheel-v2"
PAIR_IDEA_WHEEL_TEMPLATE_DEFINITION = {
    **PAIR_WHEEL_TEMPLATE_DEFINITION,
    "subjects": ["idea"],
    "description": (
        "Pair judgement over IDEAS (proposals/work orders): "
        "same geometry and same vocabulary as pair-wheel-v2, but the "
        "question is which idea is more worth pursuing, not which execution "
        "is better."
    ),
}

PAIR_IDEA_WHEEL_JUDGE_INSTRUCTIONS = (
    "You compare two PROPOSALS (A left, B right) - judge the IDEA (worth "
    "pursuing?), not any execution.\n\n"
    "Verdicts, exactly as pair-wheel-v2: a-wins-big, a-wins, tie, b-wins, "
    "b-wins-big for the comparison; discard-a and discard-b to eject ONE "
    "side permanently; skip when you genuinely cannot judge. A discard names "
    "one item and touches only that item - the other returns to the pool and "
    "is judged again on its own merits. A skip establishes nothing about "
    "either side and awards no rank.\n\n"
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

WHEEL_SEED_TEMPLATES = (
    (
        PAIR_WHEEL_TEMPLATE_NAME,
        PAIR_WHEEL_TEMPLATE_VERSION,
        PAIR_WHEEL_TEMPLATE_DEFINITION,
        PAIR_WHEEL_PROMPT_NAME,
        PAIR_WHEEL_JUDGE_INSTRUCTIONS,
    ),
    (
        PAIR_IDEA_WHEEL_TEMPLATE_NAME,
        PAIR_IDEA_WHEEL_TEMPLATE_VERSION,
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

DEFAULT_TEMPLATE_NAME = PAIR_WHEEL_TEMPLATE_NAME

VOCABULARY_RESET = "pair-wheel-v2-vocabulary-reset"

RETIRED_BY_THE_VOCABULARY_RESET = (
    ("card-prioritizer-v0", None),
    ("card-prioritizer-v1", None),
    ("pair-wheel-v1", None),
    ("pair-idea-wheel-v1", None),
)

VOCABULARY_RESET_NOTICE = (
    "{reset}: {ratings} judgement(s) in {db} were made under a rubric this "
    "reset retired ({rubrics}). card-prioritizer-v0, card-prioritizer-v1, "
    "pair-wheel-v1 and pair-idea-wheel-v1 are DELETED; the pair rubrics are "
    "now {pair_rubric} and {idea_rubric}, both at version {version}, and they "
    "carry a different verdict vocabulary. Every pair_key stored before the "
    "reset -- which hashes the rubric NAME and version -- no longer joins. "
    "Those pairs will be asked again from scratch, and NOTHING is "
    "backfilled. This is a deliberate abandonment of the old corpus, not "
    "data loss: the old rows are still on disk and still mean exactly what "
    "they meant under the vocabulary they were judged with."
)

THE_RESET_NOTICE_NAMES_THE_RUBRIC_THE_OPERATOR_WILL_ACTUALLY_FIND_ON_DISK = (
    "an abandonment notice that names a rubric nobody seeded sends the "
    "operator looking for rows that do not exist, so the notice interpolates "
    "the registered constants instead of spelling the new names out."
)
assert "{pair_rubric}" in VOCABULARY_RESET_NOTICE, (
    THE_RESET_NOTICE_NAMES_THE_RUBRIC_THE_OPERATOR_WILL_ACTUALLY_FIND_ON_DISK
)

FRONTIER_OPENROUTER_MODELS = _llm_config.FRONTIER_OPENROUTER_MODELS

DEFAULT_JUDGE_PANEL_MODELS = FRONTIER_OPENROUTER_MODELS[:1]

ONE_MACHINE_OPINION_IS_ENOUGH_TO_DISAGREE_WITH_A_HUMAN = (
    "the machine panel exists to produce human-versus-machine disagreement, "
    "and one machine opinion produces it. Three fanned every enqueue out to "
    "1 human + 3 machine rows -- roughly 288 machine judgements for a "
    "33-item campaign -- buying a second and third opinion nothing reads. "
    "Widen it by taking a longer slice of FRONTIER_OPENROUTER_MODELS the day "
    "something consumes the spread; the sync below archives whatever falls "
    "outside the slice, so narrowing and widening are the same one-line edit."
)

def _openrouter_config(model: str) -> dict:
    return _llm_config.judge_config(model).as_dict()

DEFAULT_LLM_CONFIG = _openrouter_config(FRONTIER_OPENROUTER_MODELS[0])

def _connect(readonly: bool = False) -> sqlite3.Connection:
    DATA_HOME.mkdir(parents=True, exist_ok=True)
    if readonly:
        uri = f"file:{DB_PATH}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

def init_db() -> None:
    """Create the fabric DB and seed the ONE pair rubric. Idempotent.

    Side effects on first run: pushes the judge instructions to Langfuse
    Prompts (text-equality idempotent -- see bin/prompts.push), and, on a
    database that already holds judgements made under a retired rubric,
    prints VOCABULARY_RESET_NOTICE to stderr. That notice is the loud
    part of the abandonment: nothing is migrated and nothing is backfilled.
    """
    DATA_HOME.mkdir(parents=True, exist_ok=True)
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with _connect() as conn:
        conn.executescript(schema_sql)
        _migrate_pending_judgement(conn)
        from bin import optimizer_runs as _optimizer_runs
        _optimizer_runs.init()
        announce_vocabulary_reset(conn)
        created = _seed_wheel_templates(conn)
        if PAIR_WHEEL_TEMPLATE_NAME in created:
            from bin import prompts as _prompts
            _prompts.push(
                DEFAULT_JUDGE_PROMPT_NAME,
                PAIR_WHEEL_JUDGE_INSTRUCTIONS,
                labels=["production"],
            )
            _prompts.push(
                SEED_DOMAIN_BUILDER_PROMPT_NAME,
                SEED_DOMAIN_BUILDER_INSTRUCTIONS,
                labels=["production"],
            )
        _sync_default_llm_configs(
            conn, _template_id(conn, PAIR_WHEEL_TEMPLATE_NAME,
                               PAIR_WHEEL_TEMPLATE_VERSION)
        )

def _existing_template(conn: sqlite3.Connection, name: str, version: int):
    return conn.execute(
        "SELECT id FROM eval_template WHERE name=? AND version=?",
        (name, version),
    ).fetchone()

def _template_id(conn: sqlite3.Connection, name: str, version: int) -> int:
    row = conn.execute(
        "SELECT id FROM eval_template WHERE name=? AND version=?",
        (name, version),
    ).fetchone()
    if row is None:
        raise LookupError(f"no template: {name} v{version}")
    return row["id"]

def retired_corpus(conn: sqlite3.Connection) -> dict[str, int]:
    """Ratings this database holds under a rubric the reset retired.

    Keyed by "name vN". Empty on a database with nothing to abandon, which
    is what keeps the notice from crying wolf on a fresh install.
    """
    found: dict[str, int] = {}
    rows = conn.execute(
        "SELECT t.name AS name, t.version AS version, "
        "       COUNT(DISTINCT s.rating_id) AS ratings "
        "FROM score s JOIN eval_template t ON t.id = s.template_id "
        "GROUP BY t.name, t.version"
    ).fetchall()
    for row in rows:
        for name, version in RETIRED_BY_THE_VOCABULARY_RESET:
            if row["name"] == name and version in (None, row["version"]):
                found[f"{row['name']} v{row['version']}"] = row["ratings"]
    return found

def announce_vocabulary_reset(conn: sqlite3.Connection) -> Optional[str]:
    """Tell the operator, once per init, that the old corpus no longer joins.

    Returns the notice (also printed to stderr), or None when this database
    holds no pre-reset judgement.
    """
    found = retired_corpus(conn)
    if not found:
        return None
    notice = VOCABULARY_RESET_NOTICE.format(
        reset=VOCABULARY_RESET,
        ratings=sum(found.values()),
        db=DB_PATH,
        rubrics=", ".join(sorted(found)),
        pair_rubric=PAIR_WHEEL_TEMPLATE_NAME,
        idea_rubric=PAIR_IDEA_WHEEL_TEMPLATE_NAME,
        version=PAIR_WHEEL_TEMPLATE_VERSION,
    )
    print(notice, file=sys.stderr)
    return notice

def _seed_wheel_templates(conn: sqlite3.Connection) -> set[str]:
    """Seed every registered rubric. Idempotent; returns the names created.

    The existence check and the INSERT run in ONE ``BEGIN IMMEDIATE``
    transaction: eval_template carries UNIQUE(name, version), so an unlocked
    check-then-insert lets a concurrent init_db abort on the constraint
    instead of finding the row. The Langfuse push happens AFTER the commit,
    because a network call has no business holding a write lock.
    """
    from bin import prompts as _prompts

    created: set[str] = set()
    pushes: list[tuple[str, str]] = []
    for name, version, definition, prompt_name, instructions in WHEEL_SEED_TEMPLATES:
        validate_output_definition(definition)
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = _existing_template(conn, name, version)
            if existing is None:
                tpl_id = conn.execute(
                    "INSERT INTO eval_template(name, version, output_definition, "
                    "                          langfuse_prompt_name) "
                    "VALUES (?, ?, ?, ?)",
                    (name, version, json.dumps(definition), prompt_name),
                ).lastrowid
                conn.execute(
                    "INSERT INTO job_configuration(template_id, rater_type, "
                    "rater_config) VALUES (?, 'human', '{}')",
                    (tpl_id,),
                )
                created.add(name)
                pushes.append((prompt_name, instructions))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    for prompt_name, instructions in pushes:
        _prompts.push(prompt_name, instructions, labels=["production"])
    return created

def _sync_default_llm_configs(conn: sqlite3.Connection, template_id: int) -> None:
    """Keep the one pair rubric on DEFAULT_JUDGE_PANEL_MODELS.

    Reuse a legacy active row for each panel model so already-enqueued
    judgements immediately pick up the new configuration, then archive every
    active llm row the panel no longer names -- see
    ONE_MACHINE_OPINION_IS_ENOUGH_TO_DISAGREE_WITH_A_HUMAN for why the slice
    is one model wide.
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
        if model in DEFAULT_JUDGE_PANEL_MODELS and model not in by_model:
            by_model[model] = row["id"]
        else:
            legacy.append(row["id"])

    for model in DEFAULT_JUDGE_PANEL_MODELS:
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

_POST_V0_COLUMNS = (
    ("pending_judgement", "domain_id", "INTEGER REFERENCES domain(id)"),
    ("pending_judgement", "pair_key", "TEXT"),
    ("pending_judgement", "content_a", "TEXT"),
    ("pending_judgement", "content_b", "TEXT"),
    ("score", "pair_key", "TEXT"),
)

_POST_V0_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_pending_pair_key "
    "ON pending_judgement(pair_key)",
    "CREATE INDEX IF NOT EXISTS idx_score_pair_key ON score(pair_key)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_pending_one_open_row_per_config_pair "
    "ON pending_judgement(config_id, pair_key) "
    "WHERE status='pending' AND pair_key IS NOT NULL",
)

def _migrate_pending_judgement(conn) -> None:
    """Idempotent ALTER TABLE + backfill for columns added after v0.

    Runs after the schema script, so the tables always exist. Adding a
    column twice is an error in SQLite, hence the PRAGMA check; the index
    and backfill passes are no-ops once they have run.
    """
    for table, column, decl in _POST_V0_COLUMNS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    for ddl in _POST_V0_INDEXES:
        conn.execute(ddl)
    conn.commit()
    backfill_pair_keys(conn)

def backfill_pair_keys(conn=None) -> int:
    """Give every pair-shaped judgement row its content snapshot and pair key.

    Idempotent: only rows with a NULL pair_key are considered, and rows that
    are not a pair (single-subject judgements, byes) are left NULL forever.
    Returns the number of pending rows keyed.
    """
    if conn is None:
        with _connect() as own:
            return backfill_pair_keys(own)
    rows = conn.execute(
        "SELECT p.id, p.trace_payload, p.content_a, p.content_b, "
        "       t.name AS rubric, t.version AS rubric_version "
        "FROM pending_judgement p "
        "JOIN job_configuration c ON c.id = p.config_id "
        "JOIN eval_template t ON t.id = c.template_id "
        "WHERE p.pair_key IS NULL"
    ).fetchall()
    keyed = 0
    for row in rows:
        try:
            payload = json.loads(row["trace_payload"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        content_a = row["content_a"]
        content_b = row["content_b"]
        if content_a is None or content_b is None:
            content_a, content_b = snapshot_pair(payload)
        if content_a is None or content_b is None:
            continue
        key = pair_key(content_a, content_b, row["rubric"], row["rubric_version"])
        conn.execute(
            "UPDATE pending_judgement SET pair_key=?, content_a=?, content_b=? "
            "WHERE id=?",
            (key, content_a, content_b, row["id"]),
        )
        keyed += 1
    conn.execute(
        "UPDATE score SET pair_key = ("
        "  SELECT p.pair_key FROM pending_judgement p WHERE p.id = score.pending_id"
        ") WHERE pair_key IS NULL AND pending_id IS NOT NULL"
    )
    conn.commit()
    return keyed

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

SNAPSHOT_MAX_CHARS = 200_000

def _read_source_text(ref: str) -> Optional[str]:
    try:
        path = Path(ref)
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8", errors="replace")[:SNAPSHOT_MAX_CHARS]
    except (OSError, ValueError):
        return None

def _side_snapshot(card: Any, ref: Any) -> Optional[str]:
    """The judged text of one side, or None when there is no text to judge.

    A path is content only once it has been read. An unreadable ref yields
    None rather than the ref string, because a pair keyed by a filename would
    collide with every other unreadable ref of that name and would change
    identity the moment the file moved.
    """
    if isinstance(card, dict):
        text = card.get("body") or card.get("text") or card.get("title")
        if text:
            return str(text)[:SNAPSHOT_MAX_CHARS]
    if isinstance(ref, str) and ref.strip():
        return _read_source_text(ref)
    return None

def snapshot_pair(payload: dict) -> tuple[Optional[str], Optional[str]]:
    """Resolve a trace payload into the two texts judged.

    Card-shaped rows already carry the content; match-shaped rows carry only
    a file ref, which is read ONCE, here, at enqueue time. Returns (None, None)
    for payloads that are not a pair (single-subject rows, byes).
    """
    if not isinstance(payload, dict):
        return None, None
    a = _side_snapshot(payload.get("card_a"), payload.get("input_a"))
    b = _side_snapshot(payload.get("card_b"), payload.get("input_b"))
    if a is None or b is None:
        return None, None
    return a, b

REUSE_SATISFIED_BY: dict[str, tuple[str, ...]] = {
    "human": ("human",),
    "llm": ("human", "llm"),
}

def satisfying_rater_types(rater_type: str) -> tuple[str, ...]:
    """Which raters' verdicts may stand in for ``rater_type``'s own queue.

    docs/design/priority-tournament.md: the judgements a person makes are
    the product, so a machine verdict must never foreclose the human
    comparison the tournament exists to collect. A human verdict may stand
    in for a machine one — the asymmetry is the point. Any other rater type
    is satisfied only by itself, so an unrecognised rater is never silenced
    by a machine.
    """
    return REUSE_SATISFIED_BY.get(rater_type, (rater_type,))

def _rating_rater_types(conn, rating_id: str) -> set[str]:
    """The rater types recorded on one rating's score rows."""
    types: set[str] = set()
    for row in conn.execute(
        "SELECT metadata FROM score WHERE rating_id=?", (rating_id,)
    ):
        try:
            meta = json.loads(row["metadata"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        rater_type = (meta.get("rater") or {}).get("type")
        if rater_type:
            types.add(rater_type)
    return types

def _find_judgement_by_pair(conn, key: str,
                            rater_types: Optional[Sequence[str]] = None
                            ) -> Optional[str]:
    """The rating in effect for this pair, read from RESOLVED pending rows.

    Resolved pending rows are the only source: score.pair_key is derived
    from the pending row it belongs to (see backfill_pair_keys), so a
    keyed score row can never name a pair its pending row does not, and
    answering from score would bypass the revision chain and hand back a
    superseded rating.
    """
    wanted = set(rater_types) if rater_types is not None else None
    for row in conn.execute(
        "SELECT id, rating_id FROM pending_judgement "
        "WHERE pair_key=? AND status='done' AND rating_id IS NOT NULL "
        "ORDER BY completed_at ASC, id ASC",
        (key,),
    ).fetchall():
        rating_id = _effective_rating_id(conn, row["id"]) or row["rating_id"]
        if wanted is None or _rating_rater_types(conn, rating_id) & wanted:
            return rating_id
    return None

def find_judgement_by_pair(key: str, *,
                           for_rater_type: Optional[str] = None
                           ) -> Optional[str]:
    """The rating_id already recorded for this content pair, or None.

    Follows the revision chain, so the answer is the rating currently in
    effect rather than a superseded one. Derive the key with
    ``pair_key(content_a, content_b, rubric_id, rubric_version)``.

    ``for_rater_type`` names the queue asking. Omit it to ask "has anybody
    judged this pair"; pass 'human' to ask "has a PERSON judged this pair",
    which is the question the no-rematch rule must answer before it declines
    to ask a person again.
    """
    rater_types = (
        None if for_rater_type is None else satisfying_rater_types(for_rater_type)
    )
    with _connect(readonly=True) as conn:
        return _find_judgement_by_pair(conn, key, rater_types)

def pair_key_of_pending(pending_id: int) -> Optional[str]:
    """The stored pair key of one pending row (None for non-pair rows)."""
    with _connect(readonly=True) as conn:
        row = conn.execute(
            "SELECT pair_key FROM pending_judgement WHERE id=?", (pending_id,)
        ).fetchone()
        return row["pair_key"] if row is not None else None

class EnqueueOutcome(list):
    """The inserted pending ids, plus the pair identity they resolved to.

    A list of pending_judgement ids for every existing caller; `pair_key` and
    `existing_rating_id` say why the list may be empty — an already-judged
    pair is never re-asked of the rater it was already asked of, and its
    prior rating is the answer instead. The reuse gate is per rater type, so
    a non-empty list and a non-null `existing_rating_id` can coexist: a
    machine already answered, a person has still never been asked.
    """

    def __init__(self, pending_ids=(), *, pair_key: Optional[str] = None,
                 existing_rating_id: Optional[str] = None):
        super().__init__(pending_ids)
        self.pair_key = pair_key
        self.existing_rating_id = existing_rating_id

def enqueue_for_match(
    *,
    tournament_db_path: str,
    match_id: int,
    template_name: str = DEFAULT_TEMPLATE_NAME,
    trace_id: Optional[str] = None,
    payload: Optional[dict] = None,
    domain_id: Optional[int] = None,
) -> EnqueueOutcome:
    """For each active config on this rubric, insert a pending row.

    Returns an EnqueueOutcome (a list of inserted pending_judgement ids).
    Three things stop a row from being written:
      - the pair already carries a completed judgement THAT RATER TYPE
        accepts under this rubric version — nothing already judged is
        re-asked of the same kind of rater, and the outcome's
        existing_rating_id is that prior rating. The gate is scoped by
        :func:`satisfying_rater_types`: a machine verdict never stands in
        for a person's, so the LLM drain cannot silently foreclose the human
        comparison the tournament exists to collect;
      - the pair is already queued, unresolved, for the same config;
      - a row already exists for (config_id, tournament_db_path, match_id),
        the pre-pair-key idempotency rule, which still governs rows whose
        content could not be snapshotted.

    All three are SELECTs the INSERT depends on, so they run inside ONE
    ``BEGIN IMMEDIATE`` transaction (the shape bin/campaigns.py uses in five
    places) and the pair stop is backed in SQL by
    ``idx_pending_one_open_row_per_config_pair``. Read unlocked they are
    advisory: two concurrent enqueues of the same pair both see "not
    queued" and both insert, and the same person is asked twice.

    `payload` overrides the tournament-DB read, for callers that hold the
    content already; the row is snapshotted from it either way.
    """
    if payload is None:
        payload = _trace_payload(tournament_db_path, match_id)
    if payload is None:
        return EnqueueOutcome()
    template = get_template(template_name)
    content_a, content_b = snapshot_pair(payload)
    key = (
        pair_key(content_a, content_b, template["name"], template["version"])
        if content_a is not None and content_b is not None
        else None
    )
    configs = list_active_configs(template_name)
    inserted: list[int] = []
    prior_by_rater: dict[str, Optional[str]] = {}
    reused: Optional[str] = None
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for cfg in configs:
                rater_type = cfg["rater_type"]
                if key is not None and rater_type not in prior_by_rater:
                    prior_by_rater[rater_type] = _find_judgement_by_pair(
                        conn, key, satisfying_rater_types(rater_type)
                    )
                prior = prior_by_rater.get(rater_type)
                if prior is not None:
                    reused = reused or prior
                    continue
                existing = conn.execute(
                    "SELECT id FROM pending_judgement "
                    "WHERE config_id=? AND tournament_db_path=? AND match_id=?",
                    (cfg["id"], tournament_db_path, match_id),
                ).fetchone()
                if existing is not None:
                    continue
                if key is not None and conn.execute(
                    "SELECT id FROM pending_judgement "
                    "WHERE config_id=? AND pair_key=? AND status='pending'",
                    (cfg["id"], key),
                ).fetchone() is not None:
                    continue
                pid = conn.execute(
                    "INSERT INTO pending_judgement(config_id, tournament_db_path, "
                    "match_id, trace_id, trace_payload, pair_key, content_a, "
                    "content_b, domain_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        cfg["id"],
                        tournament_db_path,
                        match_id,
                        trace_id,
                        json.dumps(payload),
                        key,
                        content_a,
                        content_b,
                        domain_id,
                    ),
                ).lastrowid
                inserted.append(pid)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return EnqueueOutcome(inserted, pair_key=key, existing_rating_id=reused)

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
        "       p.trace_id, p.trace_payload, p.pair_key, p.created_at, "
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
                "pair_key": r["pair_key"],
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

_PENDING_ROW_SQL = (
    "SELECT p.id, p.config_id, p.tournament_db_path, p.match_id, "
    "       p.trace_id, p.pair_key, c.template_id, "
    "       t.version AS template_version, t.output_definition "
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
            for s in subjects
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
    trace = (prow["tournament_db_path"], prow["match_id"], prow["trace_id"],
             prow["pair_key"])
    for subj, v, c, r in entries:
        prefix = f"judgement.{subj}." if subj is not None else "judgement."
        verdict_meta = {"rater": rater}
        if r:
            verdict_meta["rationale"] = r
        confidence_meta = {"rater": rater}
        conn.execute(
            "INSERT INTO score(rating_id, pending_id, template_id, "
            "  rubric_version, name, data_type, value, metadata, "
            "  tournament_db_path, match_id, trace_id, pair_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (*common, f"{prefix}verdict", "CATEGORICAL",
             v, json.dumps(verdict_meta), *trace),
        )
        conn.execute(
            "INSERT INTO score(rating_id, pending_id, template_id, "
            "  rubric_version, name, data_type, value, metadata, "
            "  tournament_db_path, match_id, trace_id, pair_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
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
        cur = conn.execute(
            "UPDATE pending_judgement SET status='done', rating_id=?, "
            "completed_at=? WHERE id=? AND status='pending'",
            (rating_id, _now(), pending_id),
        )
        if cur.rowcount != 1:
            raise RuntimeError(f"pending {pending_id} already resolved")
        conn.commit()
    return rating_id

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

def _payload_as_card_pair(payload: dict) -> dict:
    """Normalize trace_payload to (card_a, card_b) tuples regardless of whether
    the row was written under the new card-shaped contract or the legacy
    match-shaped contract.
    """
    a = payload.get("card_a") or {}
    b = payload.get("card_b") or {}
    if not a or not b:
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

    settings_lm = getattr(_dspy.settings, "lm", None)
    row_lm = settings_lm or _build_dspy_lm(cfg)

    try:
        with _dspy.context(lm=row_lm):
            judge = MatchJudge(prompt_name=p["judge_prompt_name"] or DEFAULT_JUDGE_PROMPT_NAME)
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

def _export_trace(row) -> tuple[dict, Any, Any]:
    """The display payload and the two judged contents for one export line.

    The snapshot taken at enqueue time is authoritative: the export must
    reproduce what the rater actually judged, and a work-order pair's
    `tournament_db_path` is a `domain:<id>` handle that no re-read can
    resolve. Falls back to the stored payload, then to the tournament DB,
    for rows written before snapshots existed.
    """
    try:
        payload = json.loads(row["trace_payload"] or "{}")
    except (TypeError, json.JSONDecodeError, IndexError, KeyError):
        payload = {}
    if not payload:
        payload = _trace_payload(row["tournament_db_path"], row["match_id"]) or {}
    side_a = row["content_a"]
    side_b = row["content_b"]
    if side_a is None:
        side_a = _side_snapshot(payload.get("card_a"), payload.get("input_a"))
    if side_b is None:
        side_b = _side_snapshot(payload.get("card_b"), payload.get("input_b"))
    return payload, side_a, side_b

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
           COALESCE(s_v.pair_key, p.pair_key) AS pair_key,
           p.trace_payload, p.content_a, p.content_b,
           t.name AS rubric, t.version AS rubric_version,
           t.output_definition,
           s_v.created_at
    FROM score s_v
    JOIN score s_c ON s_c.rating_id = s_v.rating_id
                 AND s_c.name = 'judgement.confidence'
    JOIN eval_template t ON t.id = s_v.template_id
    LEFT JOIN pending_judgement p ON p.id = s_v.pending_id
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
        trace, side_a, side_b = _export_trace(r)
        out.append({
            "ratingId": r["rating_id"],
            "rubric": r["rubric"],
            "rubricVersion": r["rubric_version"],
            "pairKey": r["pair_key"],
            "instructions": outdef.get("instructions", ""),
            "trace": {
                "tournamentDbPath": r["tournament_db_path"],
                "matchId": r["match_id"],
                "label": trace.get("label"),
                "input_a": side_a,
                "input_b": side_b,
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
    sp.add_argument("--template", default=DEFAULT_TEMPLATE_NAME)
    sp.set_defaults(func=_cmd_enqueue)

    sp = sub.add_parser("export")
    sp.add_argument("--rubric", default=DEFAULT_TEMPLATE_NAME)
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

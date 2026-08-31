"""Conservative GEPA + context-playbook optimization for the judge.

The optimizer uses only human labels, creates deterministic train/validation/
holdout partitions, lets GEPA discover lessons from rich trace feedback, then
uses a separate curator model to merge those lessons into an incremental
playbook. The production seed is retained unless the curated candidate beats it
on the untouched holdout set.

Promotion is four conjuncts — effective playbook change, the improvement
margin, exact accuracy and invalid rate — and the run row records which one
decided the outcome. The margin itself carries provenance (caller,
OPTIMIZER_MIN_IMPROVEMENT, or DEFAULT_MIN_IMPROVEMENT) and the holdout size
it needs to mean anything: on N holdout examples no improvement can land
between 0 and 1/N, so a margin finer than that is a threshold nothing can
fail. A run whose improvement clears the configured margin but not one
holdout example is reported as insufficient-evidence, never accepted.

Output is line-oriented so Phoenix can tail it directly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import time
import uuid
from contextlib import redirect_stdout
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

os.environ.setdefault(
    "DSPY_CACHEDIR",
    str(Path(os.environ.get("DATA_TOURNAMENTS_HOME", "/tmp/data-tournaments")) / ".dspy-cache"),
)

from concurrent.futures import ThreadPoolExecutor

import dspy

from bin import judgement as _judgement
from bin import llm_config as _llm_config
from bin import prompts as _prompts
from bin.context_playbook import (
    entries_for_domain,
    merge_entries,
    render_prompt,
    split_prompt,
)

FRONTIER_OPENROUTER_MODELS = _llm_config.FRONTIER_OPENROUTER_MODELS

DEFAULT_RUBRIC = _judgement.DEFAULT_TEMPLATE_NAME
VERDICTS = set(_judgement.PAIR_WHEEL_TEMPLATE_DEFINITION["verdict_enum"])

THE_OPTIMIZER_GRADES_AGAINST_THE_RUBRIC_THE_JUDGE_WAS_HANDED = (
    "VERDICTS is the enum SEEDED under DEFAULT_RUBRIC, not a copy of it. A "
    "copy that drifted made every stored human label 'invalid' to the metric, "
    "so the optimizer scored a perfect judge at zero and promoted nothing."
)
assert VERDICTS == {
    verdict
    for name, _version, definition, _prompt, _instructions
    in _judgement.WHEEL_SEED_TEMPLATES
    if name == DEFAULT_RUBRIC
    for verdict in definition["verdict_enum"]
}, THE_OPTIMIZER_GRADES_AGAINST_THE_RUBRIC_THE_JUDGE_WAS_HANDED

DATA_HOME = lambda: Path(os.environ.get("DATA_TOURNAMENTS_HOME", "/tmp/data-tournaments"))
DB_PATH = lambda: DATA_HOME() / "judgements.db"

def load_trainset(
    rubric: str = DEFAULT_RUBRIC, *, domain: Optional[str] = None
) -> list[dspy.Example]:
    """Load valid human preferences for one rubric and optional domain."""
    sql = """
    SELECT p.id AS pending_id, COALESCE(d.name, '_global') AS domain_name,
           s_v.value AS verdict, s_c.value AS confidence, p.trace_payload
    FROM score s_v
    JOIN score s_c ON s_c.rating_id = s_v.rating_id
                  AND s_c.name = 'judgement.confidence'
    JOIN pending_judgement p ON p.id = s_v.pending_id
    JOIN eval_template t ON t.id = s_v.template_id
    LEFT JOIN domain d ON d.id = p.domain_id
    WHERE s_v.name = 'judgement.verdict'
      AND t.name = ?
      AND json_extract(s_v.metadata, '$.rater.type') = 'human'
    """
    params: list[str] = [rubric]
    if domain is not None:
        sql += " AND d.name = ?"
        params.append(domain)
    sql += " ORDER BY s_v.created_at ASC, p.id ASC"

    conn = sqlite3.connect(f"file:{DB_PATH()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out: list[dspy.Example] = []
    try:
        for row in conn.execute(sql, params):
            if row["verdict"] not in VERDICTS:
                continue
            try:
                payload = json.loads(row["trace_payload"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            a = payload.get("card_a") or {}
            b = payload.get("card_b") or {}
            if not (a.get("title") or a.get("body")) or not (b.get("title") or b.get("body")):
                continue
            fingerprint = _example_fingerprint(a, b)
            out.append(
                dspy.Example(
                    example_id=str(row["pending_id"]),
                    example_fingerprint=fingerprint,
                    domain_name=row["domain_name"],
                    card_a_title=a.get("title", "(no title)"),
                    card_a_body=a.get("body", "(empty)"),
                    card_a_source_ref=a.get("source_ref", ""),
                    card_b_title=b.get("title", "(no title)"),
                    card_b_body=b.get("body", "(empty)"),
                    card_b_source_ref=b.get("source_ref", ""),
                    verdict=row["verdict"],
                    confidence=row["confidence"],
                ).with_inputs(
                    "card_a_title",
                    "card_a_body",
                    "card_a_source_ref",
                    "card_b_title",
                    "card_b_body",
                    "card_b_source_ref",
                )
            )
    finally:
        conn.close()
    return out

def _example_fingerprint(a: dict, b: dict) -> str:
    cards = sorted(
        [
            [a.get("title", ""), a.get("body", ""), a.get("source_ref", "")],
            [b.get("title", ""), b.get("body", ""), b.get("source_ref", "")],
        ]
    )
    canonical = json.dumps(cards, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()

def _verdict_side(value: str) -> str:
    for side in ("a", "b", "tie"):
        if value.startswith(side + "-"):
            return side
    return value

@dataclass(frozen=True)
class DatasetPartitions:
    train: list[dspy.Example]
    validation: list[dspy.Example]
    holdout: list[dspy.Example]
    duplicates_dropped: int
    digest: str

def partition_examples(examples: list[dspy.Example], *, seed: int = 0) -> DatasetPartitions:
    """Deduplicate and deterministically create leakage-free 3-way splits."""
    unique: dict[str, dspy.Example] = {}
    for example in examples:
        unique.setdefault(example.example_fingerprint, example)
    deduped = list(unique.values())
    if len(deduped) < 3:
        raise RuntimeError("need at least 3 distinct card pairs for train/validation/holdout")

    buckets: dict[str, list[dspy.Example]] = {}
    for example in deduped:
        buckets.setdefault(_verdict_side(example.verdict), []).append(example)
    for side, values in buckets.items():
        values.sort(
            key=lambda ex: hashlib.sha256(
                f"{seed}:{side}:{ex.example_fingerprint}".encode()
            ).hexdigest()
        )

    ordered: list[dspy.Example] = []
    sides = sorted(buckets, key=lambda side: (-len(buckets[side]), side))
    while any(buckets[side] for side in sides):
        for side in sides:
            if buckets[side]:
                ordered.append(buckets[side].pop(0))

    n = len(ordered)
    eval_size = 2 if n >= 7 else 1
    eval_size = min(eval_size, (n - 1) // 2)
    holdout = ordered[:eval_size]
    validation = ordered[eval_size : eval_size * 2]
    train = ordered[eval_size * 2 :]
    digest = hashlib.sha256(
        "|".join(example.example_fingerprint for example in ordered).encode()
    ).hexdigest()[:16]
    return DatasetPartitions(
        train=train,
        validation=validation,
        holdout=holdout,
        duplicates_dropped=len(examples) - len(deduped),
        digest=digest,
    )

def verdict_score(gold: str, got: str) -> float:
    if got not in VERDICTS:
        return 0.0
    if gold == got:
        return 1.0
    if _verdict_side(gold) == _verdict_side(got):
        return 0.6
    return 0.0

def verdict_match_metric(example, pred, trace=None, pred_name=None, pred_trace=None):
    """GEPA metric with actionable, per-trajectory textual feedback."""
    gold = example.verdict
    got = (getattr(pred, "verdict", "") or "").strip()
    score = verdict_score(gold, got)
    gold_side, got_side = _verdict_side(gold), _verdict_side(got)
    if got not in VERDICTS:
        diagnosis = f"The verdict {got!r} is invalid. Emit exactly one allowed rubric label."
    elif score == 1.0:
        diagnosis = "The verdict exactly matches the human preference. Preserve this decision rule."
    elif gold_side == got_side:
        diagnosis = (
            f"The preferred side ({gold_side}) is correct, but the strength/quality label is not: "
            f"human={gold!r}, model={got!r}. Calibrate clear vs marginal and strong vs weak."
        )
    else:
        diagnosis = (
            f"The decision direction is wrong: human={gold!r}, model={got!r}. "
            "Recompare specificity, evidence, actionability, novelty, and impact before committing."
        )
    rationale = (getattr(pred, "rationale", "") or "").strip()
    card_a_title = getattr(example, "card_a_title", "(unknown)")
    card_b_title = getattr(example, "card_b_title", "(unknown)")
    feedback = (
        f"{diagnosis}\nCard A: {card_a_title!r}; Card B: {card_b_title!r}.\n"
        f"Model rationale: {rationale or '(missing)'}."
    )
    return dspy.Prediction(score=score, feedback=feedback)

@dataclass(frozen=True)
class ExampleOutcome:
    example_id: str
    gold: str
    predicted: str
    score: float
    error: Optional[str] = None

@dataclass(frozen=True)
class EvaluationSummary:
    score: float
    exact_accuracy: float
    side_accuracy: float
    invalid_rate: float
    examples: int
    outcomes: list[ExampleOutcome]

    def public_dict(self) -> dict:
        data = asdict(self)
        data.pop("outcomes")
        return data

def _score_example(program, example: dspy.Example) -> ExampleOutcome:
    try:
        pred = program(**example.inputs())
        got = (getattr(pred, "verdict", "") or "").strip()
        return ExampleOutcome(
            example_id=example.example_id,
            gold=example.verdict,
            predicted=got,
            score=verdict_score(example.verdict, got),
        )
    except Exception as exc:
        return ExampleOutcome(
            example_id=example.example_id,
            gold=example.verdict,
            predicted="",
            score=0.0,
            error=f"{type(exc).__name__}: {exc}",
        )

def _evaluate_program(program, examples: list[dspy.Example]) -> EvaluationSummary:
    workers = min(_llm_config.optimizer_concurrency(), len(examples)) or 1
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(pool.map(lambda e: _score_example(program, e), examples))
    else:
        outcomes = [_score_example(program, e) for e in examples]
    count = len(outcomes)
    return EvaluationSummary(
        score=sum(item.score for item in outcomes) / count,
        exact_accuracy=sum(item.gold == item.predicted for item in outcomes) / count,
        side_accuracy=sum(
            _verdict_side(item.gold) == _verdict_side(item.predicted) for item in outcomes
        )
        / count,
        invalid_rate=sum(item.predicted not in VERDICTS for item in outcomes) / count,
        examples=count,
        outcomes=outcomes,
    )

def _compile_with_gepa(
    program,
    trainset,
    metric,
    *,
    valset,
    auto=None,
    max_metric_calls=None,
    reflection_lm=None,
    seed=0,
    log_dir=None,
):
    from dspy.teleprompt import GEPA

    budget = {"auto": auto} if auto else {"max_metric_calls": max_metric_calls}
    optimizer = GEPA(
        metric=metric,
        reflection_lm=reflection_lm,
        reflection_minibatch_size=min(3, len(trainset)),
        candidate_selection_strategy="pareto",
        seed=seed,
        log_dir=str(log_dir),
        track_stats=True,
        add_format_failure_as_feedback=True,
        num_threads=_llm_config.optimizer_concurrency(),
        **budget,
    )
    return optimizer.compile(program, trainset=trainset, valset=valset)

class CurateContextDelta(dspy.Signature):
    """Extract durable judge lessons as structured deltas, never a replacement prompt.

    Return strict JSON with an `entries` array. Each entry may carry an `op`:
    "add" (default) introduces a lesson and requires `section` (strategy,
    evidence, or mistake) plus a detailed, reusable `content` string;
    "reinforce" endorses an existing playbook entry; "weaken" flags an existing
    entry as harmful; "retire" removes one. Lifecycle ops must target an
    existing entry via its `id` (shown in brackets in the seed playbook) or its
    exact content. Preserve distinct useful details, reject tautologies, and
    produce at most 12 entries. Do not copy task-specific titles or reveal the
    human verdict.
    """

    seed_context = dspy.InputField(desc="Current production prompt and existing playbook")
    evolved_context = dspy.InputField(desc="Best context discovered by GEPA")
    optimization_evidence = dspy.InputField(desc="Dataset and search provenance")
    delta_json = dspy.OutputField(desc='Strict JSON: {"entries":[{"op":"add|reinforce|weaken|retire","id":"(lifecycle ops only)","section":"strategy|evidence|mistake","content":"..."}]}')

def _json_object(value: str) -> dict:
    text = (value or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("curator output must be a JSON object")
    return parsed

def _scope_deltas(deltas: list, domain: str) -> list:
    """Domain-scoped runs must not mint or claim global/foreign lessons.

    Every delta from a scoped run is forced into the active domain — even if
    the curator explicitly claimed another domain. Global (domain-less) runs
    pass through unchanged.
    """
    if not domain:
        return deltas
    return [{**item, "domain": domain} for item in deltas]

def _playbook_change_stats(existing, merged) -> dict:
    """Per-op breakdown of what a merge actually did to the playbook.

    ``len(merged) - len(existing)`` is meaningless once retire/prune ops
    exist (a run that retires two entries and adds one nets -1), so callers
    gate on real change instead of net growth.
    """
    existing_by_id = {entry.id: entry for entry in existing}
    merged_by_id = {entry.id: entry for entry in merged}
    return {
        "added": sum(1 for eid in merged_by_id if eid not in existing_by_id),
        "removed": sum(1 for eid in existing_by_id if eid not in merged_by_id),
        "reinforced": sum(
            1
            for eid, entry in merged_by_id.items()
            if eid in existing_by_id and entry.helpful > existing_by_id[eid].helpful
        ),
        "weakened": sum(
            1
            for eid, entry in merged_by_id.items()
            if eid in existing_by_id and entry.harmful > existing_by_id[eid].harmful
        ),
    }

def _curate_context(
    seed_prompt: str,
    evolved_prompt: str,
    evidence: dict,
    curator_lm,
    *,
    provenance: str = "",
    domain: str = "",
):
    with dspy.context(lm=curator_lm):
        result = dspy.Predict(CurateContextDelta)(
            seed_context=seed_prompt,
            evolved_context=evolved_prompt,
            optimization_evidence=json.dumps(evidence, sort_keys=True),
        )
    payload = _json_object(result.delta_json)
    deltas = payload.get("entries")
    if not isinstance(deltas, list):
        raise ValueError("curator JSON must contain an entries array")
    cleaned = [item for item in deltas[:12] if isinstance(item, dict)]
    cleaned = _scope_deltas(cleaned, domain)
    base, existing = split_prompt(seed_prompt)
    merged = merge_entries(existing, cleaned, provenance=provenance, domain=domain)
    stats = _playbook_change_stats(existing, merged)
    if domain:
        rendered = entries_for_domain(merged, domain)
        stats["foreign_excluded"] = len(merged) - len(rendered)
    else:
        rendered = merged
    return render_prompt(base, rendered), stats, cleaned

def _extract_instructions(program) -> str:
    predictor = getattr(program, "predictor", None)
    signature = getattr(predictor, "signature", None)
    instructions = getattr(signature, "instructions", None)
    if instructions:
        return instructions
    for _, named_predictor in program.named_predictors():
        instructions = getattr(getattr(named_predictor, "signature", None), "instructions", None)
        if instructions:
            return instructions
    raise RuntimeError("GEPA returned a program without optimized predictor instructions")

def _build_lm(model: Optional[str] = None, max_tokens: int = 16000) -> dspy.LM:
    return _build_role_lm(model, temperature=0.0, max_tokens=max_tokens)

def _build_reflection_lm(model: Optional[str] = None) -> dspy.LM:
    cfg = _llm_config.optimizer_lm_config("reflection", model)
    return _build_role_lm(cfg.model, temperature=cfg.temperature, max_tokens=cfg.max_tokens)

def _build_curator_lm(model: Optional[str] = None) -> dspy.LM:
    cfg = _llm_config.optimizer_lm_config("curator", model)
    return _build_role_lm(cfg.model, temperature=cfg.temperature, max_tokens=cfg.max_tokens)

def _default_model(index: int) -> str:
    return _llm_config.default_model(index)

def _build_role_lm(
    model: Optional[str],
    *,
    temperature: float,
    max_tokens=None,
    timeout: Optional[float] = None,
    num_retries: Optional[int] = None,
):
    cfg = _llm_config.role_lm_config(
        model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        num_retries=num_retries,
    )
    kwargs = {
        "model": f"openai/{cfg.model}",
        "api_base": cfg.base_url,
        "api_key": cfg.api_key,
        "temperature": cfg.temperature,
        "timeout": cfg.timeout,
        "num_retries": cfg.num_retries,
    }
    if cfg.max_tokens is not None:
        kwargs["max_tokens"] = cfg.max_tokens
    return dspy.LM(**kwargs)

DEFAULT_MIN_IMPROVEMENT = 0.01

ACCEPTANCE_CONJUNCTS = (
    "effective_change",
    "improvement_margin",
    "exact_accuracy",
    "invalid_rate",
)

def _resolve_min_improvement(value: Optional[float]) -> tuple[float, str]:
    """The promotion margin and where it came from — caller, environment,
    or the module default. Provenance is recorded on the run row so the
    number is never just a signature default nobody can audit."""
    if value is not None:
        return float(value), "caller"
    env = os.environ.get("OPTIMIZER_MIN_IMPROVEMENT")
    if env:
        return float(env), "env:OPTIMIZER_MIN_IMPROVEMENT"
    return DEFAULT_MIN_IMPROVEMENT, "default"

def _resolve_margin_evidence_floor(value: Optional[int]) -> tuple[Optional[int], str]:
    if value is not None:
        return int(value), "caller"
    env = os.environ.get("OPTIMIZER_MARGIN_EVIDENCE_FLOOR")
    if env:
        return int(env), "env:OPTIMIZER_MARGIN_EVIDENCE_FLOOR"
    return None, "unset"

def margin_resolution_floor(min_improvement: float) -> int:
    """Holdout examples needed for a margin of ``min_improvement`` to be a
    real threshold.

    One holdout example is worth 1/N of the score, so on a holdout of N
    examples no improvement can land strictly between 0 and 1/N. A margin
    finer than 1/N therefore fails nothing it would not also fail at 0 —
    the fixed-threshold-without-a-null hazard. ceil(1/margin) is the
    smallest holdout on which the configured margin bites.
    """
    if min_improvement <= 0:
        return 0
    return math.ceil(1.0 / min_improvement)

def margin_policy(
    *,
    min_improvement: float,
    source: str,
    holdout_size: int,
    evidence_floor: Optional[int] = None,
    evidence_floor_source: str = "unset",
) -> dict:
    """The margin, its provenance, and the holdout resolution it needs.

    ``effective_min_improvement`` is the larger of the configured margin
    and one holdout example's worth of score: a run cannot buy resolution
    it did not measure. ``binding`` names which of the two is doing the
    work, so the run row says whether the threshold was a judgement call
    or a measurement limit.
    """
    resolution_floor = margin_resolution_floor(min_improvement)
    holdout_resolution = 1.0 / holdout_size if holdout_size > 0 else float("inf")
    effective = max(min_improvement, holdout_resolution)
    return {
        "min_improvement": min_improvement,
        "source": source,
        "resolution_floor_examples": resolution_floor,
        "holdout_examples": holdout_size,
        "holdout_resolution": holdout_resolution,
        "effective_min_improvement": effective,
        "binding": (
            "configured-margin"
            if min_improvement >= holdout_resolution
            else "holdout-resolution"
        ),
        "evidence_floor": evidence_floor,
        "evidence_floor_source": evidence_floor_source,
    }

def _margin_verdict(improvement: float, policy: dict) -> tuple[bool, bool]:
    """(margin conjunct holds, the evidence is thick enough to judge it).

    An improvement that clears the configured margin but not one holdout
    example's worth of score is not a small win — it is an unmeasurable
    one, reported as insufficient-evidence rather than accepted.
    """
    holds = improvement >= policy["effective_min_improvement"]
    if holds:
        return True, True
    unmeasurable = (
        improvement >= policy["min_improvement"]
        and improvement < policy["holdout_resolution"]
    )
    return False, not unmeasurable

@dataclass
class OptimizeResult:
    accepted: bool
    decision: str
    candidate_version: Optional[int]
    total_examples: int
    trainset_size: int
    validation_size: int
    holdout_size: int
    dataset_digest: str
    seed: int
    budget: str
    baseline: dict
    candidate: dict
    improvement: float
    judge_model: Optional[str]
    reflection_model: Optional[str]
    curator_model: Optional[str]
    gepa: dict
    playbook_entries_added: int
    artifact_dir: str
    margin: dict = field(default_factory=dict)
    conjuncts: dict = field(default_factory=dict)
    deciding_conjunct: str = ""

def _gepa_stats(program) -> dict:
    details = getattr(program, "detailed_results", None)
    if details is None:
        return {}
    scores = list(getattr(details, "val_aggregate_scores", []) or [])
    return {
        "candidates": len(getattr(details, "candidates", []) or []),
        "total_metric_calls": getattr(details, "total_metric_calls", None),
        "full_validation_evals": getattr(details, "num_full_val_evals", None),
        "seed_validation_score": scores[0] if scores else None,
        "best_validation_score": max(scores) if scores else None,
        "best_candidate_index": getattr(details, "best_idx", None),
    }

def _paired_changes(baseline: EvaluationSummary, candidate: EvaluationSummary) -> dict:
    base = {item.example_id: item.score for item in baseline.outcomes}
    deltas = [item.score - base[item.example_id] for item in candidate.outcomes]
    return {
        "improved": sum(delta > 0 for delta in deltas),
        "unchanged": sum(delta == 0 for delta in deltas),
        "regressed": sum(delta < 0 for delta in deltas),
    }

def _artifact_dir(prompt_name: str, seed: int) -> Path:
    safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", prompt_name).strip("-")
    run_name = f"{time.strftime('%Y%m%d-%H%M%S')}-{seed}-{uuid.uuid4().hex[:8]}"
    path = DATA_HOME() / "optimizer" / safe_name / run_name
    path.mkdir(parents=True, exist_ok=False)
    return path

def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")

def _insufficient_evidence_result(
    *,
    reason: str,
    total_examples: int,
    trainset_size: int,
    validation_size: int,
    holdout_size: int,
    digest: str,
    seed: int,
    budget: str,
    prompt_name: str,
    margin: Optional[dict] = None,
) -> OptimizeResult:
    """Retain the production seed without spending any LM budget."""
    print(
        f"[optimize] retained production seed (insufficient-evidence): {reason}",
        flush=True,
    )
    run_dir = _artifact_dir(prompt_name, seed)
    result = OptimizeResult(
        accepted=False,
        decision="insufficient-evidence",
        candidate_version=None,
        total_examples=total_examples,
        trainset_size=trainset_size,
        validation_size=validation_size,
        holdout_size=holdout_size,
        dataset_digest=digest,
        seed=seed,
        budget=budget,
        baseline={},
        candidate={},
        improvement=0.0,
        judge_model=None,
        reflection_model=None,
        curator_model=None,
        gepa={"insufficient_evidence_reason": reason},
        playbook_entries_added=0,
        artifact_dir=str(run_dir),
        margin=dict(margin or {}),
        conjuncts={},
        deciding_conjunct="evidence",
    )
    _write_json(run_dir / "result.json", asdict(result))
    return result

def run(
    *,
    rubric: str = DEFAULT_RUBRIC,
    auto: Optional[str] = None,
    max_metric_calls: int = 40,
    min_trainset: int = 7,
    min_improvement: Optional[float] = None,
    margin_evidence_floor: Optional[int] = None,
    prompt_name: str = "judge-instructions",
    domain: Optional[str] = None,
    model: Optional[str] = None,
    reflection_model: Optional[str] = None,
    curator_model: Optional[str] = None,
    seed: int = 0,
) -> OptimizeResult:
    min_improvement, margin_source = _resolve_min_improvement(min_improvement)
    evidence_floor, evidence_floor_source = _resolve_margin_evidence_floor(
        margin_evidence_floor
    )
    examples = load_trainset(rubric=rubric, domain=domain)
    scope = f" for domain {domain!r}" if domain else " across all domains"
    print(f"[optimize] loaded {len(examples)} valid human examples{scope}", flush=True)
    if len(examples) < min_trainset:
        raise RuntimeError(
            f"need at least {min_trainset} human judgements to optimize; "
            f"have {len(examples)} on rubric {rubric!r}{scope}"
        )
    budget = f"auto:{auto}" if auto else f"metric_calls:{max_metric_calls}"
    try:
        partitions = partition_examples(examples, seed=seed)
    except RuntimeError as exc:
        return _insufficient_evidence_result(
            reason=str(exc),
            total_examples=len(examples),
            trainset_size=0,
            validation_size=0,
            holdout_size=0,
            digest="",
            seed=seed,
            budget=budget,
            prompt_name=prompt_name,
            margin=margin_policy(
                min_improvement=min_improvement,
                source=margin_source,
                holdout_size=0,
                evidence_floor=evidence_floor,
                evidence_floor_source=evidence_floor_source,
            ),
        )
    print(
        "[optimize] split "
        f"train={len(partitions.train)} validation={len(partitions.validation)} "
        f"holdout={len(partitions.holdout)} seed={seed} digest={partitions.digest}",
        flush=True,
    )
    if partitions.duplicates_dropped:
        print(f"[optimize] dropped {partitions.duplicates_dropped} duplicate pairs", flush=True)

    min_validation = _llm_config.optimizer_min_validation()
    min_holdout = _llm_config.optimizer_min_holdout()
    margin = margin_policy(
        min_improvement=min_improvement,
        source=margin_source,
        holdout_size=len(partitions.holdout),
        evidence_floor=evidence_floor,
        evidence_floor_source=evidence_floor_source,
    )
    print(
        f"[optimize] promotion margin {min_improvement} (source {margin_source}); "
        f"it bites from {margin['resolution_floor_examples']} holdout examples, "
        f"this run has {margin['holdout_examples']}; effective margin "
        f"{margin['effective_min_improvement']:.3f} ({margin['binding']})",
        flush=True,
    )
    if len(partitions.validation) < min_validation or len(partitions.holdout) < min_holdout:
        return _insufficient_evidence_result(
            reason=(
                f"validation={len(partitions.validation)} (min {min_validation}), "
                f"holdout={len(partitions.holdout)} (min {min_holdout})"
            ),
            total_examples=len(examples),
            trainset_size=len(partitions.train),
            validation_size=len(partitions.validation),
            holdout_size=len(partitions.holdout),
            digest=partitions.digest,
            seed=seed,
            budget=budget,
            prompt_name=prompt_name,
            margin=margin,
        )
    if evidence_floor is not None and len(partitions.holdout) < evidence_floor:
        return _insufficient_evidence_result(
            reason=(
                f"holdout={len(partitions.holdout)} is below the configured "
                f"margin evidence floor {evidence_floor} "
                f"({evidence_floor_source}) for min_improvement="
                f"{min_improvement}"
            ),
            total_examples=len(examples),
            trainset_size=len(partitions.train),
            validation_size=len(partitions.validation),
            holdout_size=len(partitions.holdout),
            digest=partitions.digest,
            seed=seed,
            budget=budget,
            prompt_name=prompt_name,
            margin=margin,
        )

    judge_lm = _build_lm(model=model)
    reflection_lm = _build_reflection_lm(model=reflection_model)
    curator_lm = _build_curator_lm(model=curator_model)
    print(f"[optimize] generator/judge model: {judge_lm.model}", flush=True)
    print(f"[optimize] reflector model: {reflection_lm.model}", flush=True)
    print(f"[optimize] curator model: {curator_lm.model}", flush=True)

    run_dir = _artifact_dir(prompt_name, seed)
    seed_prompt = _prompts.get(prompt_name, label="production")
    from bin.judges.match_judge import MatchJudge

    baseline_program = MatchJudge(instructions=seed_prompt)
    with dspy.context(lm=judge_lm):
        baseline = _evaluate_program(baseline_program, partitions.holdout)
    print(
        f"[optimize] untouched holdout baseline={baseline.score:.3f} "
        f"exact={baseline.exact_accuracy:.3f}",
        flush=True,
    )

    print(f"[optimize] starting GEPA ({budget})", flush=True)
    with dspy.context(lm=judge_lm):
        optimized = _compile_with_gepa(
            baseline_program,
            partitions.train,
            verdict_match_metric,
            valset=partitions.validation,
            auto=auto,
            max_metric_calls=None if auto else max_metric_calls,
            reflection_lm=reflection_lm,
            seed=seed,
            log_dir=run_dir / "gepa",
        )
    evolved_prompt = _extract_instructions(optimized)
    gepa_stats = _gepa_stats(optimized)
    print(
        f"[optimize] GEPA proposed {gepa_stats.get('candidates', '?')} candidates "
        f"using {gepa_stats.get('total_metric_calls', '?')} metric calls",
        flush=True,
    )

    evidence = {
        "rubric": rubric,
        "domain": domain,
        "dataset_digest": partitions.digest,
        "train_examples": len(partitions.train),
        "validation_examples": len(partitions.validation),
        "gepa": gepa_stats,
    }
    candidate_prompt, playbook_changes, curator_deltas = _curate_context(
        seed_prompt,
        evolved_prompt,
        evidence,
        curator_lm,
        provenance=run_dir.name,
        domain=domain or "",
    )
    if len(candidate_prompt) > _llm_config.optimizer_context_char_budget():
        raise RuntimeError("curated context exceeds OPTIMIZER_CONTEXT_CHAR_BUDGET")
    print(f"[optimize] curator playbook changes: {playbook_changes}", flush=True)

    candidate_program = MatchJudge(instructions=candidate_prompt)
    with dspy.context(lm=judge_lm):
        candidate = _evaluate_program(candidate_program, partitions.holdout)
    improvement = candidate.score - baseline.score
    paired = _paired_changes(baseline, candidate)
    print(
        f"[optimize] untouched holdout candidate={candidate.score:.3f} "
        f"exact={candidate.exact_accuracy:.3f} delta={improvement:+.3f} "
        f"paired={paired}",
        flush=True,
    )

    effective_change = (
        sum(playbook_changes.get(k, 0) for k in ("added", "removed", "reinforced", "weakened")) > 0
    )
    margin_holds, margin_measurable = _margin_verdict(improvement, margin)
    conjuncts = {
        "effective_change": effective_change,
        "improvement_margin": margin_holds,
        "exact_accuracy": candidate.exact_accuracy >= baseline.exact_accuracy,
        "invalid_rate": candidate.invalid_rate <= baseline.invalid_rate,
    }
    failed = [name for name in ACCEPTANCE_CONJUNCTS if not conjuncts[name]]
    accepted = not failed
    if not margin_measurable:
        accepted = False
        decision = "insufficient-evidence"
        deciding_conjunct = "improvement_margin"
    elif accepted:
        decision = "accepted"
        deciding_conjunct = "all-conjuncts-hold"
    elif improvement < 0 or candidate.exact_accuracy < baseline.exact_accuracy:
        decision = "regression"
        deciding_conjunct = (
            "exact_accuracy" if not conjuncts["exact_accuracy"] else failed[0]
        )
    else:
        decision = "plateau"
        deciding_conjunct = failed[0]
    margin = {
        **margin,
        "improvement": improvement,
        "holds": margin_holds,
        "measurable": margin_measurable,
    }
    print(
        f"[optimize] decision {decision} decided by {deciding_conjunct} "
        f"(conjuncts {conjuncts})",
        flush=True,
    )
    if not margin_measurable:
        print(
            f"[optimize] improvement {improvement:+.3f} clears the configured "
            f"margin {min_improvement} but not one holdout example "
            f"({margin['holdout_resolution']:.3f} on "
            f"{margin['holdout_examples']} examples) — insufficient evidence, "
            f"not a small win",
            flush=True,
        )

    candidate_version = None
    if accepted:
        candidate_version = _prompts.push(prompt_name, candidate_prompt, labels=["candidate"])
        print(
            f"[optimize] accepted measured improvement; candidate v{candidate_version} created",
            flush=True,
        )
    else:
        print(
            f"[optimize] retained production seed ({decision}); no candidate label created",
            flush=True,
        )

    result = OptimizeResult(
        accepted=accepted,
        decision=decision,
        candidate_version=candidate_version,
        total_examples=len(examples),
        trainset_size=len(partitions.train),
        validation_size=len(partitions.validation),
        holdout_size=len(partitions.holdout),
        dataset_digest=partitions.digest,
        seed=seed,
        budget=budget,
        baseline=baseline.public_dict(),
        candidate=candidate.public_dict(),
        improvement=improvement,
        judge_model=judge_lm.model,
        reflection_model=reflection_lm.model,
        curator_model=curator_lm.model,
        gepa={**gepa_stats, "paired_holdout": paired},
        playbook_entries_added=playbook_changes["added"],
        artifact_dir=str(run_dir),
        margin=margin,
        conjuncts=conjuncts,
        deciding_conjunct=deciding_conjunct,
    )
    _write_json(
        run_dir / "result.json",
        {
            **asdict(result),
            "baseline_outcomes": [asdict(item) for item in baseline.outcomes],
            "candidate_outcomes": [asdict(item) for item in candidate.outcomes],
            "curator_deltas": curator_deltas,
            "playbook_changes": playbook_changes,
            "playbook_provenance": run_dir.name,
            "playbook_added_entry_ids": sorted(
                {entry.id for entry in split_prompt(candidate_prompt)[1]}
                - {entry.id for entry in split_prompt(seed_prompt)[1]}
            ),
        },
    )
    (run_dir / "seed-prompt.txt").write_text(seed_prompt)
    (run_dir / "gepa-evolved-prompt.txt").write_text(evolved_prompt)
    (run_dir / "curated-candidate-prompt.txt").write_text(candidate_prompt)
    print(f"[optimize] artifacts: {run_dir}", flush=True)
    return result

def run_with_persistence(*, run_id: int, **kwargs) -> None:
    from bin import optimizer_runs

    line_buf: list[str] = []

    class _Tee:
        def write(self, value):
            sys.__stdout__.write(value)
            line_buf.append(value)
            if "\n" in value:
                joined = "".join(line_buf)
                *complete, remainder = joined.split("\n")
                for line in complete:
                    if line.strip():
                        optimizer_runs.append_log(run_id, line)
                line_buf.clear()
                if remainder:
                    line_buf.append(remainder)
            return len(value)

        def flush(self):
            sys.__stdout__.flush()

    try:
        with redirect_stdout(_Tee()):
            result = run(**kwargs)
        optimizer_runs.finish(run_id, status="done", exit_code=0, result=asdict(result))
    except RuntimeError as exc:
        optimizer_runs.append_log(run_id, f"[optimize] aborted: {exc}")
        optimizer_runs.finish(
            run_id, status="error", exit_code=2, result={"error": str(exc)}
        )
    except Exception as exc:
        optimizer_runs.append_log(run_id, f"[optimize] crashed: {exc}")
        optimizer_runs.finish(
            run_id, status="error", exit_code=1, result={"error": str(exc)}
        )
        raise

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rubric", default=DEFAULT_RUBRIC)
    budget = parser.add_mutually_exclusive_group()
    budget.add_argument("--auto", choices=["light", "medium", "heavy"], default=None)
    budget.add_argument("--max-metric-calls", type=int, default=40)
    parser.add_argument("--min-trainset", type=int, default=7)
    parser.add_argument(
        "--min-improvement", type=float, default=None,
        help="promotion margin; default reads OPTIMIZER_MIN_IMPROVEMENT, "
             f"else {DEFAULT_MIN_IMPROVEMENT}. Its provenance is recorded "
             "on the run row",
    )
    parser.add_argument(
        "--margin-evidence-floor", type=int, default=None,
        help="minimum holdout examples before the margin may accept a run; "
             "default reads OPTIMIZER_MARGIN_EVIDENCE_FLOOR. Below it a run "
             "is insufficient-evidence, never accepted",
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--prompt-name", default="judge-instructions")
    parser.add_argument("--domain", default=None)
    parser.add_argument("--model", default=None, help="generator/judge model id")
    parser.add_argument("--reflection-model", default=None)
    parser.add_argument("--curator-model", default=None)
    parser.add_argument("--run-id", type=int, default=None)
    args = parser.parse_args()
    kwargs = {
        "rubric": args.rubric,
        "auto": args.auto,
        "max_metric_calls": args.max_metric_calls,
        "min_trainset": args.min_trainset,
        "min_improvement": args.min_improvement,
        "margin_evidence_floor": args.margin_evidence_floor,
        "prompt_name": args.prompt_name,
        "domain": args.domain,
        "model": args.model,
        "reflection_model": args.reflection_model,
        "curator_model": args.curator_model,
        "seed": args.seed,
    }
    if args.run_id is not None:
        run_with_persistence(run_id=args.run_id, **kwargs)
        return
    try:
        run(**kwargs)
    except RuntimeError as exc:
        print(f"[optimize] aborted: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(2) from exc

if __name__ == "__main__":
    main()

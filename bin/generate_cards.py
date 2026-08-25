"""Generate cards from a domain's corpus and enqueue pairs for the judge wheel.

CLI::

    nix run .#generate-cards -- --domain memory-extraction --limit 50

Output is line-oriented for Phoenix Port-tail consumption.
"""
from __future__ import annotations
import argparse
import json
import os
import random
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import dspy

from bin import domains as _domains
from bin import llm_config as _llm_config
from bin import prompts as _prompts
from bin.corpus import iter_filesystem_paths
from bin.env_loader import load_dotenv as _load_dotenv
from bin.generators.card_gen import Card, CardGen, CardGenError
from bin.generators.workorder_gen import WorkOrderGen
from bin.workorder import capture_repo_snapshot, finalize_work_order, to_markdown

# Phoenix shell-outs inherit only the server's environment; without the repo
# .env the generation LM silently falls back to the keyless default endpoint
# and every corpus item fails with an opaque auth/connect error.
_load_dotenv()


def _db_path() -> Path:
    home = Path(os.environ.get("DATA_TOURNAMENTS_HOME", "/tmp/data-tournaments"))
    return home / "judgements.db"


# Module-level handles so tests can monkey-patch.
CardGen = CardGen  # noqa: F811
WorkOrderGen = WorkOrderGen  # noqa: F811


@dataclass
class GenerateResult:
    cards_generated: int
    pairs_enqueued: int
    errors: int
    # Singles path (--judgement-kind single / single-kind rubric): number of
    # per-artifact pending rows written. Zero on the pair path.
    singles_enqueued: int = 0
    # Per-class failure counts keyed by failure class:
    # "timeout" | "parse-error" | "truncation" | "error" (unclassified).
    failures: dict = field(default_factory=dict)
    # Non-empty when the run stopped early because the provider itself was
    # down/unauthenticated (as opposed to per-item content failures).
    aborted_reason: str = ""


# Substrings that mark a provider-level (systemic) failure: retrying the
# next corpus item cannot succeed, so the loop must stop instead of
# emitting one identical error per file across an entire repository.
_SYSTEMIC_ERROR_MARKERS = (
    "AuthenticationError",
    "All connection attempts failed",
    "Cannot connect to host",
    "Connection refused",
)
_SYSTEMIC_ABORT_AFTER = 3


def _build_generation_lm() -> dspy.LM:
    """Build a bounded LM for interactive corpus fan-out.

    Generation runs once per corpus item, so the optimizer's deliberately
    generous retry/output defaults can make one bad request look like a hung
    Domains job. Keep this role short and fail fast; the item loop already
    records an error and continues with the remaining corpus.
    """
    from bin.optimize import _build_role_lm

    cfg = _llm_config.generator_config()
    return _build_role_lm(
        cfg.model,
        temperature=cfg.temperature,
        max_tokens=cfg.max_tokens,
        timeout=cfg.timeout,
        num_retries=cfg.num_retries,
    )


def _iter_corpus(spec: _domains.DomainSpec) -> Iterator[dict]:
    """Yield {text, source_ref} dicts from the domain's corpus_source."""
    src = spec.corpus_source
    kind = src["kind"]
    if kind == "inline":
        for i, item in enumerate(src.get("items", [])):
            text = item.get("text") or item.get("body") or json.dumps(item)
            ref = item.get("source_ref") or item.get("ref") or f"inline:{i}"
            yield {"text": text, "source_ref": ref}
    elif kind == "filesystem":
        for path in iter_filesystem_paths(src):
            try:
                yield {"text": path.read_text(encoding="utf-8"), "source_ref": str(path)}
            except (OSError, UnicodeError):
                continue
    elif kind == "sqlite":
        conn = sqlite3.connect(f"file:{src['path']}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute(src["query"]):
                d = {k: row[k] for k in row.keys()}
                text = d.get("text") or d.get("content") or d.get("body") or ""
                ref = d.get("source_ref") or d.get("ref") or str(d.get("id") or "?")
                yield {"text": text, "source_ref": ref}
        finally:
            conn.close()
    else:  # pragma: no cover — _validate_corpus_source guards this
        raise ValueError(f"unknown corpus_source kind: {kind!r}")


def _enqueue_pairs(domain_id: int, items: list[dict], rng: random.Random) -> int:
    """Bracket generated artifacts and write pending rows with domain-unique
    match ids.

    ``items`` are payload dicts (a card's ``model_dump()`` or a work order's
    display payload). Repeated generation runs continue after the domain's
    highest match id; otherwise Results would merge unrelated pairs that both
    used slot zero. The last item with an odd count gets a bye (no enqueue).
    Returns the number of pairs enqueued.

    Domain pairs are reviewed by HUMANS (the review bar — user-devs stand at
    the end of the loop), so exactly ONE pending row is written per pair,
    against a single human-rater config. The pre-fix behaviour inserted one
    row per ACTIVE config (3-model LLM panel + human = 4 identical rows per
    pair) whenever the domain's rubric matched no eval_template name — the
    LLM rows were never drained by any domain flow and inflated the queue
    (wave-9 L6).
    """
    shuffled = list(items)
    rng.shuffle(shuffled)
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    enqueued = 0
    try:
        cfgs = conn.execute(
            "SELECT c.id, c.rater_type, t.name FROM job_configuration c "
            "JOIN eval_template t ON t.id = c.template_id "
            "WHERE c.status='active' AND c.rater_type='human'"
        ).fetchall()
        if not cfgs:
            raise RuntimeError(
                "no active human job_configuration exists — run judgement init "
                "before generating domain pairs"
            )
        # Prefer a human config whose template matches this domain's rubric;
        # otherwise use the first active human config. Exactly ONE config —
        # one pending row per pair.
        spec = conn.execute(
            "SELECT rubric FROM domain WHERE id=?", (domain_id,)
        ).fetchone()
        rubric = spec["rubric"] if spec else None
        matched = [c for c in cfgs if rubric is not None and c["name"] == rubric]
        cfg = (matched or cfgs)[0]

        i = 0
        last_match = conn.execute(
            "SELECT COALESCE(MAX(match_id), -1) FROM pending_judgement "
            "WHERE domain_id=?",
            (domain_id,),
        ).fetchone()[0]
        slot = int(last_match) + 1
        while i + 1 < len(shuffled):
            a, b = shuffled[i], shuffled[i + 1]
            payload = json.dumps({
                "label": f"R1-{slot + 1}",
                "card_a": a,
                "card_b": b,
            })
            conn.execute(
                "INSERT INTO pending_judgement(config_id, tournament_db_path, "
                "match_id, trace_payload, domain_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (cfg["id"], f"domain:{domain_id}", slot, payload, domain_id),
            )
            enqueued += 1
            slot += 1
            i += 2
        conn.commit()
    finally:
        conn.close()
    return enqueued


def _human_config_for_rubric(conn: sqlite3.Connection, domain_id: int,
                             rubric: Optional[str] = None) -> sqlite3.Row:
    """Resolve the ONE human job_configuration a domain's pendings bind to.

    Prefer a human config whose template matches the rubric (explicit
    override or the domain's own); otherwise the first active human config.
    Exactly ONE config — one pending row per pair/artifact (the wave-9 L6
    invariant: domain judgements are reviewed by HUMANS, never fanned out
    across the LLM panel).
    """
    cfgs = conn.execute(
        "SELECT c.id, c.rater_type, t.name FROM job_configuration c "
        "JOIN eval_template t ON t.id = c.template_id "
        "WHERE c.status='active' AND c.rater_type='human'"
    ).fetchall()
    if not cfgs:
        raise RuntimeError(
            "no active human job_configuration exists — run judgement init "
            "before generating domain pairs"
        )
    if rubric is None:
        spec = conn.execute(
            "SELECT rubric FROM domain WHERE id=?", (domain_id,)
        ).fetchone()
        rubric = spec["rubric"] if spec else None
    matched = [c for c in cfgs if rubric is not None and c["name"] == rubric]
    return (matched or cfgs)[0]


def enqueue_singles(domain_id: int, items: list[dict],
                    rubric: Optional[str] = None) -> int:
    """Write ONE pending row per generated artifact for single judgement.

    Mirrors ``_enqueue_pairs`` but never pretends to be a pair: the
    trace_payload is ``{"label": ..., "card": {...}}`` (no card_a/card_b —
    a single judgement is NEVER represented by duplicating one artifact
    into both pair slots). match_id stays domain-unique so repeated runs
    never collide, and every artifact is enqueued (no bye). The L6
    invariant holds: exactly ONE pending row per artifact, bound to a
    single human config (``rubric`` overrides the domain's own when the
    caller passed --rubric).
    """
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    enqueued = 0
    try:
        cfg = _human_config_for_rubric(conn, domain_id, rubric)
        last_match = conn.execute(
            "SELECT COALESCE(MAX(match_id), -1) FROM pending_judgement "
            "WHERE domain_id=?",
            (domain_id,),
        ).fetchone()[0]
        slot = int(last_match) + 1
        for item in items:
            payload = json.dumps({
                "label": f"S1-{slot + 1}",
                "card": item,
            })
            conn.execute(
                "INSERT INTO pending_judgement(config_id, tournament_db_path, "
                "match_id, trace_payload, domain_id) "
                "VALUES (?, ?, ?, ?, ?)",
                (cfg["id"], f"domain:{domain_id}", slot, payload, domain_id),
            )
            enqueued += 1
            slot += 1
        conn.commit()
    finally:
        conn.close()
    return enqueued


def _resolve_judgement_kind(spec, judgement_kind: Optional[str],
                            rubric: Optional[str]) -> str:
    """Pick 'pair' or 'single' for this run.

    Explicit --judgement-kind wins. Otherwise consult the (normalized)
    output_definition of the explicit --rubric, or the domain's active
    template: a single-kind template selects the singles path. Missing
    templates fall back to 'pair' (legacy behavior).
    """
    if judgement_kind is not None:
        if judgement_kind not in ("pair", "single"):
            raise ValueError(f"unknown judgement kind: {judgement_kind!r}")
        return judgement_kind
    # Read the template straight from the fabric DB via _db_path() — the
    # judgement module binds its DB path at import time, which goes stale
    # under per-run DATA_TOURNAMENTS_HOME (tests, Phoenix shell-outs). The
    # normalization helper itself is pure and safe to reuse.
    from bin.judgement import normalize_output_definition
    conn = sqlite3.connect(str(_db_path()))
    try:
        row = conn.execute(
            "SELECT output_definition FROM eval_template "
            "WHERE name=? AND is_draft=0 ORDER BY version DESC LIMIT 1",
            (rubric or spec.rubric,),
        ).fetchone()
    except sqlite3.OperationalError:
        return "pair"  # fabric DB not initialized — legacy pair behavior
    finally:
        conn.close()
    if row is None:
        return "pair"
    return normalize_output_definition(json.loads(row[0]))["judgement_kind"]


def _is_systemic(error: Exception) -> bool:
    text = f"{type(error).__name__}: {error}"
    return any(marker in text for marker in _SYSTEMIC_ERROR_MARKERS)


def _work_order_payload(wo, cited_evidence: Optional[list[str]] = None) -> dict:
    """Display/judging payload for a finalized WorkOrder.

    ``title``/``body``/``source_ref`` keep the legacy card shape so the
    existing judge UI and LLM-judge prompt path render work orders without
    changes (body carries the canonical markdown). The full structured
    object rides alongside for richer consumers. ``cited_evidence`` is a
    list of catalog EvidenceRef digests stamped by the PIPELINE (never the
    model) — the judge view resolves them into tier-badged citation chips.
    """
    payload = {
        "kind": "work-order",
        "title": wo.title,
        "body": to_markdown(wo),
        "source_ref": wo.source_ref,
        "work_order": wo.model_dump(),
    }
    if cited_evidence:
        payload["cited_evidence"] = list(cited_evidence)
    return payload


class _EvidenceStamper:
    """Pipeline-side citation stamping for work-order generation.

    For git-backed filesystem corpora: each corpus item becomes a
    TIER1_SYSTEM EvidenceRef pinned to the captured base commit
    (git_local.file_refs), persisted through bin.catalog into the shared
    fabric DB under project ``catalog_project`` (corpus_source override,
    default: the domain name) / source ``corpus``. The returned digests are
    stamped into the payload's ``cited_evidence`` — the model NEVER supplies
    them (WorkOrderDraft has no such field by design).

    Fail-open by design: citation stamping must never break generation.
    Any failure (non-git corpus, catalog unavailable) disables the stamper
    for the rest of the run and logs once.
    """

    def __init__(self, spec, snap):
        self._spec = spec
        self._snap = snap
        self._enabled = snap is not None
        self._source_id: Optional[int] = None
        self._warned = False

    def _disable(self, why: str) -> None:
        if not self._warned:
            print(f"[generate] evidence stamping off: {why}", flush=True)
            self._warned = True
        self._enabled = False

    def _ensure_source(self) -> Optional[int]:
        if self._source_id is not None:
            return self._source_id
        from bin import catalog as _catalog

        project = self._spec.corpus_source.get("catalog_project") or self._spec.name
        _catalog.init()
        try:
            _catalog.get_project(project)
        except LookupError:
            _catalog.create_project(
                name=project,
                description=f"auto-created by generation for domain {self._spec.name}",
            )
        try:
            src = _catalog.get_source(project, "corpus")
        except LookupError:
            _catalog.create_source(
                project=project,
                name="corpus",
                kind="git",
                locator=self._snap.root,
                trust_tier=1,
            )
            src = _catalog.get_source(project, "corpus")
        self._source_id = int(src["id"])
        return self._source_id

    def cite(self, item_path: str) -> list[str]:
        """Return EvidenceRef digests for one corpus item (or [])."""
        if not self._enabled:
            return []
        try:
            from bin import catalog as _catalog
            from bin.landscape.adapters import git_local

            root = Path(self._snap.root).resolve()
            rel = str(Path(item_path).resolve().relative_to(root))
            refs = git_local.file_refs(
                str(root),
                [rel],
                why=f"work-order source for domain {self._spec.name}",
                commit=self._snap.base_commit,
            )
            source_id = self._ensure_source()
            return [
                _catalog.insert_evidence_ref(ref, source_id=source_id)
                for ref in refs
            ]
        except Exception as exc:  # never block generation on citations
            self._disable(f"{type(exc).__name__}: {exc}")
            return []


def run(domain_name: str, *, limit: Optional[int] = None, seed: int = 0,
        artifact: Optional[str] = None, judgement_kind: Optional[str] = None,
        rubric: Optional[str] = None) -> GenerateResult:
    spec = _domains.get_domain(domain_name)
    if limit is None:
        limit = _llm_config.generator_max_items()
    if artifact is None:
        artifact = spec.corpus_source.get("artifact", "card")
    if artifact not in ("card", "work-order"):
        raise ValueError(f"unknown artifact kind: {artifact!r}")
    kind = _resolve_judgement_kind(spec, judgement_kind, rubric)
    cfg = _llm_config.generator_config()
    print(
        f"[generate] domain={spec.name} corpus_kind={spec.corpus_source['kind']} "
        f"artifact={artifact} judgement_kind={kind} item_budget={limit}",
        flush=True,
    )
    # Effective LM bounds up front: a stale in-flight process is instantly
    # distinguishable from a fresh one by this line.
    print(
        f"[generate] config max_tokens={cfg.max_tokens} "
        f"timeout={cfg.timeout:g}s retries={cfg.num_retries}",
        flush=True,
    )

    if getattr(dspy.settings, "lm", None) is None:
        dspy.settings.configure(lm=_build_generation_lm())

    rng = random.Random(seed or hash(spec.name) & 0xFFFFFFFF)

    # System-supplied provenance for work orders, captured once per run so
    # every WorkOrder from this run pins the same base commit. The model
    # identity comes from the ACTIVE LM instance, not the config: when
    # GENERATOR_MODEL/LLM_MODEL are unset the LM builder falls back to the
    # frontier panel default, and recording an empty models list while a
    # real model did the work would be false provenance.
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    active_lm_model = getattr(getattr(dspy.settings, "lm", None), "model", None)
    run_models = [m for m in [active_lm_model or cfg.model or ""] if m]
    repos = []
    if artifact == "work-order" and spec.corpus_source.get("kind") == "filesystem":
        snap = capture_repo_snapshot(spec.corpus_source.get("root", ""))
        if snap is not None:
            repos = [snap]
            dirty = " (dirty working tree)" if snap.dirty else ""
            print(
                f"[generate] repo {snap.remote or snap.root} "
                f"@ {snap.base_commit[:12]}{dirty}",
                flush=True,
            )

    if artifact == "work-order":
        gen = WorkOrderGen(prompt_name=spec.generator_prompt)
        output_field = "work_orders"
        stamper = _EvidenceStamper(spec, repos[0] if repos else None)
    else:
        gen = CardGen(prompt_name=spec.generator_prompt)
        output_field = "cards"
        stamper = None

    payloads: list[dict] = []
    errors = 0
    failures: dict[str, int] = {}
    aborted_reason = ""
    consecutive_systemic = 0
    last_systemic = ""
    seen = 0
    for item in _iter_corpus(spec):
        if seen >= limit:
            print(
                f"[generate] item budget reached ({limit}); run again to continue "
                "or raise GENERATOR_MAX_ITEMS / pass --limit",
                flush=True,
            )
            break
        seen += 1
        print(f"[generate] item {seen}: {item['source_ref']}", flush=True)
        try:
            result = gen(corpus_text=item["text"])
            produced = getattr(result, output_field, None) or []
            consecutive_systemic = 0
            for artifact_item in produced:
                if artifact == "work-order":
                    wo = finalize_work_order(
                        artifact_item,
                        domain=spec.name,
                        created_at=created_at,
                        models=run_models,
                        repos=repos,
                        # The model only sees corpus_text; the authoritative
                        # corpus reference always comes from the loop.
                        source_ref=item["source_ref"],
                    )
                    # Pipeline-stamped citations (model never supplies them):
                    # corpus item -> pinned-commit EvidenceRef -> digest.
                    cited = stamper.cite(item["source_ref"]) if stamper else []
                    payloads.append(_work_order_payload(wo, cited_evidence=cited))
                else:
                    # The model only sees corpus_text, not the filesystem/DB
                    # location. Always attach the authoritative corpus
                    # reference instead of a guessed/hallucinated source_ref.
                    c = artifact_item.model_copy(
                        update={"source_ref": item["source_ref"]}
                    )
                    payloads.append(c.model_dump())
        except CardGenError as e:
            # Classified per-item failure (timeout / parse-error /
            # truncation). Record the class, keep going with the rest of
            # the corpus. A truncated or malformed finding is never
            # repaired into a valid-looking card.
            errors += 1
            consecutive_systemic = 0
            failures[e.failure_class] = failures.get(e.failure_class, 0) + 1
            print(
                f"[generate] item failed failure={e.failure_class} "
                f"on {item.get('source_ref')}: {type(e).__name__}: {e}",
                flush=True, file=sys.stderr,
            )
        except Exception as e:
            errors += 1
            failures["error"] = failures.get("error", 0) + 1
            print(
                f"[generate] item failed failure=error "
                f"on {item.get('source_ref')}: {type(e).__name__}: {e}",
                flush=True, file=sys.stderr,
            )
            # Provider-level failures (bad credentials, unreachable endpoint)
            # fail identically for every remaining item. Trip the breaker
            # instead of emitting one copy of the same error per file.
            if _is_systemic(e):
                consecutive_systemic += 1
                last_systemic = f"{type(e).__name__}: {e}"
                if consecutive_systemic >= _SYSTEMIC_ABORT_AFTER:
                    aborted_reason = (
                        f"provider unavailable after {consecutive_systemic} "
                        f"consecutive failures: {last_systemic}"
                    )
                    print(
                        f"[generate] ABORTED: {aborted_reason}\n"
                        "[generate] check OPENROUTER_API_KEY / LLM_BASE_URL "
                        "(repo .env), then rerun",
                        flush=True, file=sys.stderr,
                    )
                    break
            else:
                consecutive_systemic = 0

    breakdown = (
        " [" + ", ".join(f"{k}={v}" for k, v in sorted(failures.items())) + "]"
        if failures else ""
    )
    noun = "work orders" if artifact == "work-order" else "cards"
    print(f"[generate] generated {len(payloads)} {noun} from {seen} corpus items "
          f"({errors} errors{breakdown})", flush=True)

    pairs = 0
    singles = 0
    if kind == "single":
        singles = enqueue_singles(spec.id, payloads, rubric=rubric)
        print(f"[generate] enqueued {singles} single(s) for the judge axis",
              flush=True)
    else:
        pairs = _enqueue_pairs(spec.id, payloads, rng)
        print(f"[generate] enqueued {pairs} pair(s) for the judge wheel",
              flush=True)

    return GenerateResult(
        cards_generated=len(payloads),
        pairs_enqueued=pairs,
        singles_enqueued=singles,
        errors=errors,
        failures=failures,
        aborted_reason=aborted_reason,
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--domain", required=True)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument(
        "--artifact",
        choices=("card", "work-order"),
        default=None,
        help=(
            "Artifact kind to generate. Default: the domain's "
            "corpus_source['artifact'], falling back to 'card'."
        ),
    )
    p.add_argument(
        "--judgement-kind",
        choices=("pair", "single"),
        default=None,
        help=(
            "How generated artifacts are enqueued: 'pair' brackets them for "
            "the comparison wheel, 'single' writes one pending per artifact "
            "for absolute judgement. Default: the judgement_kind of the "
            "domain's active template (or --rubric), falling back to 'pair'."
        ),
    )
    p.add_argument(
        "--rubric",
        default=None,
        help=(
            "Explicit eval_template name to bind pendings to (overrides the "
            "domain's own rubric for config matching and kind detection)."
        ),
    )
    args = p.parse_args()
    result = run(args.domain, limit=args.limit, seed=args.seed,
                 artifact=args.artifact, judgement_kind=args.judgement_kind,
                 rubric=args.rubric)
    # A systemic abort (provider down/unauthenticated) is a failed run, not
    # a small one — exit nonzero so the UI reports failure instead of
    # "Generation finished".
    if result.aborted_reason:
        sys.exit(2)


if __name__ == "__main__":
    main()

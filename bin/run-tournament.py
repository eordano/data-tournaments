#!/usr/bin/env python3
"""
run-tournament <config.json>  — Langfuse-native tournament orchestrator.

Drives a Swiss tournament where each match is one execution of a sandboxed
agent (Hermes via MCP) that compares two inputs and submits a synthesis
markdown plus a winner -- or no winner at all, which is a draw.

Architecture
------------
Each round becomes one Langfuse `run_experiment(...)` invocation:
  - Dataset name:      "tournament:<config name>"  (created on first run)
  - Run name:          "rN"  (round number)
  - DatasetItems:      one per match, input = {file_a, file_b, label, …}
  - task=run_match:    shells out to bin/hermes-harness.sh, captures
                       (synthesis_md, winner_id) via per-match tempfiles
  - Evaluators (item-level):
      * required_sections_present  (BOOLEAN) — schema check
      * word_count_within_limit    (BOOLEAN) — config.max_words
      * synthesis_quality_judge    (NUMERIC 0–1) — LLM-as-judge

Pairing
-------
Pairing is bin/swiss.py — the one answer in this tree to "who plays whom":
football points (win 3, draw 1, loss 0), a seeded random first round, later
rounds sorted by matches played then points, never a repeated pair, and
ceil(log2 N) rounds over a pool that never shrinks. Nothing is eliminated, so
the run ends with a standings table rather than one conclusion. An odd pool
gives one item a bye each round: no agent runs and no points are scored.

Draws
-----
Each match produces a synthesis and a winner_id of 1 or 2. Anything else — 0,
null, a missing winner file — is recorded as a draw, worth a point to each
side. A comparison the agent would not call is never settled by inventing a
winner for it.

State
-----
SQLite tracks the rounds, the matches, their pair keys and their outcomes, so
a resumed run rebuilds the standings from stored results and continues at the
round it stopped in. config["advance"] no longer decides what the next round
consumes — the pool is the configured inputs from first round to last — and
survives only as the choice of what the `conclusion` column keeps.

Config schema
-------------
  {
    "name":              "tournament",                // label + dataset suffix
    "inputs":            ["path1", "path2", ...],     // round-1 files
    "parallelism":       0,                           // 0 → 1 (Hermes is serial per-slot)
    "seed":              20260101,
    "db_path":           "/tmp/<name>.db",
    "workdir":           "/tmp/<name>",
    "match_prompt":      "<template>",
    "required_sections": ["## ..."],
    "max_words":         500,
    "advance":           "synthesis",                 // "synthesis" | "winner"
    "judge": {
      "model":           "moonshotai/kimi-k3",
      "base_url":        "https://openrouter.ai/api/v1",
      "api_key_env":     "OPENROUTER_API_KEY",
      "temperature":     0.0
    },
    "langfuse": {
      "host":            "https://cloud.langfuse.com",
      "tags":            ["tournament", "actions-style"]
    }
  }

The match_prompt template sees these placeholders:
  {LABEL}      e.g. "R2-3"
  {INPUTS}     numbered list "1. /path/a.ts\n2. /path/b.ts"
  {N_INPUTS}   integer count
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Optional

try:
    import judgement  # noqa: F401  (side-effect: loads .env into os.environ)
except Exception:
    pass

try:
    from bin import swiss
except ImportError:
    import swiss

BIN_DIR = Path(os.environ.get("DATA_TOURNAMENTS_BIN") or Path(__file__).resolve().parent)
HARNESS = BIN_DIR / "hermes-harness.sh"
DEFAULT_REQUIRED = [
    "## Shared patterns", "## Divergent patterns", "## Naming & exports",
    "## Validation", "## Error handling", "## Auth & session",
    "## Return shapes", "## Database access", "## Other conventions",
    "## Guideline candidates",
]
DEFAULT_JUDGE = {
    "model": "moonshotai/kimi-k3",
    "base_url": "https://openrouter.ai/api/v1",
    "api_key_env": "OPENROUTER_API_KEY",
    "temperature": 0.0,
}

def log(msg: str) -> None:
    print(msg, flush=True)

def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text())
    cfg.setdefault("name", path.stem)
    cfg.setdefault("seed", 20260101)
    cfg.setdefault("db_path", f"/tmp/{cfg['name']}.db")
    cfg.setdefault("workdir", f"/tmp/{cfg['name']}")
    cfg.setdefault("parallelism", 0)
    cfg.setdefault("required_sections", DEFAULT_REQUIRED)
    cfg.setdefault("max_words", 500)
    cfg.setdefault("advance", "synthesis")
    judge = dict(DEFAULT_JUDGE)
    judge.update(cfg.get("judge") or {})
    cfg["judge"] = judge
    cfg.setdefault("langfuse", {})
    if cfg["advance"] not in ("synthesis", "winner"):
        raise SystemExit(f"advance must be 'synthesis' or 'winner', got {cfg['advance']!r}")
    if "inputs" not in cfg or "match_prompt" not in cfg:
        raise SystemExit("config must include `inputs` and `match_prompt`")
    return cfg

def init_db(path: Path) -> sqlite3.Connection:
    fresh = not path.exists()
    db = sqlite3.connect(str(path))
    if fresh:
        db.executescript("""
        CREATE TABLE matches (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          round INTEGER NOT NULL,
          slot INTEGER NOT NULL,
          input_a TEXT NOT NULL,
          input_b TEXT,
          is_bye INTEGER NOT NULL DEFAULT 0,
          pair_key TEXT,
          outcome TEXT,
          conclusion TEXT,                         -- what the config kept
          synthesis TEXT,                          -- always the agent's synthesis
          winner_id INTEGER,                       -- 1 or 2 (NULL on a draw)
          winner_reasoning TEXT,
          trace_id TEXT,                           -- Langfuse trace id (32 hex)
          created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX idx_round ON matches(round, slot);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        db.commit()
    else:
        cols = {r[1] for r in db.execute("PRAGMA table_info(matches)")}
        for col, ddl in [
            ("synthesis", "ALTER TABLE matches ADD COLUMN synthesis TEXT"),
            ("winner_id", "ALTER TABLE matches ADD COLUMN winner_id INTEGER"),
            ("winner_reasoning", "ALTER TABLE matches ADD COLUMN winner_reasoning TEXT"),
            ("trace_id", "ALTER TABLE matches ADD COLUMN trace_id TEXT"),
            ("pair_key", "ALTER TABLE matches ADD COLUMN pair_key TEXT"),
            ("outcome", "ALTER TABLE matches ADD COLUMN outcome TEXT"),
        ]:
            if col not in cols:
                db.execute(ddl)
        db.commit()
    return db

VERDICT_FOR_OUTCOME = swiss.CANONICAL_VERDICT_FOR_OUTCOME

THE_ORCHESTRATOR_READS_THE_ENGINES_VOCABULARY_IT_NEVER_RESTATES_IT = (
    "this used to be a hand-written map of outcome to verdict. It kept "
    "naming a vocabulary the engine had retired, so every replayed match "
    "raised out of swiss.record and the shipped orchestrator could not "
    "finish a round. Reading the engine's own map means a rubric rename "
    "cannot leave this file behind."
)
assert set(VERDICT_FOR_OUTCOME) >= {
    swiss.OUTCOME_A, swiss.OUTCOME_B, swiss.OUTCOME_DRAW
}, THE_ORCHESTRATOR_READS_THE_ENGINES_VOCABULARY_IT_NEVER_RESTATES_IT

def pool_item(path: str) -> swiss.Item:
    """One entrant. The pair key hashes the file's content, so two inputs that
    are the same text are the same entrant however they are named."""
    try:
        content = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        content = path
    return swiss.Item(id=path, content=content)

def build_pool(cfg: dict, db: sqlite3.Connection) -> swiss.Pool:
    """Rebuild the standings from what SQLite already holds.

    The pool is the configured inputs, first round to last -- it does not
    shrink as rounds are played. Replaying the stored outcomes in round order
    is what lets a resumed run continue at the right round with the right
    points.
    """
    pool = swiss.new_pool(
        (pool_item(path) for path in cfg["inputs"]),
        rubric_id=str(cfg["name"]),
        rubric_version=int(cfg.get("rubric_version", 1)),
        seed=int(cfg["seed"]),
    )
    rows = db.execute(
        "SELECT round, input_a, input_b, is_bye, outcome FROM matches "
        "ORDER BY round, slot"
    ).fetchall()
    for round_n, input_a, input_b, is_bye, outcome in rows:
        if is_bye:
            swiss.no_result(pool, round=round_n, item_id=input_a,
                            cause=swiss.NO_RESULT_CAUSE_BYE)
            continue
        if outcome not in VERDICT_FOR_OUTCOME:
            continue
        swiss.record(pool, round=round_n, item_a=input_a, item_b=input_b,
                     verdict=VERDICT_FOR_OUTCOME[outcome])
    return pool

def insert_round(db: sqlite3.Connection, drawn: swiss.Round) -> None:
    """Persist one drawn round.

    Matches carry their pair key and wait for an outcome of 'a', 'b' or
    'draw'. A bye is a row with no opponent, an outcome of 'bye' and no agent
    run: the item sits out, scoring nothing.
    """
    for match in drawn.matches:
        db.execute(
            "INSERT INTO matches(round,slot,input_a,input_b,is_bye,pair_key) "
            "VALUES(?,?,?,?,0,?)",
            (drawn.number, match.slot, match.item_a, match.item_b,
             match.pair_key),
        )
    for offset, item_id in enumerate(drawn.byes):
        db.execute(
            "INSERT INTO matches(round,slot,input_a,input_b,is_bye,outcome,"
            "conclusion) VALUES(?,?,?,NULL,1,'bye','bye')",
            (drawn.number, len(drawn.matches) + offset, item_id),
        )
    db.commit()

def outcome_for_winner(winner_id: Optional[int]) -> str:
    """The match outcome: 'a' when winner_id is 1, 'b' when it is 2, and
    'draw' for everything else -- never a default win for the first input."""
    if winner_id == 1:
        return swiss.OUTCOME_A
    if winner_id == 2:
        return swiss.OUTCOME_B
    return swiss.OUTCOME_DRAW

def build_prompt(template: str, label: str, inputs: list[str]) -> str:
    bullets = "\n".join(f"  {i+1}. {p}" for i, p in enumerate(inputs))
    return (template
            .replace("{LABEL}", label)
            .replace("{INPUTS}", bullets)
            .replace("{N_INPUTS}", str(len(inputs))))

def resolve_match_inputs(db_path: str, workdir: Path, row: tuple) -> list[str]:
    """The files a match compares.

    Always the pool's own inputs: under Swiss no round consumes the previous
    round's output, so there is nothing to dereference.
    """
    _mid, _slot, input_a, input_b, _is_bye = row[:5]
    return [ref for ref in (input_a, input_b) if ref]

def run_match_subprocess(cfg: dict, db_path: str, round_n: int, row: tuple,
                         workdir: Path, trace_id: str, parent_obs_id: str) -> dict:
    """Synchronous: invokes the harness, returns the recorded artifacts.

    Returns a dict:
      {
        "match_id":    int,
        "slot":        int,
        "is_bye":      bool,
        "synthesis":   str,            # markdown body
        "winner_id":   Optional[int],  # None when the agent declined to pick
        "outcome":     str,            # 'a' | 'b' | 'draw'
        "winner_reasoning": str,
        "exit_code":   int,
        "stderr_tail": str,
      }

    Byes never reach here: an item sitting a round out runs no agent.
    """
    mid, slot, _input_a, _input_b, _is_bye = row[:5]
    outdir = workdir / f"r{round_n}"
    outdir.mkdir(parents=True, exist_ok=True)
    outpath = outdir / f"m{slot + 1}.md"
    errpath = outdir / f"m{slot + 1}.err"

    files = resolve_match_inputs(db_path, workdir, row)

    label = f"R{round_n}-{slot + 1}"
    prompt_override = build_prompt(cfg["match_prompt"], label, files)

    fd_out, outfile_ipc = tempfile.mkstemp(prefix="tour-syn-", suffix=".md")
    os.close(fd_out)
    Path(outfile_ipc).unlink(missing_ok=True)
    fd_w, winner_file = tempfile.mkstemp(prefix="tour-winner-", suffix=".json")
    os.close(fd_w)
    Path(winner_file).unlink(missing_ok=True)

    env = os.environ.copy()
    env["TOURNAMENT_PROMPT_OVERRIDE"] = prompt_override
    env["TOURNAMENT_REQUIRED_SECTIONS"] = "\x1f".join(cfg["required_sections"])
    env["TOURNAMENT_TRACE_ID"] = trace_id
    if parent_obs_id:
        env["TOURNAMENT_PARENT_OBSERVATION_ID"] = parent_obs_id

    par = cfg["parallelism"] or 1
    env["HARNESS_SLOT"] = str(slot % max(par, 1))
    env["HARNESS_OUTFILE"] = outfile_ipc
    env["HARNESS_WINNER_FILE"] = winner_file

    cmd = [str(HARNESS), label, *files]

    with open(outpath, "w") as out_f, open(errpath, "w") as err_f:
        proc = subprocess.run(
            cmd, stdout=out_f, stderr=err_f, env=env, timeout=600,
        )
    tail = errpath.read_text()[-400:] if errpath.exists() else ""

    synthesis = ""
    winner_id: Optional[int] = None
    winner_reasoning = ""
    if Path(outfile_ipc).exists():
        synthesis = Path(outfile_ipc).read_text()
    elif outpath.exists():
        synthesis = outpath.read_text()
    if Path(winner_file).exists():
        try:
            wd = json.loads(Path(winner_file).read_text())
            winner_id = int(wd.get("winner_id")) if wd.get("winner_id") is not None else None
            winner_reasoning = str(wd.get("reasoning") or "")
        except Exception as e:
            tail += f"\n[winner_file parse error: {e!r}]"

    Path(outfile_ipc).unlink(missing_ok=True)
    Path(winner_file).unlink(missing_ok=True)

    return {
        "match_id": mid, "slot": slot, "is_bye": False,
        "synthesis": synthesis, "winner_id": winner_id,
        "outcome": outcome_for_winner(winner_id),
        "winner_reasoning": winner_reasoning,
        "exit_code": proc.returncode, "stderr_tail": tail,
    }

def make_required_sections_evaluator(required: list[str]):
    from langfuse import Evaluation

    def evaluator(*, input: Any, output: Any, expected_output: Any = None,
                  metadata: Optional[dict] = None) -> Evaluation:  # type: ignore[valid-type]
        text = (output or {}).get("synthesis") if isinstance(output, dict) else str(output or "")
        text_l = (text or "").lower()
        missing = [s for s in required if s.lower() not in text_l]
        return Evaluation(
            name="required_sections_present",
            value=len(missing) == 0,
            comment=("ok" if not missing else f"missing: {', '.join(missing)}"),
            data_type="BOOLEAN",
            metadata={"missing_count": len(missing), "required_total": len(required)},
        )

    return evaluator

def make_word_count_evaluator(max_words: int):
    from langfuse import Evaluation

    def evaluator(*, input: Any, output: Any, expected_output: Any = None,
                  metadata: Optional[dict] = None) -> Evaluation:  # type: ignore[valid-type]
        text = (output or {}).get("synthesis") if isinstance(output, dict) else str(output or "")
        n = len(str(text or "").split())
        return Evaluation(
            name="word_count_within_limit",
            value=n <= max_words,
            comment=f"{n}/{max_words} words",
            data_type="BOOLEAN",
            metadata={"word_count": n, "max_words": max_words},
        )

    return evaluator

def make_judge_evaluator(judge_cfg: dict, criteria: str):
    """LLM-as-judge: scores synthesis quality 0.0–1.0.

    Uses any OpenAI-compatible endpoint (httpx, no extra deps). Prompt
    asks for structured JSON; we parse and clamp the score.
    """
    import httpx

    api_key = os.environ.get(judge_cfg.get("api_key_env") or "", "") or "none"
    base_url = judge_cfg["base_url"].rstrip("/")
    model = judge_cfg["model"]
    temperature = float(judge_cfg.get("temperature", 0.0))

    system = (
        "You are an impartial judge scoring a code-style tournament match. "
        "Read the inputs (file paths and short labels), the agent's chosen winner, "
        "and the agent's synthesis markdown. Score the SYNTHESIS quality on 0.0–1.0:\n"
        " - 0.0–0.3: incoherent, off-task, or contradicts the chosen winner\n"
        " - 0.4–0.6: covers the required sections but shallow or wrong in places\n"
        " - 0.7–0.9: solid, specific, prescriptive guidelines, defensible winner\n"
        " - 1.0:    excellent — concrete, accurate, and decisive\n\n"
        "Respond with strict JSON only: "
        '{"score": <float 0..1>, "rationale": "<<=200 chars>>"}'
    )

    def score_synthesis(*, input: Any, output: Any, expected_output: Any = None,
                       metadata: Optional[dict] = None):
        from langfuse import Evaluation

        out = output if isinstance(output, dict) else {"synthesis": str(output or "")}
        synthesis = (out.get("synthesis") or "")[:6000]
        winner_id = out.get("winner_id")
        winner_reasoning = out.get("winner_reasoning") or ""
        in_obj = input if isinstance(input, dict) else {"raw": str(input)}

        user = (
            f"### Tournament criteria\n{criteria}\n\n"
            f"### Match inputs\n{json.dumps(in_obj, indent=2)[:1500]}\n\n"
            f"### Chosen winner\ninput_{winner_id}\n"
            f"### Winner reasoning\n{winner_reasoning[:600]}\n\n"
            f"### Synthesis markdown\n{synthesis}\n"
        )

        try:
            with httpx.Client(timeout=60) as client:
                r = client.post(
                    f"{base_url}/chat/completions",
                    json={
                        "model": model,
                        "temperature": temperature,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": user},
                        ],
                        "response_format": {"type": "json_object"},
                    },
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                r.raise_for_status()
                content = r.json()["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            score = max(0.0, min(1.0, float(parsed.get("score", 0.0))))
            rationale = str(parsed.get("rationale", ""))[:200]
        except Exception as e:
            return Evaluation(
                name="synthesis_quality_judge",
                value=0.0,
                comment=f"judge error: {type(e).__name__}: {e}",
                data_type="NUMERIC",
                metadata={"error": True},
            )

        return Evaluation(
            name="synthesis_quality_judge",
            value=score,
            comment=rationale,
            data_type="NUMERIC",
            metadata={"model": model, "winner_id": winner_id},
        )

    return score_synthesis

def ensure_dataset(lf: Any, dataset_name: str) -> None:
    try:
        lf.get_dataset(dataset_name)
    except Exception:
        lf.create_dataset(name=dataset_name, description="data-tournaments bracket")

def upsert_dataset_items_for_round(lf: Any, dataset_name: str,
                                   db_path: str, workdir: Path,
                                   round_n: int, rows: list[tuple]) -> dict[int, str]:
    """Create one DatasetItem per match; return {match_id: dataset_item_id}.

    `input` is a small JSON dict describing the match — file paths and the
    match label. Files are NOT inlined; the agent reads them via read_file.
    """
    item_id_for_match: dict[int, str] = {}
    for row in rows:
        mid, slot, _input_a, _input_b, is_bye = row[:5]
        files = resolve_match_inputs(db_path, workdir, row)
        item_input = {
            "label": f"R{round_n}-{slot + 1}",
            "round": round_n,
            "slot": slot,
            "files": files,
            "n_inputs": len(files),
            "is_bye": bool(is_bye),
        }
        try:
            created = lf.create_dataset_item(
                dataset_name=dataset_name,
                input=item_input,
                metadata={
                    "round": round_n, "slot": slot, "match_id": mid,
                    "kind": "bye" if is_bye else "match",
                },
            )
            item_id_for_match[mid] = getattr(created, "id", None) or ""
        except Exception as e:
            log(f"  [warn] dataset_item upsert failed for match {mid}: {e!r}")
            item_id_for_match[mid] = ""
    return item_id_for_match

def run_round(cfg: dict, db: sqlite3.Connection, round_n: int, workdir: Path,
              lf: Any, dataset_name: str) -> None:
    rows = db.execute(
        "SELECT id,slot,input_a,input_b,is_bye FROM matches "
        "WHERE round=? AND is_bye=0 ORDER BY slot",
        (round_n,),
    ).fetchall()
    byes = db.execute(
        "SELECT input_a FROM matches WHERE round=? AND is_bye=1", (round_n,)
    ).fetchall()
    log(f"\n── Round {round_n}: {len(rows)} match{'es' if len(rows)!=1 else ''} "
        f"({len(byes)} bye) ──")

    par = cfg["parallelism"] or 1
    par = max(1, min(par, len(rows)))
    log(f"  harness={HARNESS.name} parallelism={par}  advance={cfg['advance']}")

    item_id_for_match = (upsert_dataset_items_for_round(
        lf, dataset_name, cfg["db_path"], workdir, round_n, rows,
    ) if lf is not None else {mid: "" for (mid, *_rest) in rows})

    data: list[dict] = []
    for row in rows:
        mid, slot, _a, _b, is_bye = row[:5]
        data.append({
            "input": {
                "match_id": mid,
                "round": round_n,
                "slot": slot,
                "files": resolve_match_inputs(cfg["db_path"], workdir, row),
                "is_bye": bool(is_bye),
                "label": f"R{round_n}-{slot + 1}",
            },
            "metadata": {
                "match_id": mid, "round": round_n, "slot": slot,
                "dataset_item_id": item_id_for_match.get(mid, ""),
            },
        })

    def task(*, item: dict, **_kwargs) -> dict:
        in_obj = item["input"]
        mid = in_obj["match_id"]
        slot = in_obj["slot"]
        is_bye = in_obj["is_bye"]
        row = next(r for r in rows if r[0] == mid)

        trace_id = ""
        parent_obs = ""
        if lf is not None:
            try:
                trace_id = lf.get_current_trace_id() or ""
                parent_obs = lf.get_current_observation_id() or ""
            except Exception:
                pass

        result = run_match_subprocess(
            cfg, cfg["db_path"], round_n, row, workdir,
            trace_id=trace_id, parent_obs_id=parent_obs,
        )

        if trace_id:
            db_local = sqlite3.connect(cfg["db_path"])
            db_local.execute("UPDATE matches SET trace_id=? WHERE id=?", (trace_id, mid))
            db_local.commit()

        if result["exit_code"] != 0 and not is_bye:
            raise RuntimeError(
                f"match {mid} (R{round_n}-{slot+1}) failed exit={result['exit_code']}: "
                f"{result['stderr_tail'][:200]}"
            )
        return result

    evaluators = []
    evaluators.append(make_required_sections_evaluator(cfg["required_sections"]))
    evaluators.append(make_word_count_evaluator(int(cfg["max_words"])))

    judge_cfg = cfg["judge"]
    if judge_cfg and judge_cfg.get("model"):
        criteria_summary = (
            "Tournament criteria from match_prompt: judge whether the synthesis "
            "is concrete, prescriptive, and defends the chosen winner. Penalize "
            "vague platitudes; reward specific, testable guidelines."
        )
        evaluators.append(make_judge_evaluator(judge_cfg, criteria_summary))

    if lf is not None:
        run_name = f"r{round_n}"
        log(f"  langfuse: dataset={dataset_name!r} run={run_name!r}")
        result = lf.run_experiment(
            name=cfg["name"],
            run_name=run_name,
            description=f"data-tournaments round {round_n}",
            data=data,
            task=task,
            evaluators=evaluators,
            max_concurrency=par,
            metadata={
                "round": round_n,
                "advance": cfg["advance"],
                "harness": HARNESS.name,
            },
        )
        item_results = list(getattr(result, "item_results", []) or [])
    else:
        log("  langfuse: disabled (no LANGFUSE_PUBLIC_KEY/SECRET_KEY)")
        item_results = []

        class _StubResult:
            def __init__(self, output, metadata):
                self.output = output
                self.metadata = metadata
                self.evaluations = []

        def _run_one(d):
            try:
                out = task(item=d)
            except Exception as e:
                out = {"error": str(e)}
            return _StubResult(out, d.get("metadata", {}))

        with ThreadPoolExecutor(max_workers=par) as ex:
            for r in ex.map(_run_one, data):
                item_results.append(r)

    advance_mode = cfg["advance"]
    failures: list[str] = []
    for ir in item_results:
        out = getattr(ir, "output", None) or {}
        if isinstance(out, dict) and out.get("error"):
            failures.append(out["error"])
            continue
        if not isinstance(out, dict):
            continue
        mid = out.get("match_id")
        if mid is None:
            continue
        synthesis = out.get("synthesis") or ""
        winner_id = out.get("winner_id")
        outcome = out.get("outcome") or outcome_for_winner(winner_id)
        winner_reasoning = out.get("winner_reasoning") or ""
        is_bye = bool(out.get("is_bye"))

        advancing: str = synthesis
        if advance_mode == "winner" and not is_bye and winner_id in (1, 2):
            row = next(r for r in rows if r[0] == mid)
            files = resolve_match_inputs(cfg["db_path"], workdir, row)
            chosen = files[winner_id - 1] if winner_id - 1 < len(files) else None
            if chosen and Path(chosen).exists():
                advancing = Path(chosen).read_text(encoding="utf-8", errors="replace")
            else:
                advancing = synthesis

        db.execute(
            "UPDATE matches SET conclusion=?, synthesis=?, winner_id=?, "
            "outcome=?, winner_reasoning=? WHERE id=?",
            (advancing, synthesis, winner_id, outcome, winner_reasoning, mid),
        )
    db.commit()

    try:
        import judgement
        judgement.init_db()
        rated_ids = [
            mid for (mid, *_rest) in db.execute(
                "SELECT id FROM matches WHERE round=? AND conclusion IS NOT NULL "
                "AND is_bye=0",
                (round_n,),
            ).fetchall()
        ]
        enqueued = 0
        for mid in rated_ids:
            enqueued += len(judgement.enqueue_for_match(
                tournament_db_path=cfg["db_path"],
                match_id=mid,
            ))
        if enqueued:
            log(f"  judgement: enqueued {enqueued} pending row(s)")
            try:
                res = judgement.drain_llm_queue(limit=enqueued)
                if res["ok"] or res["error"]:
                    log(f"  judgement.llm: {res['ok']} ok, "
                        f"{res['error']} error, {res['skipped']} skipped")
            except Exception as e:
                log(f"  judgement.llm: drain failed ({type(e).__name__}: {e})")
    except Exception as e:
        log(f"  judgement: hook failed ({type(e).__name__}: {e}); continuing")

    incomplete = db.execute(
        "SELECT id, slot FROM matches WHERE round=? AND (conclusion IS NULL OR conclusion='') "
        "AND is_bye=0",
        (round_n,),
    ).fetchall()
    if incomplete or failures:
        for f in failures:
            log(f"  fail: {f[:300]}")
        for mid, slot in incomplete:
            log(f"  incomplete: match {mid} (slot {slot})")
        raise SystemExit(f"round {round_n} had {len(incomplete) + len(failures)} failure(s); aborting")

def played_rounds(db: sqlite3.Connection) -> int:
    return db.execute(
        "SELECT COUNT(DISTINCT round) FROM matches WHERE outcome IS NOT NULL"
    ).fetchone()[0]

def resume_round(db: sqlite3.Connection) -> int:
    """The round to run next: the lowest one still holding an unplayed match,
    or the one after the last round on record."""
    unfinished = db.execute(
        "SELECT MIN(round) FROM matches WHERE is_bye=0 AND outcome IS NULL"
    ).fetchone()[0]
    if unfinished is not None:
        return int(unfinished)
    last = db.execute("SELECT MAX(round) FROM matches").fetchone()[0]
    return 1 if last is None else int(last) + 1

def maybe_init_langfuse(cfg: dict):
    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return None
    try:
        from langfuse import Langfuse
    except ImportError:
        log("warn: langfuse not installed — running without telemetry")
        return None
    host = (cfg.get("langfuse") or {}).get("host") or os.environ.get(
        "LANGFUSE_HOST", "https://cloud.langfuse.com"
    )
    return Langfuse(host=host)

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("config", type=Path)
    ap.add_argument("--fresh", action="store_true", help="delete existing DB first")
    args = ap.parse_args()

    cfg = load_config(args.config)
    db_path = Path(cfg["db_path"])
    workdir = Path(cfg["workdir"])

    if args.fresh and db_path.exists():
        db_path.unlink()
    workdir.mkdir(parents=True, exist_ok=True)

    db = init_db(db_path)
    lf = maybe_init_langfuse(cfg)
    dataset_name = f"tournament:{cfg['name']}"
    if lf is not None:
        try:
            ensure_dataset(lf, dataset_name)
        except Exception as e:
            log(f"warn: ensure_dataset failed: {e!r}")

    total_rounds = swiss.rounds_total(build_pool(cfg, db))
    start_round = resume_round(db)
    if start_round > 1:
        log(f"resuming at round {start_round} of {total_rounds}")

    for round_n in range(start_round, total_rounds + 1):
        if not db.execute(
            "SELECT 1 FROM matches WHERE round=?", (round_n,)
        ).fetchone():
            pool = build_pool(cfg, db)
            drawn = swiss.pair_round(pool, round_n)
            if not drawn.matches:
                log(f"round {round_n} has no legal pairing left; stopping early")
                break
            log(f"═══ {cfg['name']}: round {round_n}/{total_rounds} — "
                f"{len(drawn.matches)} match(es), {len(drawn.byes)} bye ═══")
            insert_round(db, drawn)
        run_round(cfg, db, round_n, workdir, lf, dataset_name)

    pool = build_pool(cfg, db)
    log("\n" + "─" * 72)
    log(f"STANDINGS after {played_rounds(db)} of {total_rounds} round(s)")
    log("─" * 72)
    print(swiss.format_standings(pool))

    if lf is not None:
        try:
            lf.flush()
        except Exception:
            pass

if __name__ == "__main__":
    main()

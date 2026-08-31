"""Enact a Claude Code workflow run as a sweep campaign.

A multi-agent review workflow (the find -> adversarially-verify shape) is
structurally a sweep: finder agents produce candidate findings, verifier
agents with the burden of refutation are lenses, one verification pass is
a round, and the round's computed outcome is the convergence verdict.
This module replays a finished workflow's on-disk record — the
``journal.jsonl`` (per-agent results) plus the ``agent-*.jsonl``
transcripts (per-agent prompts) under
``~/.claude/projects/<proj>/<session>/subagents/workflows/<run>/`` —
into the campaign ledger, so past agent runs become first-class sweep
history: findings with frozen evidence, per-lens verdicts, a closed round
with honest convergence.

Classification is by shape, not by trust in labels: a result carrying a
``findings`` list is a finder; an agent whose prompt names a
``FINDING [Pn] <title>`` and a ``Lens: <name>`` and whose result carries
a ``verdict`` is a verifier. Verdict mapping: CONFIRMED -> CONFIRM,
REFUTED -> REFUTE; PLAUSIBLE votes are recorded in the report but not as
verdict rows (an unsettled vote must not sway convergence).

CLI:
  enact_workflow.py --dir <workflow run dir> --project P --campaign NAME
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bin import campaigns, catalog  # noqa: E402
from bin.landscape.evidence import EvidenceRef, SourceType, TrustTier  # noqa: E402

_FINDING_RE = re.compile(r"FINDING \[(P\d)[^\]]*\]\s+(.+)")
_LENS_RE = re.compile(r"Lens: ([a-z-]+)")

_VERDICT_MAP = {"CONFIRMED": "CONFIRM", "REFUTED": "REFUTE"}


class EnactError(RuntimeError):
    pass


def _first_prompt(transcript: Path) -> str:
    for line in transcript.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        msg = row.get("message") or {}
        if row.get("type") == "user" and isinstance(msg.get("content"), str):
            return msg["content"]
    return ""


def parse_run(run_dir: str) -> dict[str, Any]:
    """Read one workflow run directory into {finders, verifiers, other}."""
    root = Path(run_dir)
    journal = root / "journal.jsonl"
    if not journal.exists():
        raise EnactError(f"no journal.jsonl under {root}")
    results: dict[str, Any] = {}
    for line in journal.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("type") == "result":
            results[row["agentId"]] = row.get("result")

    finders, verifiers, other = [], [], []
    for agent_id, result in results.items():
        transcript = root / f"agent-{agent_id}.jsonl"
        prompt = _first_prompt(transcript) if transcript.exists() else ""
        finding_m = _FINDING_RE.search(prompt)
        lens_m = _LENS_RE.search(prompt)
        if isinstance(result, dict) and isinstance(result.get("findings"), list):
            finders.append({"agent_id": agent_id, "result": result})
        elif finding_m and lens_m and isinstance(result, dict) and "verdict" in result:
            verifiers.append(
                {
                    "agent_id": agent_id,
                    "severity": finding_m.group(1),
                    "finding_title": finding_m.group(2).strip(),
                    "lens": _short_lens(lens_m.group(1)),
                    "verdict": result.get("verdict", ""),
                    "reasoning": result.get("reasoning", ""),
                }
            )
        else:
            other.append({"agent_id": agent_id, "result": result})
    return {"finders": finders, "verifiers": verifiers, "other": other, "run": root.name}


def _short_lens(lens: str) -> str:
    return "defender" if lens.startswith("read-the-code") else lens


def _slug(title: str) -> str:
    words = re.findall(r"[a-z0-9]+", title.lower())[:5]
    digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:8]
    return "-".join(words + [digest])


def enact(
    run_dir: str,
    *,
    project: str,
    campaign: str,
    kind: str = "bugsweep",
) -> dict[str, Any]:
    """Replay a parsed run into a new campaign. Only findings that were
    verified get lens verdicts; every finder result is frozen as evidence
    on the findings it produced. Returns a summary dict."""
    run = parse_run(run_dir)
    verified_titles = {v["finding_title"] for v in run["verifiers"]}
    lens_names = sorted({v["lens"] for v in run["verifiers"]})
    if not lens_names:
        raise EnactError(
            "run has no verifier agents (no 'FINDING [..]' + 'Lens:' prompts); "
            "nothing to enact as a review round"
        )

    spec = {
        "kind": kind,
        "corpus": [],
        "intake": {"max_candidates": max(len(verified_titles), 1)},
        "panel": {
            "lenses": [
                {"name": name, "prompt_ref": f"lens:{name}", "burden": "refute"}
                for name in lens_names
            ],
            "quorum": "all_lenses",
        },
        "rounds": {"max": 1, "batching": "required"},
        "validation": {"mode": "rubric_only"},
        "publish": {"gate": "human", "granularity": "report-only"},
    }
    campaigns.create_campaign(
        project=project,
        name=campaign,
        kind=kind,
        objective=f"enacted from workflow run {run['run']}",
        spec=spec,
    )

    try:
        src = catalog.get_source(project, f"workflow:{run['run']}")
    except LookupError:
        catalog.create_source(
            project=project,
            name=f"workflow:{run['run']}",
            kind="workflow-journal",
            locator=str(run_dir),
            trust_tier=2,
        )
        src = catalog.get_source(project, f"workflow:{run['run']}")

    title_meta: dict[str, dict] = {}
    evidence_of: dict[str, list[str]] = {}
    for finder in run["finders"]:
        ref = EvidenceRef(
            source_type=SourceType.DOC,
            canonical_uri=f"workflow:{run['run']}#{finder['agent_id']}",
            revision=finder["agent_id"][:12],
            trust_tier=TrustTier.TIER2_INTERNAL,
            excerpt=json.dumps(finder["result"])[:2000],
            why_selected="finder agent result from the enacted workflow run",
        )
        digest = catalog.insert_evidence_ref(ref, source_id=src["id"])
        for f in finder["result"]["findings"]:
            title = (f.get("title") or "").strip()
            if title:
                title_meta.setdefault(title, f)
                evidence_of.setdefault(title, []).append(digest)

    created = []
    for title in sorted(verified_titles):
        meta = title_meta.get(title, {})
        slug = _slug(title)
        campaigns.create_finding(
            campaign=campaign,
            slug=slug,
            title=title,
            source_kind="workflow",
            root_cause=(meta.get("defect") or "")[:200],
        )
        for digest in evidence_of.get(title, []):
            campaigns.link_finding_evidence(
                campaign=campaign, slug=slug, evidence_digest=digest, role="signal"
            )
        created.append(slug)

    campaigns.open_round(campaign)
    recorded, plausible = 0, []
    for v in sorted(run["verifiers"], key=lambda v: (v["finding_title"], v["lens"])):
        verdict = _VERDICT_MAP.get(v["verdict"])
        if verdict is None:
            plausible.append({"finding": v["finding_title"], "lens": v["lens"]})
            continue
        campaigns.add_lens_verdict(
            campaign,
            _slug(v["finding_title"]),
            lens=v["lens"],
            verdict=verdict,
            rationale=v["reasoning"][:500],
        )
        recorded += 1
    closed = campaigns.close_round(campaign)

    return {
        "campaign": campaign,
        "findings": created,
        "verdicts": recorded,
        "plausible_skipped": plausible,
        "round": closed,
        "finders": len(run["finders"]),
        "verifiers": len(run["verifiers"]),
        "unclassified": len(run["other"]),
    }


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="enact_workflow.py", description=__doc__.splitlines()[0])
    p.add_argument("--dir", required=True, help="workflow run directory (contains journal.jsonl)")
    p.add_argument("--project", required=True)
    p.add_argument("--campaign", required=True)
    p.add_argument("--kind", default="bugsweep")
    args = p.parse_args(argv)
    print(
        json.dumps(
            enact(args.dir, project=args.project, campaign=args.campaign, kind=args.kind),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

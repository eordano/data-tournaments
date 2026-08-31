"""Generate an opencode runner pack from a SweepSpec.

Emits the three-markdown-file pattern (custom command + orchestrator
agent + one lens-worker agent per panel lens) into an ``.opencode/``
tree, so `/sweep-<name>` in opencode drives a real sweep. The generated
orchestrator deliberately owns NO judgment about the loop: it shells
``bin/campaigns.py`` for every state change and treats the CLI's
refusals (batching incomplete, rounds.max reached, lens not in panel) as
the process itself — the fail-closed guards stay server-side, which is
exactly what the community ``/goal`` pattern lacks (a model deciding for
itself when it has converged).

Lens workers get the lens's registry prompt (``bin/lenses.py`` resolve)
baked in at generation time, plus a strict I/O contract: read the
finding dossier via the CLI, reply ``VERDICT: CONFIRM|REFUTE`` with a
rationale, edit nothing.

CLI (also exposed as ``campaigns.py runner-pack``):
  runner_pack.py --spec-file S --name N [--out-dir .opencode] [--campaign C]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from bin import sweep_spec as sweep_spec_mod  # noqa: E402


class RunnerPackError(RuntimeError):
    pass


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    if not slug:
        raise RunnerPackError(f"cannot derive a slug from name {name!r}")
    return slug


def _lens_prompt(prompt_ref: str) -> str:
    try:
        from bin import lenses

        return lenses.resolve(prompt_ref)
    except Exception:
        return (
            f"(Lens prompt {prompt_ref!r} was not resolvable from the prompt "
            "registry at pack-generation time. Fetch it before judging: "
            f"`python3 -c \"from bin import lenses; print(lenses.resolve({prompt_ref!r}))\"`.)"
        )


def _command_md(slug: str, campaign: str, spec) -> str:
    return f"""---
description: Drive sweep campaign '{campaign}' ({spec.kind}) through its review rounds
agent: sweep-orch-{slug}
---

Run the sweep loop for campaign `{campaign}`. Extra operator focus (may be
empty): $ARGUMENTS

Follow your protocol exactly; the campaign CLI's refusals are the process.
"""


def _orch_md(slug: str, campaign: str, spec) -> str:
    runner = spec.runner
    model_line = f"model: {runner.model}\n" if runner and runner.model else ""
    parallel = runner.parallel if runner else 4
    lens_lines = "\n".join(
        f"- `{lens.name}` -> subagent `sweep-lens-{_slug(lens.name)}-{slug}`"
        for lens in spec.panel.lenses
    )
    return f"""---
description: Orchestrates sweep '{campaign}' — dispatches lens workers, records verdicts, obeys the round guards
mode: primary
{model_line}permission:
  edit: deny
---

You orchestrate ONE sweep campaign: `{campaign}` (kind {spec.kind}). You have
no judgment authority over the loop — `python3 bin/campaigns.py` is the
process, and its refusal messages are instructions, not obstacles. Run all
CLI commands from the data-tournaments repo root.

Panel (one worker subagent per lens, at most {parallel} in flight):
{lens_lines}

Protocol, in order:

1. `python3 bin/campaigns.py ledger --campaign {campaign}` and
   `python3 bin/campaigns.py get-spec --campaign {campaign}` to load state.
2. If the ledger has no findings: `python3 bin/campaigns.py ingest-from-spec
   --campaign {campaign}`.
3. `python3 bin/campaigns.py open-round --campaign {campaign}`. If refused
   because a round is already open, continue with that round. If refused
   because rounds.max is reached, go to step 7.
4. For EVERY finding not in a terminal state, for EVERY lens above: dispatch
   the lens's worker subagent via the task tool with the finding slug.
   Run up to {parallel} workers in parallel. Each worker replies
   `VERDICT: CONFIRM|REFUTE` plus `RATIONALE: ...`; record it verbatim:
   `python3 bin/campaigns.py add-lens-verdict --campaign {campaign}
   --slug <slug> --lens <lens> --verdict <VERDICT> --rationale "<RATIONALE>"`.
5. `python3 bin/campaigns.py close-round --campaign {campaign}`. If it
   refuses with "batching is required", dispatch exactly the missing lens
   work it names and close again.
6. Read the close outcome. `converged`: report the ledger and STOP.
   `not_converged`: report what was confirmed, then return to step 3 for the
   next round.
7. If opening a round is refused with "rounds.max reached": run
   `python3 bin/campaigns.py metrics --campaign {campaign}`, present the open
   findings to the human, and STOP. NEVER call dispose-finding yourself —
   dispositions are the human tie-break by definition.

Never invent process. Never mark convergence yourself. Never edit files.
"""


def _worker_md(slug: str, campaign: str, lens) -> str:
    prompt = _lens_prompt(lens.prompt_ref)
    return f"""---
description: '{lens.name}' lens worker for sweep '{campaign}' — one finding per task, burden of {lens.burden}
mode: subagent
temperature: 0.1
permission:
  edit: deny
---

{prompt}

I/O contract — you judge exactly ONE finding per task:

1. Input: a finding slug for campaign `{campaign}`.
2. Load its dossier: `python3 bin/campaigns.py get-finding --campaign
   {campaign} --slug <slug>` (run from the data-tournaments repo root).
   Read code and run read-only repros as needed; never edit anything.
3. Your FINAL message must be exactly two lines:
   `VERDICT: CONFIRM` or `VERDICT: REFUTE`
   `RATIONALE: <one concrete sentence citing what you checked>`
"""


def generate(
    spec_payload: dict,
    *,
    name: str,
    out_dir: str = ".opencode",
    campaign: Optional[str] = None,
) -> list[str]:
    """Write the pack; returns written paths. Refuses when the spec's
    runner driver isn't opencode — a pack for a sweep that declares a
    different driver would be a lie."""
    spec = sweep_spec_mod.validate_spec(spec_payload)
    if spec.runner is None or spec.runner.driver != "opencode":
        raise RunnerPackError(
            "spec.runner.driver must be 'opencode' to generate an opencode "
            f"pack (got {spec.runner.driver if spec.runner else None!r})"
        )
    slug = _slug(name)
    campaign = campaign or name
    out = Path(out_dir)
    (out / "commands").mkdir(parents=True, exist_ok=True)
    (out / "agents").mkdir(parents=True, exist_ok=True)

    written: dict[Path, str] = {
        out / "commands" / f"sweep-{slug}.md": _command_md(slug, campaign, spec),
        out / "agents" / f"sweep-orch-{slug}.md": _orch_md(slug, campaign, spec),
    }
    for lens in spec.panel.lenses:
        written[out / "agents" / f"sweep-lens-{_slug(lens.name)}-{slug}.md"] = (
            _worker_md(slug, campaign, lens)
        )
    for path, body in written.items():
        path.write_text(body, encoding="utf-8")
    return [str(p) for p in written]


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="runner_pack.py", description=__doc__.splitlines()[0])
    p.add_argument("--spec-file", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--out-dir", default=".opencode")
    p.add_argument("--campaign")
    args = p.parse_args(argv)
    payload = json.loads(Path(args.spec_file).read_text(encoding="utf-8"))
    paths = generate(
        payload, name=args.name, out_dir=args.out_dir, campaign=args.campaign
    )
    print(json.dumps({"written": paths}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

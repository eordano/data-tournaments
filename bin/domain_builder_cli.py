"""CLI bridge for the Phoenix /domains/new flow.

Two modes:

  draft mode (default):
    bin/domain_builder_cli.py --description "..." --corpus-spec '<json>'
    Calls DomainBuilder.draft, prints `DRAFT_JSON: {...}` so Phoenix can parse.

  save mode:
    bin/domain_builder_cli.py --save --name foo --description "..." \\
        --generator-prompt "..." --judge-prompt "..." \\
        --corpus-spec '<json>'
    Pushes the prompts to Langfuse + creates the domain row.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path

# When invoked directly (not via `python -m`), make sure the repo root is on
# sys.path so `from bin import ...` works regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Loading the repo .env is mandatory for every CLI entry point: Phoenix
# shell-outs inherit only the server's environment, and without the key vars
# the drafting LM silently falls back to the keyless default endpoint.
from bin.env_loader import load_dotenv as _load_dotenv  # noqa: E402

_load_dotenv()

from bin import domains as _domains  # noqa: E402
from bin import prompts as _prompts  # noqa: E402


def _sample_from_corpus(spec: dict, n: int = 3) -> list[dict]:
    """Cheaply pull up to `n` items from the corpus so DomainBuilder can peek."""
    kind = spec.get("kind")
    out: list[dict] = []
    if kind == "inline":
        for item in (spec.get("items") or [])[:n]:
            text = item.get("text") or item.get("body") or json.dumps(item)
            out.append({"text": text[:400]})
    elif kind == "filesystem":
        from bin.corpus import iter_filesystem_paths
        for path in iter_filesystem_paths(spec):
            try:
                out.append({"text": path.read_text(encoding="utf-8")[:400], "source_ref": str(path)})
            except (OSError, UnicodeError):
                continue
            if len(out) >= n:
                break
    elif kind == "sqlite":
        import sqlite3
        try:
            conn = sqlite3.connect(f"file:{spec['path']}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            for row in conn.execute(spec["query"] + f" LIMIT {n}"):
                d = {k: row[k] for k in row.keys()}
                out.append({"text": (d.get("text") or d.get("content") or "")[:400]})
            conn.close()
        except Exception:
            pass
    return out


def cmd_draft(args):
    import dspy
    from bin.generators.builder import DomainBuilder
    from bin.optimize import _build_lm

    if getattr(dspy.settings, "lm", None) is None:
        dspy.settings.configure(lm=_build_lm())

    corpus_spec = json.loads(args.corpus_spec)
    samples = _sample_from_corpus(corpus_spec)
    builder = DomainBuilder()
    draft = builder.draft(
        description=args.description,
        corpus_kind=corpus_spec.get("kind", "inline"),
        corpus_samples=samples,
    )
    print("DRAFT_JSON: " + json.dumps({
        "domain_name": draft.domain_name,
        "generator_prompt": draft.generator_prompt,
        "judge_prompt": draft.judge_prompt,
    }))


def cmd_save(args):
    corpus_spec = json.loads(args.corpus_spec)
    name = args.name.strip()
    if not name:
        raise SystemExit("--name is required for --save")

    _prompts.push(
        f"card-generator:{name}",
        args.generator_prompt,
        labels=["production"],
    )
    _prompts.push(
        f"judge-instructions:{name}",
        args.judge_prompt,
        labels=["production"],
    )
    domain_id = _domains.create_domain(
        name=name,
        description=args.description,
        corpus_source=corpus_spec,
        generator_prompt=f"card-generator:{name}",
        judge_prompt=f"judge-instructions:{name}",
    )
    print(json.dumps({"domain_id": domain_id, "name": name}))


def cmd_edit(args):
    """Update an existing domain. Pushes new prompt versions (idempotent if
    text is unchanged) and updates description + corpus_source."""
    corpus_spec = json.loads(args.corpus_spec)
    name = args.name.strip()
    if not name:
        raise SystemExit("--name is required for --edit")

    if args.generator_prompt:
        _prompts.push(
            f"card-generator:{name}",
            args.generator_prompt,
            labels=["production"],
        )
    if args.judge_prompt:
        _prompts.push(
            f"judge-instructions:{name}",
            args.judge_prompt,
            labels=["production"],
        )
    _domains.update_domain(
        name,
        description=args.description if args.description else None,
        corpus_source=corpus_spec,
    )
    print(json.dumps({"name": name, "updated": True}))


def _error_hint(e: Exception) -> str:
    """Actionable guidance for common LM failures, without leaking secrets."""
    text = f"{type(e).__name__}: {e}"
    if "AuthenticationError" in text or "connection attempts failed" in text:
        return (
            "\nHINT: the drafting LM could not authenticate or connect. "
            "Set OPENROUTER_API_KEY in the environment or the repo .env "
            "(or point LLM_BASE_URL + LLM_GATEWAY_API_KEY at your own endpoint), "
            "then retry."
        )
    return ""


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--save", action="store_true")
    p.add_argument("--edit", action="store_true")
    p.add_argument("--description", default="")
    p.add_argument("--name", default="")
    p.add_argument("--generator-prompt", default="")
    p.add_argument("--judge-prompt", default="")
    p.add_argument("--corpus-spec", required=True,
                   help="JSON-encoded corpus_source dict")
    args = p.parse_args()
    try:
        if args.save:
            cmd_save(args)
        elif args.edit:
            cmd_edit(args)
        else:
            cmd_draft(args)
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}{_error_hint(e)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

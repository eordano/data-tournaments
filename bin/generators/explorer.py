import shutil
import subprocess
from pathlib import Path
from typing import Optional

import dspy

from bin.generators.card_gen import Card, CardGenError
from bin.workorder import WorkOrderDraft

SKIP_DIRS = {
    ".git", "node_modules", "Library", "Temp", "obj", "bin", "build", "dist",
    "target", "vendor", "__pycache__", ".venv", "Packages", "ProjectSettings",
}
MAX_ENTRIES = 200
MAX_LINES = 300
MAX_MATCHES = 60
MAX_CHARS = 20000

CONTRACT = (
    "The corpus inventory is already given to you: do not spend turns listing "
    "directories. Start by reading the files that look riskiest, then use "
    "search to check how the suspect code is called and whether a test or a "
    "caller-side guard already covers it. A finding is card-worthy only once "
    "you have read the line that produces it. Prefer a few verified findings "
    "over many plausible ones, and never file the same pattern twice -- if it "
    "recurs, file it once and say where else it appears. Every finding MUST name "
    "the corpus-relative path you read the evidence in, as the output field "
    "describes; a finding whose path does not resolve is discarded."
)


class ExploreSig(dspy.Signature):
    """Explore a code corpus and extract verified findings as cards.

    Use the tools to navigate: read the files that look risky, and search for
    how the suspect code is called or tested. Follow leads across files rather
    than judging any file in isolation.
    """

    goal: str = dspy.InputField(desc="What kind of finding to look for.")
    root: str = dspy.InputField(desc="Corpus root; all paths are relative to it.")
    files: str = dspy.InputField(desc="Corpus inventory: one 'path (size)' per line.")
    target_cards: int = dspy.InputField(desc="How many findings to aim for.")
    cards: list[Card] = dspy.OutputField(
        desc="Verified findings, each with title, body, and source_ref 'path:line'."
    )


class ExploreWorkOrderSig(dspy.Signature):
    """Explore a code corpus and extract verified work orders.

    Use the tools to navigate: read the files that look risky, and search for
    how the suspect code is called or tested. Follow leads across files rather
    than judging any file in isolation.
    """

    goal: str = dspy.InputField(desc="What kind of work to look for.")
    root: str = dspy.InputField(desc="Corpus root; all paths are relative to it.")
    files: str = dspy.InputField(desc="Corpus inventory: one 'path (size)' per line.")
    target_cards: int = dspy.InputField(desc="How many work orders to aim for.")
    work_orders: list[WorkOrderDraft] = dspy.OutputField(
        desc=(
            "Verified work orders. The first entry of each one's `files` must be "
            "the corpus-relative path you read the evidence in."
        )
    )


def _safe(root: Path, rel: str) -> Optional[Path]:
    try:
        p = (root / rel.lstrip("/")).resolve()
    except OSError:
        return None
    return p if p == root or root in p.parents else None


def _make_tools(root: Path, globs: list[str]):
    def list_dir(path: str = "") -> str:
        p = _safe(root, path)
        if p is None or not p.is_dir():
            return f"not a directory: {path}"
        out = []
        try:
            entries = sorted(p.iterdir())[:MAX_ENTRIES]
        except OSError as e:
            return f"unreadable: {e}"
        for e in entries:
            if e.name in SKIP_DIRS or e.name.startswith("."):
                continue
            try:
                out.append(e.name + "/" if e.is_dir() else f"{e.name} ({e.stat().st_size}b)")
            except OSError:
                continue
        return "\n".join(out) or "(empty)"

    def read_file(path: str, start: int = 1, end: int = 0) -> str:
        p = _safe(root, path)
        if p is None or not p.is_file():
            return f"not a file: {path}"
        start = max(1, start)
        end = start + MAX_LINES - 1 if end <= 0 else min(end, start + MAX_LINES - 1)
        try:
            lines = p.read_text(errors="replace").splitlines()
        except OSError as e:
            return f"unreadable: {e}"
        window = lines[max(0, start - 1):end]
        body = "\n".join(f"{i}: {l}" for i, l in enumerate(window, start=max(1, start)))
        return body[:MAX_CHARS] or "(empty)"

    def search(pattern: str, path: str = "") -> str:
        p = _safe(root, path or "")
        if p is None:
            return f"bad path: {path}"
        rg = shutil.which("rg")
        if rg:
            cmd = [rg, "-n", "--no-heading", "-m", "3"]
            for g in globs:
                cmd += ["-g", g]
            cmd += ["-e", pattern, "--", str(p)]
        else:
            cmd = ["grep", "-rn", "--max-count=3", "-e", pattern, "--", str(p)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"search failed: {e}"
        hits = [l.replace(str(root) + "/", "") for l in r.stdout.splitlines()[:MAX_MATCHES]]
        return "\n".join(hits) or "(no matches)"

    return [dspy.Tool(list_dir), dspy.Tool(read_file), dspy.Tool(search)]


class CorpusExplorer(dspy.Module):
    def __init__(self, instructions: str, root: str, globs: list[str],
                 max_iters: int = 16, artifact: str = "card"):
        super().__init__()
        self.root = Path(root).resolve()
        self.artifact = artifact
        base = ExploreWorkOrderSig if artifact == "work-order" else ExploreSig
        self.output_field = "work_orders" if artifact == "work-order" else "cards"
        signature = base.with_instructions(
            instructions.rstrip() + "\n\n" + CONTRACT
        )
        self.agent = dspy.ReAct(
            signature, tools=_make_tools(self.root, globs), max_iters=max_iters
        )

    def forward(self, *, goal: str, target_cards: int,
                files: str = "") -> dspy.Prediction:
        try:
            return self.agent(
                goal=goal, root=str(self.root), files=files,
                target_cards=target_cards,
            )
        except Exception as e:
            raise CardGenError(f"{type(e).__name__}: {e}") from e

    def _claimed_path(self, item) -> str:
        if self.artifact == "work-order":
            return (item.files[0] if getattr(item, "files", None) else "").strip()
        return (getattr(item, "source_ref", "") or "").strip()

    def verify(self, items) -> tuple[list[tuple[object, str]], int]:
        kept, dropped = [], 0
        seen = set()
        for item in items:
            claim = self._claimed_path(item)
            rel = claim.split(":", 1)[0]
            resolved = _safe(self.root, rel) if rel else None
            if resolved is None or not resolved.is_file():
                dropped += 1
                continue
            key = (item.title.strip().lower(), rel)
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
            kept.append((item, str(resolved)))
        return kept, dropped


def inventory(paths, root: Path, limit: int = 300) -> str:
    lines = []
    for i, path in enumerate(paths):
        if i >= limit:
            lines.append(f"... ({i}+ more files; search to reach them)")
            break
        try:
            rel = Path(path).resolve().relative_to(root)
            lines.append(f"{rel} ({Path(path).stat().st_size}b)")
        except (OSError, ValueError):
            continue
    return "\n".join(lines)


def explore(*, instructions: str, root: str, globs: list[str], goal: str,
            target_cards: int, files: str = "", artifact: str = "card",
            max_iters: Optional[int] = None) -> tuple[list[tuple[object, str]], int]:
    if max_iters is None:
        max_iters = min(40, 10 + 2 * max(1, target_cards))
    explorer = CorpusExplorer(
        instructions, root, globs, max_iters=max_iters, artifact=artifact
    )
    result = explorer(goal=goal, target_cards=target_cards, files=files)
    return explorer.verify(getattr(result, explorer.output_field, None) or [])

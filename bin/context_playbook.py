"""Structured, incremental context playbooks for judge optimization.

The production prompt remains the immutable seed. Optimizers may add or refine
small, addressable lessons inside the marked playbook block, which avoids the
progressive information loss caused by repeatedly replacing the whole prompt.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from typing import Iterable


START_MARKER = "<!-- context-playbook:start -->"
END_MARKER = "<!-- context-playbook:end -->"
SECTIONS = ("STRATEGIES & INSIGHTS", "EVIDENCE CHECKS", "COMMON MISTAKES TO AVOID")
_ENTRY_RE = re.compile(
    r"^\[(?P<id>[a-z]+-[0-9a-f]{10})\] helpful=(?P<helpful>\d+) "
    r"harmful=(?P<harmful>\d+)(?: domain=(?P<domain>\S+))? :: (?P<content>.+)$"
)
#: An entry becomes prunable once it has accumulated this much total evidence.
PRUNE_MIN_EVIDENCE = 3


@dataclass(frozen=True)
class PlaybookEntry:
    id: str
    section: str
    content: str
    helpful: int = 0
    harmful: int = 0
    #: Optional domain scope. Rendered into the prompt so it round-trips.
    domain: str = ""
    #: Optional source-run identifier. Never rendered into the runtime prompt;
    #: callers persist it out of band (e.g. in the optimizer run manifest).
    provenance: str = ""


def _clean_content(value: str) -> str:
    return " ".join(str(value).strip().split())


def _content_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", _clean_content(value).lower()).strip()


def _section(value: str) -> str:
    normalized = str(value).strip().upper().replace("_", " ").replace("-", " ")
    aliases = {
        "STRATEGY": SECTIONS[0],
        "STRATEGIES": SECTIONS[0],
        "INSIGHT": SECTIONS[0],
        "INSIGHTS": SECTIONS[0],
        "EVIDENCE": SECTIONS[1],
        "EVIDENCE CHECK": SECTIONS[1],
        "CHECK": SECTIONS[1],
        "CHECKS": SECTIONS[1],
        "MISTAKE": SECTIONS[2],
        "MISTAKES": SECTIONS[2],
        "PITFALL": SECTIONS[2],
        "PITFALLS": SECTIONS[2],
    }
    return normalized if normalized in SECTIONS else aliases.get(normalized, SECTIONS[0])


def _entry_id(section: str, content: str, domain: str = "") -> str:
    prefix = {SECTIONS[0]: "str", SECTIONS[1]: "chk", SECTIONS[2]: "mis"}[section]
    scope = f"\0{domain}" if domain else ""
    digest = hashlib.sha256(
        f"{section}\0{_content_key(content)}{scope}".encode()
    ).hexdigest()[:10]
    return f"{prefix}-{digest}"


def split_prompt(prompt: str) -> tuple[str, list[PlaybookEntry]]:
    """Return the immutable seed text and parsed playbook entries."""
    if START_MARKER not in prompt or END_MARKER not in prompt:
        return prompt.strip(), []
    before, rest = prompt.split(START_MARKER, 1)
    block, after = rest.split(END_MARKER, 1)
    base = (before.rstrip() + "\n" + after.lstrip()).strip()
    entries: list[PlaybookEntry] = []
    section = SECTIONS[0]
    for raw in block.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            section = _section(line[3:])
            continue
        match = _ENTRY_RE.match(line)
        if match:
            entries.append(
                PlaybookEntry(
                    id=match.group("id"),
                    section=section,
                    content=_clean_content(match.group("content")),
                    helpful=int(match.group("helpful")),
                    harmful=int(match.group("harmful")),
                    domain=match.group("domain") or "",
                )
            )
    return base, entries


def merge_entries(
    existing: Iterable[PlaybookEntry],
    deltas: Iterable[dict],
    *,
    provenance: str = "",
    domain: str = "",
) -> list[PlaybookEntry]:
    """Deterministically apply curator delta operations without rewriting history.

    Each delta may carry an ``op``: ``add`` (default), ``reinforce``
    (helpful+1), ``weaken`` (harmful+1), or ``retire`` (remove). Targets are
    located by ``id`` first, then by normalized content. After all deltas are
    applied, entries whose harmful count exceeds their helpful count are
    pruned once they have at least ``PRUNE_MIN_EVIDENCE`` total evidence.

    When ``domain`` is set, the merge is domain-bounded: additions are forced
    into that domain regardless of what the delta claims, and lifecycle ops
    (reinforce/weaken/retire) may only target entries of that same domain.
    Global (unscoped) entries and other domains' entries are immutable to a
    scoped run, so one domain's evidence can never erode another's lessons.
    """
    active = _clean_content(domain).replace(" ", "-")
    merged: list = list(existing)
    by_key = {
        (item.domain, _content_key(item.content)): i
        for i, item in enumerate(merged)
    }
    by_id = {item.id: i for i, item in enumerate(merged)}

    def _locate(raw: dict, content: str, delta_domain: str):
        entry_id = str(raw.get("id", "")).strip()
        index = by_id.get(entry_id) if entry_id else None
        if index is None:
            key = _content_key(content)
            index = by_key.get((delta_domain, key)) if key else None
        if index is None or merged[index] is None:
            return None
        if active and merged[index].domain != active:
            # Scoped runs cannot touch global or foreign-domain entries.
            return None
        return index

    for raw in deltas:
        if not isinstance(raw, dict):
            continue
        op = str(raw.get("op", "add")).strip().lower() or "add"
        if op not in ("add", "reinforce", "weaken", "retire"):
            # Unknown operations are rejected outright rather than being
            # silently coerced into an add.
            continue
        content = _clean_content(raw.get("content", ""))
        delta_domain = (
            active
            if active
            else _clean_content(raw.get("domain", "")).replace(" ", "-")
        )
        index = _locate(raw, content, delta_domain)
        if op == "retire":
            if index is not None:
                merged[index] = None
            continue
        if op in ("reinforce", "weaken"):
            if index is None:
                continue
            entry = merged[index]
            if op == "reinforce":
                merged[index] = replace(entry, helpful=entry.helpful + 1)
            else:
                merged[index] = replace(entry, harmful=entry.harmful + 1)
            continue
        # add (default): dedupe against existing content, else append.
        if len(content) < 20:
            continue
        if index is not None:
            merged[index] = replace(merged[index], helpful=merged[index].helpful + 1)
            continue
        section = _section(raw.get("section", "strategy"))
        entry = PlaybookEntry(
            id=_entry_id(section, content, delta_domain),
            section=section,
            content=content,
            domain=delta_domain,
            provenance=provenance,
        )
        by_key[(delta_domain, _content_key(content))] = len(merged)
        by_id[entry.id] = len(merged)
        merged.append(entry)

    kept = [entry for entry in merged if entry is not None]
    # Pruning respects the same boundary as edits: a merge may only prune
    # entries in its own scope (the active domain, or unscoped entries for a
    # global run). Untouched foreign entries always survive.
    return [
        entry
        for entry in kept
        if entry.domain != active
        or not (
            entry.helpful + entry.harmful >= PRUNE_MIN_EVIDENCE
            and entry.harmful > entry.helpful
        )
    ]


def entries_for_domain(
    entries: Iterable[PlaybookEntry], domain: str = ""
) -> list[PlaybookEntry]:
    """Select entries applicable to ``domain``.

    Unscoped entries (``domain == ""``) apply everywhere. Scoped entries only
    apply to their own domain. An empty ``domain`` returns unscoped entries
    only, so global prompts never inherit domain-specific lessons.
    """
    wanted = _clean_content(domain).replace(" ", "-")
    return [
        entry
        for entry in entries
        if not entry.domain or entry.domain == wanted
    ]


def render_prompt(base: str, entries: Iterable[PlaybookEntry]) -> str:
    entries = list(entries)
    if not entries:
        return base.strip()
    lines = [base.strip(), "", START_MARKER]
    for section in SECTIONS:
        section_entries = [entry for entry in entries if entry.section == section]
        if not section_entries:
            continue
        lines.extend([f"## {section}"])
        lines.extend(
            f"[{entry.id}] helpful={entry.helpful} harmful={entry.harmful}"
            + (f" domain={entry.domain}" if entry.domain else "")
            + f" :: {entry.content}"
            for entry in section_entries
        )
        lines.append("")
    if lines[-1] == "":
        lines.pop()
    lines.append(END_MARKER)
    return "\n".join(lines).strip()

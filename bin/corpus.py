"""Shared corpus traversal helpers for domain drafting and card generation."""
from __future__ import annotations

from pathlib import Path
from typing import Iterator


DEFAULT_IGNORED_DIRS = {
    ".claude",
    ".cursor",
    ".git",
    ".idea",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".vscode",
    "__pycache__",
    "build",
    "deps",
    "dist",
    "node_modules",
    "target",
    "vendor",
    "_build",
}


def split_globs(value: str) -> list[str]:
    """Accept comma- or newline-separated glob patterns."""
    return [
        part.strip()
        for line in (value or "*").splitlines()
        for part in line.split(",")
        if part.strip()
    ]


def iter_filesystem_paths(source: dict) -> Iterator[Path]:
    """Yield unique, readable-sized files matching a filesystem source.

    Generated/dependency directories are excluded by default so a recursive
    code glob fans out the project's authored source rather than vendored or
    build output. Domains can override ``ignored_dirs`` and
    ``max_file_bytes`` in their corpus_source JSON.
    """
    root = Path(source["root"])
    ignored = set(source.get("ignored_dirs", DEFAULT_IGNORED_DIRS))
    max_bytes = int(source.get("max_file_bytes") or 512_000)
    seen: set[Path] = set()

    for pattern in split_globs(source.get("glob", "*")):
        for path in sorted(root.glob(pattern)):
            if path in seen or not path.is_file():
                continue
            try:
                relative_parts = path.relative_to(root).parts[:-1]
                if any(part in ignored for part in relative_parts):
                    continue
                if path.stat().st_size > max_bytes:
                    continue
            except (OSError, ValueError):
                continue
            seen.add(path)
            yield path

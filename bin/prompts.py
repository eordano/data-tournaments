"""Prompt storage wrapper.

Langfuse remains the preferred backend when its credentials are configured.
For local development, the same API transparently falls back to a JSON store
under ``DATA_TOURNAMENTS_HOME`` so drafting and domain runs do not require a
Langfuse account.
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from langfuse import get_client


def _client_factory():
    return get_client()


@dataclass
class PromptInfo:
    name: str
    versions: list[int] = field(default_factory=list)
    labels: set[str] = field(default_factory=set)
    production_version: Optional[int] = None
    candidate_version: Optional[int] = None


def _use_local_store() -> bool:
    backend = os.environ.get("PROMPT_BACKEND", "auto").strip().lower()
    if backend == "local":
        return True
    if backend == "langfuse":
        return False
    return not (
        os.environ.get("LANGFUSE_PUBLIC_KEY")
        and os.environ.get("LANGFUSE_SECRET_KEY")
    )


def _local_store_path() -> Path:
    home = Path(os.environ.get("DATA_TOURNAMENTS_HOME", "/tmp/data-tournaments"))
    return home / "prompts.json"


def _read_local_store() -> dict:
    path = _local_store_path()
    if not path.is_file():
        return {"prompts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"could not read local prompt store {path}: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("prompts"), dict):
        raise RuntimeError(f"invalid local prompt store format: {path}")
    return data


def _write_local_store(data: dict) -> None:
    path = _local_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _local_get(name: str, label: str) -> str:
    versions = _read_local_store()["prompts"].get(name, [])
    for row in reversed(versions):
        if label in row.get("labels", []):
            return row["prompt"]
    raise LookupError(f"prompt {name!r} (label={label!r}) not found in local store")


def get(name: str, label: str = "production") -> str:
    if _use_local_store():
        return _local_get(name, label)
    cli = _client_factory()
    try:
        p = cli.get_prompt(name, label=label)
    except LookupError:
        raise
    except Exception as e:
        raise LookupError(f"prompt {name!r} (label={label!r}): {e}") from e
    return p.prompt


def push(name: str, text: str, labels: Optional[list[str]] = None) -> int:
    """Push a prompt version, idempotent on text equality with any existing
    version among the requested labels (so re-pushing the same production
    body returns the existing version instead of creating a duplicate).
    """
    if _use_local_store():
        requested_labels = labels or []
        data = _read_local_store()
        versions = data["prompts"].setdefault(name, [])
        for label in requested_labels:
            for row in reversed(versions):
                if label in row.get("labels", []) and row.get("prompt") == text:
                    return int(row["version"])

        for row in versions:
            row["labels"] = [
                label for label in row.get("labels", [])
                if label not in requested_labels
            ]
        version = max((int(row["version"]) for row in versions), default=0) + 1
        versions.append({"version": version, "prompt": text, "labels": requested_labels})
        _write_local_store(data)
        return version

    cli = _client_factory()
    for label in labels or []:
        try:
            existing = cli.get_prompt(name, label=label)
            if getattr(existing, "prompt", None) == text:
                return existing.version
        except Exception:
            continue
    created = cli.create_prompt(name=name, prompt=text, labels=labels or [], type="text")
    return created.version


def set_label(name: str, version: int, label: str) -> None:
    if _use_local_store():
        data = _read_local_store()
        versions = data["prompts"].get(name, [])
        target = None
        for row in versions:
            row["labels"] = [item for item in row.get("labels", []) if item != label]
            if int(row["version"]) == version:
                target = row
        if target is None:
            raise LookupError(f"prompt {name!r} version {version} not found in local store")
        target.setdefault("labels", []).append(label)
        _write_local_store(data)
        return
    _client_factory().api.prompt_version.update(
        name=name, version=version, new_labels=[label],
    )


def list() -> "list[PromptInfo]":
    if _use_local_store():
        out = []
        for name, rows in _read_local_store()["prompts"].items():
            info = PromptInfo(name=name)
            for row in rows:
                version = int(row["version"])
                labels = set(row.get("labels", []))
                info.versions.append(version)
                info.labels.update(labels)
                if "production" in labels:
                    info.production_version = version
                if "candidate" in labels:
                    info.candidate_version = version
            info.versions.sort()
            out.append(info)
        return sorted(out, key=lambda info: info.name)

    cli = _client_factory()
    out: dict[str, PromptInfo] = {}
    page = 1
    while True:
        resp = cli.api.prompts.list(page=page, limit=100)
        for meta in resp.data:
            info = out.setdefault(meta.name, PromptInfo(name=meta.name))
            info.versions.append(meta.version)
            info.labels.update(meta.labels or [])
            if "production" in (meta.labels or []):
                info.production_version = meta.version
            if "candidate" in (meta.labels or []):
                info.candidate_version = meta.version
        if page >= resp.meta.total_pages:
            break
        page += 1
    return [*out.values()]

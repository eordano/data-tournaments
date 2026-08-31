"""Repo .env loader shared by every CLI entry point.

Walks up from this file looking for `.env`, then parses KEY=value lines into
os.environ WITHOUT overriding values already present in the real environment.
No shell expansion; `export ` prefixes are tolerated. Maps LANGFUSE_BASE_URL
to LANGFUSE_HOST (the SDK reads HOST).

Phoenix shell-outs (generate_cards, domain_builder_cli, optimize) inherit only
the server's environment, so each CLI must call load_dotenv() explicitly at
its entry point — otherwise LM calls silently fall back to the keyless
default endpoint and fail with opaque AuthenticationErrors.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

def find_dotenv(start: Optional[Path] = None) -> Optional[Path]:
    here = (start or Path(__file__).resolve().parent)
    for d in (here, *here.parents):
        candidate = d / ".env"
        if candidate.is_file():
            return candidate
        if (d / ".git").exists():
            return None
    return None

def load_dotenv(start: Optional[Path] = None) -> None:
    path = find_dotenv(start)
    if path is None:
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):]
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key or key in os.environ:
                continue
            os.environ[key] = value
        if "LANGFUSE_HOST" not in os.environ and "LANGFUSE_BASE_URL" in os.environ:
            os.environ["LANGFUSE_HOST"] = os.environ["LANGFUSE_BASE_URL"]
    except Exception:
        pass

"""Strict-by-default in-memory stand-in for the Langfuse Prompts surface.

Every method on the returned client raises ``NotImplementedError`` unless the
test has explicitly opted in by either pre-seeding data (via ``add_prompt``)
or calling ``enable(method_name)``. This is deliberately loud so any new code
path that touches an unstubbed Langfuse method fails the test suite instead
of silently passing on a no-op fake.
"""
from __future__ import annotations
from dataclasses import dataclass, field

@dataclass
class _FakePromptRow:
    name: str
    version: int
    prompt: str
    labels: list[str] = field(default_factory=list)

class FakeLangfuse:
    def __init__(self):
        self._rows: list[_FakePromptRow] = []
        self._enabled: set[str] = set()
        self._call_log: list[tuple[str, tuple, dict]] = []

    def add_prompt(self, name: str, *, text: str, version: int, labels=None):
        self._rows.append(_FakePromptRow(name, version, text, list(labels or [])))
        self._enabled.update({"get_prompt", "list_prompts", "list_versions"})

    def enable(self, method: str):
        self._enabled.add(method)

    def reset_call_log(self):
        self._call_log.clear()

    def call_count(self, method: str) -> int:
        return sum(1 for (m, _a, _kw) in self._call_log if m == method)

    def get_prompt(self, name, *, version=None, label=None):
        rows = [r for r in self._rows if r.name == name]
        if version is not None:
            rows = [r for r in rows if r.version == version]
        if label is not None:
            rows = [r for r in rows if label in r.labels]
        if not rows:
            raise LookupError(f"FakeLangfuse: no prompt {name!r} (version={version}, label={label})")
        return sorted(rows, key=lambda r: r.version, reverse=True)[0]

    def versions(self, name: str) -> list[int]:
        return sorted(r.version for r in self._rows if r.name == name)

    def as_client(self):
        return _FakeClient(self)

class _FakeClient:
    """Mimics the subset of langfuse.Langfuse that bin/prompts.py uses.

    Every method is gated on ``fake._enabled`` so silent no-op stubs are
    impossible. The error message tells the test author exactly which
    behaviour to enable.
    """

    def __init__(self, fake: FakeLangfuse):
        self._fake = fake
        self.api = _FakeApi(fake)

    def _check(self, method: str):
        if method not in self._fake._enabled:
            raise NotImplementedError(
                f"FakeLangfuse: {method!r} called but not enabled in this test. "
                f"Either pre-seed data with fake_langfuse.add_prompt(...) "
                f"or call fake_langfuse.enable({method!r})."
            )

    def get_prompt(self, name, *, version=None, label="production"):
        self._fake._call_log.append(("get_prompt", (name,), {"version": version, "label": label}))
        self._check("get_prompt")
        row = self._fake.get_prompt(name, version=version, label=label)
        return _ClientPromptView(row)

    def create_prompt(self, *, name, prompt, labels=None, type="text", **_):
        self._fake._call_log.append(("create_prompt", (), {"name": name, "prompt": prompt, "labels": labels}))
        self._check("create_prompt")
        next_version = (max([r.version for r in self._fake._rows if r.name == name], default=0)) + 1
        for lbl in labels or []:
            for r in self._fake._rows:
                if r.name == name and lbl in r.labels:
                    r.labels.remove(lbl)
        row = _FakePromptRow(name, next_version, prompt, list(labels or []))
        self._fake._rows.append(row)
        self._fake._enabled.add("get_prompt")
        self._fake._enabled.add("list_prompts")
        return _ClientPromptView(row)

class _FakeApi:
    """Mimics ``langfuse.Langfuse().api.{prompts,prompt_version}``."""

    def __init__(self, fake: FakeLangfuse):
        self.prompts = _FakeApiPrompts(fake)
        self.prompt_version = _FakeApiPromptVersions(fake)

class _FakeApiPrompts:
    def __init__(self, fake: FakeLangfuse):
        self._fake = fake

    def list(self, *, name=None, page=1, limit=100):
        self._fake._call_log.append(("list_prompts", (), {"name": name, "page": page}))
        if "list_prompts" not in self._fake._enabled and "list_versions" not in self._fake._enabled:
            raise NotImplementedError(
                "FakeLangfuse: api.prompts.list called but not enabled. "
                "Call fake_langfuse.enable('list_prompts') or seed via add_prompt(...)."
            )
        rows = [r for r in self._fake._rows if (name is None or r.name == name)]
        rows.sort(key=lambda r: (r.name, r.version))
        return _PageView([_ClientPromptView(r) for r in rows], page=page, total=len(rows))

class _FakeApiPromptVersions:
    def __init__(self, fake: FakeLangfuse):
        self._fake = fake

    def update(self, *, name, version, new_labels):
        self._fake._call_log.append(
            ("set_label", (), {"name": name, "version": version, "new_labels": new_labels})
        )
        if "set_label" not in self._fake._enabled:
            raise NotImplementedError(
                "FakeLangfuse: api.prompt_versions.update called but not enabled. "
                "Call fake_langfuse.enable('set_label')."
            )
        for lbl in new_labels:
            for r in self._fake._rows:
                if r.name == name and lbl in r.labels and r.version != version:
                    r.labels.remove(lbl)
        for r in self._fake._rows:
            if r.name == name and r.version == version:
                for lbl in new_labels:
                    if lbl not in r.labels:
                        r.labels.append(lbl)
                return
        raise LookupError(f"FakeLangfuse: no prompt {name!r} v{version} to label")

class _ClientPromptView:
    """Mirrors the public attrs the real Langfuse prompt object exposes."""

    def __init__(self, row: _FakePromptRow):
        self.name = row.name
        self.version = row.version
        self.prompt = row.prompt
        self.labels = list(row.labels)
        self.type = "text"

class _PageView:
    def __init__(self, data, *, page, total, limit=100):
        self.data = data
        self.meta = type("Meta", (), {"page": page, "total_pages": 1, "total_items": total, "limit": limit})()

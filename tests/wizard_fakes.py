"""Shared fakes for the wizard model-selection tests: a scripted ui, fake accounts and a fake catalog."""
from __future__ import annotations

import json
import pathlib

from ai_resources import model_accounts as ma
from ai_resources import models
from ai_resources.setup import model_selection as ms
from ai_resources.setup import ui, wizard

FIX = pathlib.Path(__file__).parent / "fixtures" / "models"
SENTINEL = "FAKEKEY-DO-NOT-LEAK"


def accounts_for(**creds):
    out = {}
    for pid in ("anthropic", "google", "vertex", "openai", "ollama", "openrouter", "deepseek", "moonshot"):
        ok = creds.get(pid, False)
        out[pid] = ma.Account(ok, ["synthetic" if pid == "anthropic" else "env-file"] if ok else [],
                              "credentials found" if ok else "no credentials", direct_key=ok)
    return out


class FakeCatalog:
    """models.discover over the recorded catalogs, no subprocess."""

    def __init__(self):
        self.calls = []

    def runner(self, argv, **kw):
        self.calls.append(argv)
        if "refresh" in argv:
            return 0, "ok"
        provider = argv[argv.index("--provider") + 1]
        name = "claude-cli-catalog.json" if provider == "claude-cli" else f"catalog-{provider}.json"
        return 0, (FIX / name).read_text()

    def __call__(self, probe):
        return models.discover(self.runner, refresh=False, selection=probe,
                               accounts=accounts_for(**{p: True for p in probe.providers}))


class Script:
    """Answers ui prompts by a substring of the message, records everything shown."""

    def __init__(self, **answers):
        self.answers = dict(answers)
        self.asked: list[str] = []
        self.shown: list[str] = []

    def _find(self, message, kind):
        self.asked.append(f"{kind}: {message}")
        for key in sorted(self.answers, key=len, reverse=True):
            if key in message:
                return self.answers[key]
        raise AssertionError(f"unscripted {kind}: {message!r}")

    def install(self, monkeypatch, *, non_interactive=False):
        s = self
        monkeypatch.setattr(ui, "select", lambda message, choices, default=None, **k: s._pick(message, choices, default))
        monkeypatch.setattr(ui, "checkbox", lambda message, choices, default=None, **k: s._pick(message, choices, default, multi=True))
        monkeypatch.setattr(ui, "confirm", lambda message, default=True: s._find(message, "confirm"))
        monkeypatch.setattr(ui, "password", lambda message, **k: s._find(message, "password"))
        monkeypatch.setattr(ui, "text", lambda message, default="", **k: s._find(message, "text"))
        for name in ("info", "detail", "warn", "error", "ok"):
            monkeypatch.setattr(ui, name, lambda msg, _n=name, _s=s: _s.shown.append(f"{_n}: {msg}"))
        monkeypatch.setattr(ui, "is_non_interactive", lambda: non_interactive)
        monkeypatch.setattr(ui, "console", lambda: _Console(s))

    def _pick(self, message, choices, default, multi=False):
        raw = self._find(message, "checkbox" if multi else "select")
        values = [getattr(c, "value", c) for c in choices]
        if callable(raw):
            raw = raw(values)
        return raw

    @property
    def transcript(self) -> str:
        return "\n".join(self.asked + self.shown)


class _Console:
    def __init__(self, script):
        self.script = script

    def print(self, *a, **k):
        self.script.shown.append(" ".join(str(x) for x in a))

    def rule(self, *a, **k):
        pass


def one(model_id):
    def answer(values):
        return next(v for v in values if getattr(v, "id", None) == model_id)
    return answer


def many(*ids):
    def answer(values):
        return [v for v in values if getattr(v, "id", None) in ids]
    return answer


def slot(slot_name):
    def answer(values):
        return next(v for v in values if getattr(v, "slot", v) == slot_name or v == slot_name)
    return answer

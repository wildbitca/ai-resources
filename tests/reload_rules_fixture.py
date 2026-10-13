"""A reload-rule table built from a checked-in dist excerpt, never from the host's OpenClaw.

The version is deliberately not a real OpenClaw release: nothing in the kit may depend on it.
"""
from __future__ import annotations

import pathlib

from ai_resources import openclaw_reload_rules as rr

FIXTURE_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "openclaw_dist"
FIXTURE_VERSION = "2099.1.1"
DIST_FILE = FIXTURE_DIR / "dist" / "config-reload-plan-baseline.mjs"
DIST_TEXT = DIST_FILE.read_text(encoding="utf-8")


def fixture_ruleset(text: str | None = None, *, version: str | None = FIXTURE_VERSION) -> rr.RuleSet:
    return rr.ruleset_from_source(DIST_TEXT if text is None else text, version=version)


def mutated(old: str, new: str, *, count: int = 1) -> str:
    assert DIST_TEXT.count(old) >= 1, old
    return DIST_TEXT.replace(old, new, count)


def make_rules_unavailable(monkeypatch, *, version: str | None = FIXTURE_VERSION,
                           reason: str = "the installed OpenClaw's reload table cannot be read (test)") -> None:
    """Every classifier call sees a host whose reload rules cannot be read (fail closed: all restart)."""
    monkeypatch.setattr(rr, "current_ruleset", lambda runner, **kw: rr.unavailable(version, reason))

"""Fixtures for the scoring tests. Scores here are ILLUSTRATIVE FIXTURES, never evidence and never seed data."""
from __future__ import annotations

import copy
import functools

from ai_resources.scoring import db as scores_db
from ai_resources.setup import compat

SONNET = "anthropic/claude-sonnet-5.5"
OPUS = "anthropic/claude-opus-5.5"
HAIKU = "anthropic/claude-haiku-5.5"
FABLE = "anthropic/claude-fable-5.1"
GEMINI = "google/gemini-3.8-flash"
SOL = "openai/gpt-6.1-sol"
DEEPSEEK = "deepseek/deepseek-v4-pro"

ROUTE_OF = {SONNET: "anthropic-direct", OPUS: "anthropic-direct", HAIKU: "anthropic-direct",
            FABLE: "anthropic-direct", GEMINI: "google-ai-studio", SOL: "litellm", DEEPSEEK: "litellm"}


@functools.lru_cache(maxsize=1)
def _shipped() -> dict:
    return scores_db.load_shipped()


def shipped_copy() -> dict:
    return copy.deepcopy(_shipped())


def row(role, model, score, *, label="INFERRED", as_of="2026-10-12", route=None, effort="default", **extra):
    r = {"role": role, "model": model, "route": route or ROUTE_OF[model], "effort": effort, "score": score,
         "label": label, "as_of": as_of, "source_ref": "fixture (not evidence)"}
    if label == "INFERRED":
        r["basis"] = "fixture"
    r.update(extra)
    return r


def fixture_doc(rows) -> dict:
    """The shipped policy, routes, surfaces, pools and models with the rows replaced by fixture rows."""
    doc = shipped_copy()
    doc["rows"] = list(rows)
    return doc


def fixture_db(rows) -> scores_db.ScoresDB:
    return scores_db.ScoresDB(fixture_doc(rows))


def make_input(db, *, mode="mixed", enabled=("anthropic", "google", "openai"), allow_unverified=False,
               as_of="2026-10-13", mode_key=compat.MULTI_LITELLM, roles=(), **kw):
    from ai_resources.scoring import gate as gate_mod
    from ai_resources.scoring.compose import ComposeInput
    gate = gate_mod.make_gate(db, cockpit="claude", mode_key=mode_key, allow_unverified=allow_unverified)
    return ComposeInput(db=db, mode=mode, enabled=tuple(enabled), as_of=as_of, gate=gate,
                        allow_unverified=allow_unverified, roles=tuple(roles), **kw)


# -- lanes (ADR-0007): inputs for `scoring.lanes.resolve_lanes` --------------------------------------------

def lane_by_role() -> dict:
    """The `subscription-claude-55` by_role table in alias form (single-model mode)."""
    return {
        "explore": {"model": "haiku", "fallbacks": ["sonnet"]},
        "tester": {"model": "sonnet", "fallbacks": ["haiku"]},
        "doc-writer": {"model": "sonnet"},
        "package-upgrade": {"model": "sonnet"},
        "generalPurpose": {"model": "sonnet"},
        "implementer": {"model": "sonnet"},
        "crashlytics-fixer": {"model": "sonnet"},
        "sentry-fixer": {"model": "sonnet"},
        "terraform-maintainer": {"model": "sonnet"},
        "crossplane-upjet-maintainer": {"model": "sonnet"},
        "code-reviewer": {"model": "sonnet"},
        "security-auditor": {"model": "opus"},
        "verifier": {"model": "opus"},
        "planner": {"model": "sonnet", "fallbacks": ["opus"]},
        "software-architect": {"model": "opus", "fallbacks": ["sonnet"]},
    }


def lane_usage_verdicts(db=None) -> dict:
    """`gate.usage_gate` verdicts keyed by pool id, from the shipped (or a fixture) database."""
    from ai_resources.scoring import gate as gate_mod
    db = db or scores_db.ScoresDB(shipped_copy())
    return {p["id"]: gate_mod.usage_gate(db, "", p["id"])
            for p in db.pools if p["id"] in ("claude-subscription", "agy-claude-gpt", "agy-gemini")}


ALL_ROLES = tuple(lane_by_role())
OPEN_ACKS = {"anthropic": {"roles": list(ALL_ROLES), "text_sha256": "a" * 64},
             "google": {"roles": list(ALL_ROLES), "text_sha256": "b" * 64}}
OPEN_GATES = {"antigravity_flag": True, "adr_0007_agy_accepted": True}

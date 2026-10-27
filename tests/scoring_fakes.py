"""Fixtures for the scoring tests. Scores here are ILLUSTRATIVE FIXTURES, never evidence and never seed data."""
from __future__ import annotations

import copy
import functools

from ai_resources.scoring import db as scores_db

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

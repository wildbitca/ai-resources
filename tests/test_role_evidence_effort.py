"""FU-3 / FU-4: per-role evidence tag and effort are read-only metadata in the composition and `resolved:`."""
from __future__ import annotations

import copy

import pytest

from ai_resources.scoring import evidence
from ai_resources.setup import lanes_render, profiles

ACK = {"anthropic": {"roles": ["explore", "implementer", "planner", "code-reviewer"]}}
BY_ROLE = {"explore": {"model": "haiku"}, "implementer": {"model": "sonnet"}, "planner": {"model": "sonnet"},
           "code-reviewer": {"model": "sonnet"}}
IDS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"}


def _doc(meta=True):
    d = copy.deepcopy(profiles.load_profile("subscription-claude-55"))
    if not meta:
        d.pop("evidence", None)
        d.pop("role_effort", None)
    return d


def _lanes(doc):
    lanes = lanes_render.propose(doc, BY_ROLE, "single-model", acks=ACK, claude_ids=IDS)
    return lanes, lanes_render.render_doc(lanes, BY_ROLE, "single-model", ACK)


def test_confidence_is_derived_from_the_label_and_unknown_is_none():
    assert evidence.confidence("MEASURED-HERE") == "high"
    assert evidence.confidence("INFERRED") == "low"
    assert evidence.confidence("nonsense") == "none"


def test_profile_declares_study_based_evidence_and_effort():
    doc = _doc()
    assert doc["evidence"]["tag"] == "study-based"
    assert doc["evidence"]["confidence"] in ("medium-low", "low", "medium")
    assert doc["role_effort"]["planner"] == "high" and doc["role_effort"]["implementer"] == "high"


def test_resolved_carries_evidence_and_effort_per_role():
    lanes, out = _lanes(_doc())
    roles = out["resolved"]["roles"]
    assert roles["planner"]["effort"] == "high" and roles["implementer"]["effort"] == "high"
    assert roles["explore"]["effort"] == "default"
    for role in ("planner", "explore"):
        ev = roles[role]["evidence"]
        assert ev["tag"] == "study-based" and ev["label"] == "INFERRED" and ev["confidence"]
    assert out["digest"] == lanes_render.doc_digest(out)


def test_metadata_never_changes_routing():
    _, with_meta = _lanes(_doc())
    _, without = _lanes(_doc(meta=False))
    strip = lambda r: {k: {kk: vv for kk, vv in v.items() if kk not in ("evidence", "effort")} for k, v in r.items()}  # noqa: E731
    assert strip(with_meta["resolved"]["roles"]) == without["resolved"]["roles"]
    assert with_meta["roles"] == without["roles"] and with_meta["agents"] == without["agents"]


def test_no_metadata_means_no_new_keys():
    _, out = _lanes(_doc(meta=False))
    for entry in out["resolved"]["roles"].values():
        assert "evidence" not in entry and "effort" not in entry

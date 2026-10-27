"""FU-3 / FU-4: per-role evidence tag and effort are read-only metadata of the profile and the composition."""
from __future__ import annotations

import copy

import pytest

from ai_resources.scoring import evidence
from ai_resources.setup import profiles

def _doc(meta=True):
    d = copy.deepcopy(profiles.load_profile("subscription-claude-55"))
    if not meta:
        d.pop("evidence", None)
        d.pop("role_effort", None)
    return d


def test_confidence_is_derived_from_the_label_and_unknown_is_none():
    assert evidence.confidence("MEASURED-HERE") == "high"
    assert evidence.confidence("INFERRED") == "low"
    assert evidence.confidence("nonsense") == "none"


def test_profile_declares_study_based_evidence_and_effort():
    doc = _doc()
    assert doc["evidence"]["tag"] == "study-based"
    assert doc["evidence"]["confidence"] in ("medium-low", "low", "medium")
    assert doc["role_effort"]["planner"] == "high" and doc["role_effort"]["implementer"] == "high"

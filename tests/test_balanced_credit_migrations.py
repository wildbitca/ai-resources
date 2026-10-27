"""Balanced credit policy: the safe subset of cost migrations (utility model, flash alias)."""
from __future__ import annotations

import json

from ai_resources import openclaw_cost_migrations as cm

LANE = "kit-lane-agy-gemini38flashhigh"


def _doc(**over):
    doc = {"agents": {
        "defaults": {"utilityModel": "anthropic/claude-haiku-5-5",
                     "imageGenerationModel": {"primary": "google/gemini-3.1-flash-image"},
                     "models": {"anthropic/claude-haiku-5-5": {}}},
        "entries": {LANE: {"model": {"primary": "agy-cli-lane/gemini-3.8-flash-high",
                                     "fallbacks": ["anthropic/claude-haiku-5-5"]}},
                    "main": {"model": {"primary": "anthropic/claude-sonnet-5-5"}}}}}
    doc["agents"].update(over)
    return doc


def _by_id(props):
    return {p.id: p for p in props}


def test_the_safe_subset_is_proposed_on_a_google_host():
    got = _by_id(cm.balanced_credit_proposals(_doc()))
    assert set(got) == {cm.BALANCED_UTILITY_ID, cm.BALANCED_ALIAS_ID}
    assert got[cm.BALANCED_UTILITY_ID].new == "google/gemini-3.8-flash"
    assert got[cm.BALANCED_ALIAS_ID].new == {"alias": "flash"} and got[cm.BALANCED_ALIAS_ID].had is False


def test_heartbeat_is_never_moved():
    for p in cm.balanced_credit_proposals(_doc()):
        assert "heartbeat" not in p.path


def test_a_host_without_google_gets_nothing():
    doc = _doc()
    doc["agents"]["defaults"].pop("imageGenerationModel")
    assert cm.balanced_credit_proposals(doc) == []


def test_applied_keep_and_force_are_respected_and_it_is_idempotent():
    doc = _doc()
    all_ids = [p.id for p in cm.balanced_credit_proposals(doc)]
    assert cm.balanced_credit_proposals(doc, applied=all_ids) == []
    assert cm.BALANCED_UTILITY_ID not in _by_id(cm.balanced_credit_proposals(doc, keep={"agents.defaults.utilityModel"}))
    done = _doc()
    done["agents"]["defaults"]["utilityModel"] = "google/gemini-3.8-flash"
    done["agents"]["defaults"]["models"]["google/gemini-3.8-flash"] = {"alias": "flash"}
    assert cm.balanced_credit_proposals(done) == []


def test_build_patch_fills_the_alias():
    patch, replace = cm.build_patch(cm.balanced_credit_proposals(_doc()))
    assert patch["agents"]["defaults"]["utilityModel"] == "google/gemini-3.8-flash"
    assert patch["agents"]["defaults"]["models"]["google/gemini-3.8-flash"] == {"alias": "flash"}
    assert replace == []
    json.dumps(patch)


def test_proposals_opt_in_flag():
    assert not any(p.id.startswith("credit-") for p in cm.proposals(_doc()))
    assert any(p.id.startswith("credit-") for p in cm.proposals(_doc(), balanced_credit=True))

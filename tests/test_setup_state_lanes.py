"""Additive setup-state fields for subscription lanes (ADR-0007, plan 4.1): old files round-trip unchanged."""
from __future__ import annotations

import yaml

from ai_resources.setup import state

LANES = {"orchestrators": ["main"], "routing": {"explore": {"primary": "claude-code:haiku"}}}
OLD_LANES = {                                    # the pre-3.2 shape (ADR-0019 migrates it on load)
    "agents": {"kit-lane-cli-sonnet55": {"backend": "claude-cli", "model": "anthropic/claude-sonnet-5-5",
                                         "pool": "claude-subscription"}},
    "roles": {"implementer": {"candidates": ["openclaw-agent:kit-lane-cli-sonnet55", "claude-code"]}},
    "second": {"code-reviewer": ["openclaw-agent:kit-lane-cli-sonnet55"]},
    "orchestrators": ["main"],
    "verify_capability": {"main": {"mode": "exec", "source": "static-config", "evidence_ref": ""}},
    "pool_caps": {"claude-subscription": 2},
    "max_remote_concurrency": 2,
    "workspace_root": "/home/u/.openclaw/kit-lanes",
    "gates": {"antigravity": True},
    "routing": {"explore": {"primary": "claude-code:haiku"}},
}
ACKS = {"anthropic": {"date": "2026-10-15", "text_sha256": "ab", "text": "t", "roles": ["implementer"]}}


def test_old_state_file_round_trips_byte_identically(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "state_path", lambda: tmp_path / "setup-state.yaml")
    s = state.SetupState()
    s.profile.name = "balanced-claude-55"
    state.save(s)
    first = (tmp_path / "setup-state.yaml").read_text()
    doc = yaml.safe_load(first)
    assert "lanes" not in doc["profile"] and "lane_acks" not in doc["profile"]
    assert "lanes_applied" not in doc["openclaw"]
    loaded = state.load()
    assert loaded.profile.lanes is None
    assert loaded.openclaw.lanes_applied == {}
    state.save(loaded)
    again = yaml.safe_load((tmp_path / "setup-state.yaml").read_text())
    again["last_run"] = doc["last_run"]
    assert again == doc


def test_lanes_and_applied_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "state_path", lambda: tmp_path / "setup-state.yaml")
    s = state.SetupState()
    s.profile.lanes = LANES
    s.openclaw.lanes_applied = {"created_agent_ids": ["kit-lane-cli-sonnet55"],
                                "allow_agents_added": {"main": ["kit-lane-cli-sonnet55"]}}
    state.save(s)
    back = state.load()
    assert back.profile.lanes == LANES
    assert back.openclaw.lanes_applied["created_agent_ids"] == ["kit-lane-cli-sonnet55"]
    assert state.lanes_enabled(back)


def test_the_old_lanes_shape_is_migrated_on_load_and_old_files_still_load(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "state_path", lambda: tmp_path / "setup-state.yaml")
    s = state.SetupState()
    s.profile.lanes = OLD_LANES
    state.save(s)
    path = tmp_path / "setup-state.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["profile"]["lane_acks"] = ACKS             # an old kit wrote the acknowledgements beside the lanes
    path.write_text(yaml.safe_dump(doc))
    back = state.load()
    assert back.profile.lanes == LANES and state.lanes_enabled(back)       # gates dropped, routing kept
    assert not hasattr(back.profile, "lane_acks")
    state.save(back)
    saved = yaml.safe_load(path.read_text())["profile"]
    assert "lane_acks" not in saved and "gates" not in saved["lanes"]       # gone from the file after the next save


def test_empty_lanes_are_not_enabled():
    s = state.SetupState()
    assert not state.lanes_enabled(s)
    s.profile.lanes = {}
    assert not state.lanes_enabled(s)

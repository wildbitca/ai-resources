"""Additive state for composed teams: selection.headroom and profile.composed (AC-15)."""
from __future__ import annotations

import yaml

from ai_resources import selection as sel
from ai_resources.setup import state


def _use_tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "state_path", lambda: tmp_path / "setup-state.yaml")


def _chosen(**kw):
    return sel.Selection(shape="multi-provider",
                         providers={"anthropic": sel.ProviderSel(True, ["synthetic"])},
                         slots={"anthropic:sonnet": sel.SlotSel("anthropic/claude-sonnet-5-5", "family")},
                         primary="anthropic:sonnet", **kw)


def test_headroom_defaults_to_empty_and_is_not_written_when_empty():
    s = _chosen()
    assert s.headroom == {}
    assert "headroom" not in s.to_dict()


def test_headroom_round_trips_with_level_and_live_flag():
    s = _chosen(headroom={"claude-subscription": {"level": "scarce", "live": True},
                          "openai-api": {"level": "ample", "live": False}})
    back = sel.Selection.from_dict(s.to_dict())
    assert back == s and back.headroom["claude-subscription"] == {"level": "scarce", "live": True}


def test_headroom_garbage_is_dropped_not_fatal():
    d = _chosen().to_dict()
    d["headroom"] = {"p": {"level": "enormous", "live": "yes"}, "q": "x", 3: {"level": "normal"},
                     "ok": {"level": "normal", "live": "yes"}}
    back = sel.Selection.from_dict(d)
    assert back.headroom == {"ok": {"level": "normal", "live": False}}


def test_old_state_file_without_headroom_or_composed_loads_and_round_trips(tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    s = state.SetupState()
    s.set_selection(_chosen())
    state.save(s)
    path = state.state_path()
    doc = yaml.safe_load(path.read_text())
    assert "composed" not in doc["profile"] and "headroom" not in doc["selection"]
    loaded = state.load()
    assert loaded.profile.composed is None and loaded.get_selection().headroom == {}
    assert loaded.schema_version == state.SCHEMA_VERSION == "1.0"
    state.save(loaded)
    again = yaml.safe_load(path.read_text())
    assert again["profile"] == doc["profile"] and again["selection"] == doc["selection"]


def test_composed_provenance_round_trips(tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    s = state.SetupState()
    s.profile.customized = True
    s.profile.customizations = {"planner": {"model": "anthropic/claude-opus-5.5"}}
    s.profile.composed = {"mode": "mixed", "input_digest": "abc", "unverified_roles": ["tester"]}
    state.save(s)
    back = state.load()
    assert back.profile.composed == {"mode": "mixed", "input_digest": "abc", "unverified_roles": ["tester"]}
    assert back.profile.customizations == s.profile.customizations


def test_unknown_future_keys_in_the_profile_block_are_ignored(tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    state.state_path().write_text(yaml.safe_dump({"schema_version": "1.0",
                                                  "profile": {"name": "x", "future_key": 1}}))
    assert state.load().profile.name == "x"

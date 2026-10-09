"""model_pins: the single declaration, the overlay and the higher-of rule."""
from __future__ import annotations

import json
import logging
import os
import stat

import pytest

from ai_resources import model_pins as mp
from ai_resources import openclaw_host as host


def test_no_overlay_effective_equals_defaults():
    assert mp.effective() == mp.DEFAULTS


def test_overlay_raises_one_class_only():
    got = mp.effective({"pins": {"sonnet": "claude-sonnet-5-5"}})
    assert got["sonnet"] == "claude-sonnet-5-5"
    assert {k: v for k, v in got.items() if k != "sonnet"} == {k: v for k, v in mp.DEFAULTS.items() if k != "sonnet"}


def test_higher_of_kit_default_beats_a_stale_overlay(monkeypatch):
    monkeypatch.setitem(mp.DEFAULTS, "sonnet", "claude-sonnet-5-5")
    assert mp.effective({"pins": {"sonnet": "claude-sonnet-5"}})["sonnet"] == "claude-sonnet-5-5"


def test_frozen_class_keeps_a_lower_pin(monkeypatch):
    monkeypatch.setitem(mp.DEFAULTS, "sonnet", "claude-sonnet-5-5")
    ov = {"pins": {"sonnet": "claude-sonnet-5"}, "policy": {"classes": {"sonnet": {"mode": "frozen"}}}}
    assert mp.effective(ov)["sonnet"] == "claude-sonnet-5"


def test_frozen_without_explicit_pin_does_not_hold_anything():
    ov = {"policy": {"classes": {"sonnet": {"mode": "frozen"}}}}
    assert mp.effective(ov) == mp.DEFAULTS


def test_overlay_pin_of_another_family_is_ignored():
    assert mp.effective({"pins": {"sonnet": "claude-opus-9"}}) == mp.DEFAULTS


def test_corrupt_overlay_gives_defaults_and_warns(caplog):
    mp.overlay_path().write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert mp.effective() == mp.DEFAULTS
    assert any("not valid JSON" in r.message for r in caplog.records)


def test_non_object_overlay_is_ignored():
    mp.overlay_path().write_text("[1]", encoding="utf-8")
    assert mp.load_overlay() == {}


@pytest.mark.parametrize("model_id,expected", [
    ("claude-sonnet-5-5", ("sonnet", 5, 5)),
    ("claude-opus-5", ("opus", 5, 0)),
    ("claude-haiku-4-5", ("haiku", 4, 5)),
    ("claude-mythos-5", None),
    ("claude-haiku-4-5-20251001", None),
    ("claude-opus-4-6-thinking", None),
    ("claude-sonnet-5[1m]", None),
    ("gpt-5", None),
])
def test_parse_id(model_id, expected):
    assert mp.parse_id(model_id) == expected


def test_refs():
    assert mp.openclaw_ref("claude-sonnet-5-5") == "anthropic/claude-sonnet-5-5"
    assert mp.openrouter_id("claude-haiku-4-5") == "anthropic/claude-haiku-4.5"
    assert mp.openrouter_id("claude-sonnet-5") == "anthropic/claude-sonnet-5"


def test_save_overlay_is_atomic_0600_and_trims_history(tmp_path):
    path = tmp_path / "sub" / "model-pins.json"
    mp.save_overlay({"pins": {"opus": "claude-opus-5-5"}, "history": list(range(80))}, path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    data = json.loads(path.read_text())
    assert data["schema"] == 2 and len(data["history"]) == 50 and data["history"][-1] == 79
    assert not list(path.parent.glob("*.tmp"))
    assert mp.load_overlay(path)["pins"] == {"anthropic:opus": "claude-opus-5-5"}


def test_agy_static_is_the_antigravity_list():
    from ai_resources.setup.cockpits import openclaw
    assert openclaw.ENGINES["antigravity"].models == mp.AGY_STATIC


def test_overlay_moves_host_patch_and_cockpit_default():
    doc = json.loads(open("tests/fixtures/openclaw_host_config.json").read())
    mp.save_overlay({"pins": {"sonnet": "claude-sonnet-5-5"}})
    built = host.build_host_patch(host.load_host_profile(), doc, {"DOMAIN": "a.example", "POD_CIDR": "10.0.0.0/24"})
    assert not any("MODEL_SONNET" in note for note in built["skipped"])
    assert "anthropic/claude-sonnet-5-5" in json.dumps(built["patch"])
    from ai_resources.setup.cockpits import openclaw
    from ai_resources.setup import state
    assert openclaw.default_worker_model() == "claude-sonnet-5-5"
    assert state.OpenClawState().worker_model == "claude-sonnet-5-5"
    assert openclaw.engine_models(openclaw.ENGINES["claude-code"])[0] == "anthropic/claude-sonnet-5-5"


def test_entry_already_on_the_new_pin_is_not_patched_back():
    doc = json.loads(open("tests/fixtures/openclaw_host_config.json").read())
    mp.save_overlay({"pins": {"sonnet": "claude-sonnet-5-5"}})
    for entry in doc["agents"]["entries"].values():
        if isinstance(entry, dict) and entry.get("model"):
            entry["model"] = {"primary": "anthropic/claude-sonnet-5-5"}
    built = host.build_host_patch(host.load_host_profile(), doc, {"DOMAIN": "a.example", "POD_CIDR": "10.0.0.0/24"})
    assert "claude-sonnet-5\"" not in json.dumps(built["patch"])

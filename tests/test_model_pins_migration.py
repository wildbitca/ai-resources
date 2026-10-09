"""Overlay schema 2: slot keys, per-slot answers, and the v1 -> v2 migration."""
from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace

from ai_resources import model_pins as mp

FIX = pathlib.Path(__file__).parent / "fixtures" / "models"


def _v1_file(tmp_path):
    path = tmp_path / "model-pins.json"
    path.write_text((FIX / "overlay-v1.json").read_text(), encoding="utf-8")
    return path


def test_a_v1_overlay_is_rekeyed_by_slot_on_save(tmp_path):
    path = _v1_file(tmp_path)
    mp.save_overlay(mp.load_overlay(path), path)
    data = json.loads(path.read_text())
    assert data["schema"] == 2
    assert data["pins"] == {"anthropic:opus": "claude-opus-5-5", "anthropic:haiku": "claude-haiku-4-5"}
    assert "classes" not in data["policy"]
    assert data["policy"]["slots"]["anthropic:haiku"] == {"answer": "ask", "frozen": True}
    assert data["approvals"] == {"anthropic:fable": "claude-fable-5-2"}
    assert list(data["pending"]) == ["anthropic:sonnet"]
    assert data["state"]["failed"] == {"anthropic:sonnet": "claude-sonnet-5-5"}
    assert list(data["state"]["last_change"]["changes"]) == ["anthropic:opus"]
    assert list(data["state"]["last_change"]["previous_pins"]) == ["anthropic:opus"]
    assert data["history"][0]["action"] == "switch"           # history is kept as written
    assert (tmp_path / "model-pins.json.v1.bak").read_text() == (FIX / "overlay-v1.json").read_text()


def test_load_and_save_twice_changes_nothing(tmp_path):
    path = _v1_file(tmp_path)
    mp.save_overlay(mp.load_overlay(path), path)
    first = path.read_bytes()
    mp.save_overlay(mp.load_overlay(path), path)
    assert path.read_bytes() == first
    assert (tmp_path / "model-pins.json.v1.bak").exists()


def test_the_v1_backup_is_written_once(tmp_path):
    path = _v1_file(tmp_path)
    mp.save_overlay(mp.load_overlay(path), path)
    bak = tmp_path / "model-pins.json.v1.bak"
    bak.write_text("kept", encoding="utf-8")
    mp.save_overlay(mp.load_overlay(path), path)
    assert bak.read_text() == "kept"


def test_v1_policy_modes_map_to_answers():
    ov = mp.migrate(json.loads((FIX / "overlay-v1.json").read_text()))
    assert mp.slot_policy(ov, "anthropic:opus")["answer"] == "always"
    assert mp.slot_policy(ov, "anthropic:opus")["max_bump"] == "minor"
    assert mp.slot_policy(ov, "anthropic:sonnet")["answer"] == "ask"
    assert mp.slot_policy(ov, "anthropic:haiku")["frozen"] is True


def test_defaults_keep_v1_behaviour_for_claude_and_ask_for_everything_else():
    assert mp.slot_policy({}, "anthropic:sonnet")["answer"] == "always"
    assert mp.slot_policy({}, "sonnet")["max_bump"] == "minor"
    assert mp.slot_policy({}, "google:gemini-flash")["answer"] == "ask"


def test_bare_class_keys_are_accepted_everywhere():
    assert mp.effective({"pins": {"sonnet": "claude-sonnet-5-5"}})["sonnet"] == "claude-sonnet-5-5"
    assert mp.effective({"pins": {"anthropic:sonnet": "claude-sonnet-5-5"}})["sonnet"] == "claude-sonnet-5-5"
    ov = {"pins": {"sonnet": "claude-sonnet-5"}, "policy": {"classes": {"sonnet": {"mode": "frozen"}}}}
    assert mp.is_frozen(ov, "sonnet") and mp.is_frozen(ov, "anthropic:sonnet")


def test_family_never_and_never_ids_are_read():
    ov = {"policy": {"slots": {"google:gemini-flash": {"answer": "ask", "never_ids": ["gemini-3.9-flash"]}},
                     "families": {"google:gemini-flash": {"never": True}}}}
    pol = mp.slot_policy(ov, "google:gemini-flash")
    assert pol["never_ids"] == ["gemini-3.9-flash"] and pol["family_never"] is True


def _selection(*pairs):
    return SimpleNamespace(slots={s: {"ref": r, "track": t} for s, r, t in pairs})


def test_effective_slots_without_a_selection_are_the_four_claude_slots():
    assert mp.effective_slots(None, {}) == {f"anthropic:{c}": mp.DEFAULTS[c] for c in mp.CLASSES}


def test_effective_slots_take_the_highest_of_default_selection_and_pin():
    sel = _selection(("google:gemini-flash", "google/gemini-3.7-flash", "family"))
    assert mp.effective_slots(sel, {})["google:gemini-flash"] == "gemini-3.8-flash"     # kit default is newer
    ov = {"pins": {"google:gemini-flash": "gemini-3.9-flash"}}
    assert mp.effective_slots(sel, ov)["google:gemini-flash"] == "gemini-3.9-flash"


def test_a_fixed_track_keeps_the_selected_id_and_a_frozen_slot_its_pin():
    fixed = _selection(("google:gemini-flash", "google/gemini-3.7-flash", "fixed"))
    assert mp.effective_slots(fixed, {})["google:gemini-flash"] == "gemini-3.7-flash"
    fam = _selection(("google:gemini-flash", "google/gemini-3.9-flash", "family"))
    frozen = {"pins": {"google:gemini-flash": "gemini-3.6-flash"},
              "policy": {"slots": {"google:gemini-flash": {"frozen": True}}}}
    assert mp.effective_slots(fam, frozen)["google:gemini-flash"] == "gemini-3.6-flash"


def test_a_preview_pin_never_outranks_a_stable_default():
    sel = _selection(("google:gemini-pro", "google/gemini-3.1-pro-preview", "family"))
    ov = {"pins": {"google:gemini-pro": "gemini-9-pro-preview"}}
    assert mp.effective_slots(sel, ov)["google:gemini-pro"] == "gemini-3.1-pro-preview"


def test_ref_drift_reports_a_stale_google_ref_for_its_slot():
    sel = _selection(("google:gemini-flash", "google/gemini-3.7-flash", "family"))
    slots = mp.effective_slots(sel, {})
    assert mp.ref_drift("google/gemini-3.7-flash", slots=slots) == "google/gemini-3.8-flash"
    assert mp.ref_drift("google/gemini-3.8-flash", slots=slots) is None
    assert mp.ref_drift("google/gemini-flash-latest", slots=slots) is None
    assert mp.ref_drift("google/gemini-3.7-flash") is None           # no slots: not a managed ref
    assert mp.ref_drift("anthropic/claude-sonnet-5") is None


def test_propose_decisions_are_identical_before_and_after_migration(monkeypatch):
    """AC-S4d: auto-minor -> always/minor keeps the exact v1 ladder on the golden catalog."""
    from ai_resources import audit, models
    catalog = (FIX / "claude-cli-catalog.json").read_text()

    def runner(argv, **kw):
        return 0, ("updated" if "refresh" in argv else catalog)

    prices = dict(audit.PRICES)
    prices["claude-sonnet-5-5"] = audit.PRICES["claude-sonnet-5"]
    monkeypatch.setattr(audit, "PRICES", prices)
    disc = models.discover(runner)
    v1 = json.loads((FIX / "overlay-v1.json").read_text())
    v2 = mp.migrate(v1)
    eff = mp.effective(v2)

    def rows(ov):
        return [(p.slot, p.old, p.new, p.kind, p.decision, p.reasons) for p in models.propose(eff, disc, ov)]

    assert rows(v1) == rows(v2)
    assert mp.migrate(v2) == v2

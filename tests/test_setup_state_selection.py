"""SetupState.selection: absent means the implicit Claude set and a byte-stable state file."""
from __future__ import annotations

import yaml

from ai_resources import model_pins, selection as sel
from ai_resources.setup import state


def _use_tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "state_path", lambda: tmp_path / "setup-state.yaml")


def _legacy_state_text():
    s = state.SetupState(mode="single-model", backend="litellm")
    s.last_run = "2026-10-01T00:00:00+00:00"
    doc = state._to_dict(s)
    doc.pop("selection")                # a file written before the field existed has no such key
    return yaml.safe_dump(doc, default_flow_style=False, sort_keys=False)


def test_a_state_without_a_selection_round_trips_byte_stable(tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    path = state.state_path()
    path.write_text(_legacy_state_text(), encoding="utf-8")
    s = state.load()
    assert s.selection is None and s.get_selection() is None
    state.save(s)
    first = path.read_text(encoding="utf-8")
    assert "selection" not in first
    state.save(state.load())
    stripped = lambda t: "\n".join(l for l in t.splitlines() if not l.startswith("last_run:"))
    assert stripped(path.read_text(encoding="utf-8")) == stripped(first)
    assert stripped(first) == stripped(_legacy_state_text())          # AC-S9d: nothing new is written


def test_the_implicit_selection_is_the_four_claude_slots_on_the_claude_cli():
    imp = sel.implicit_selection()
    assert list(imp.slots) == [f"anthropic:{c}" for c in model_pins.CLASSES]
    assert imp.primary == "anthropic:sonnet" and imp.smoke_path == "claude-cli"
    assert imp.slots["anthropic:sonnet"].ref == "anthropic/claude-sonnet-5"


def test_a_selection_round_trips_losslessly(tmp_path, monkeypatch):
    _use_tmp_state(tmp_path, monkeypatch)
    s = state.SetupState()
    chosen = sel.Selection(
        shape="multi-provider",
        providers={"anthropic": sel.ProviderSel(True, ["synthetic"]), "google": sel.ProviderSel(True, ["openclaw-env"], True)},
        slots={"anthropic:sonnet": sel.SlotSel("anthropic/claude-sonnet-5", "family"),
               "google:gemini-flash": sel.SlotSel("google/gemini-3.8-flash", "fixed")},
        primary="google:gemini-flash", smoke_path="litellm", openclaw_engine="claude-code", allow_unverified=True)
    s.set_selection(chosen)
    state.save(s)
    back = state.load().get_selection()
    assert back == chosen
    state.save(state.load())
    assert state.load().get_selection() == chosen


def test_shape_follows_the_providers_and_slot_count():
    assert sel.derive_shape(["google"], 1) == "single"
    assert sel.derive_shape(["google"], 3) == "single-provider"
    assert sel.derive_shape(["google", "anthropic"], 2) == "multi-provider"
    assert sel.derive_shape(["anthropic", "google"], 1) == "multi-provider"


def test_garbage_in_the_state_file_is_ignored_not_fatal():
    assert sel.Selection.from_dict("nope") is None
    assert sel.Selection.from_dict({"slots": {}}) is None
    weird = sel.Selection.from_dict({"slots": {"google:gemini-flash": {"ref": "google/x", "track": "weird"}},
                                     "shape": "bogus", "smoke_path": "bogus"})
    assert weird.shape == "single" and weird.smoke_path == "claude-cli" and weird.slots["google:gemini-flash"].track == "family"

"""The summary screen: one line per detected cockpit, before any write; Cancel writes nothing."""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path

import pytest

from ai_resources import selection as sel
from ai_resources.setup import model_selection as ms, state, ui, wizard
from wizard_fakes import SENTINEL, Script


def _single_flash():
    s = state.SetupState()
    for cid in ("claude", "openclaw", "gemini", "cursor"):
        s.cockpits[cid] = state.CockpitState(installed=True)
    chosen = sel.Selection(shape="single", providers={"google": sel.ProviderSel()},
                           slots={"google:gemini-flash": sel.SlotSel("google/gemini-3.8-flash")},
                           primary="google:gemini-flash", smoke_path="direct")
    s.set_selection(chosen)
    return s


def _lines(s):
    actions = ms.plan_table(s.get_selection(), s.mode, s.backend, wizard._detected_cockpit_ids(s))
    return {a.cockpit: ms.describe(a) for a in actions}


def test_single_gemini_flash_summary_has_one_honest_line_per_cockpit():
    got = _lines(_single_flash())
    assert got["claude"].endswith("skipped: Claude Code runs only Claude models without a gateway")
    assert "uses its own model settings; ai-resources sets auth and MCP only" in got["gemini"]
    assert "Cursor: instructions only; Cursor keeps its own model settings" in got["cursor"] or "instructions only" in got["cursor"]
    assert "skipped: no verified OpenClaw runtime" in got["openclaw"]
    assert set(got) == {"claude", "openclaw", "gemini", "cursor"}


def test_the_summary_lists_the_files_slots_and_backend(monkeypatch):
    script = Script(**{"Apply this?": ms.APPLY})
    script.install(monkeypatch)
    s = _single_flash()
    assert wizard._step_summary(s, argparse.Namespace()) == 0
    text = script.transcript
    assert "google:gemini-flash = google/gemini-3.8-flash [family] (primary)" in text
    assert "Backend: none (single-model)" in text
    assert "~/.gemini/GEMINI.md" in text and "~/.cursor/mcp.json" in text
    assert "will not work until you authenticate" in text
    assert "~/.aider.conf.yml" not in text


def test_no_backend_question_is_asked_for_a_single_model_without_a_gateway_cockpit(monkeypatch):
    script = Script(**{"Apply this?": ms.APPLY})
    script.install(monkeypatch)
    wizard._step_summary(_single_flash(), argparse.Namespace())
    assert not any("gateway" in q.lower() and q.startswith(("select", "checkbox")) for q in script.asked)


def _tree_digest(root: Path) -> str:
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(str(path.relative_to(root)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def test_cancel_returns_before_the_apply_step_and_changes_no_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    before = _tree_digest(tmp_path)
    script = Script(**{"Apply this?": ms.CANCEL})
    script.install(monkeypatch)
    applied = []
    monkeypatch.setattr(wizard, "_step9_apply", lambda s, dry_run=False: applied.append(1) or 0)
    s = _single_flash()
    assert wizard._step_summary(s, argparse.Namespace()) == 130
    assert applied == [] and _tree_digest(tmp_path) == before


def test_change_models_goes_back_to_the_picker(monkeypatch):
    calls = []
    script = Script(**{"Apply this?": ms.CHANGE})
    script.install(monkeypatch)
    monkeypatch.setattr(wizard, "_step_models", lambda s, a, dry=False: calls.append("models") or 130)
    assert wizard._step_summary(_single_flash(), argparse.Namespace()) == 130 and calls == ["models"]


def test_a_host_without_a_selection_has_no_summary_and_no_route(monkeypatch):
    script = Script()
    script.install(monkeypatch)
    s = state.SetupState()
    s.cockpits["claude"] = state.CockpitState(installed=True)
    assert wizard._step_summary(s, argparse.Namespace()) == 0 and script.asked == [] and script.shown == []


def test_non_interactive_prints_the_plan_but_never_asks(monkeypatch):
    script = Script()
    script.install(monkeypatch, non_interactive=True)
    assert wizard._step_summary(_single_flash(), argparse.Namespace()) == 0
    assert script.asked == [] and any("Cockpits:" in m for m in script.shown)


def test_the_dry_run_plan_table_names_every_detected_cockpit(monkeypatch):
    script = Script()
    script.install(monkeypatch)
    wizard._print_plan_table(_single_flash())
    text = script.transcript
    assert text.count("skipped:") >= 2 and "instructions only" in text
    s = state.SetupState()
    s.cockpits["claude"] = state.CockpitState(installed=True)
    s.cockpits["gemini"] = state.CockpitState(installed=True)
    script2 = Script()
    script2.install(monkeypatch)
    wizard._print_plan_table(s)
    assert "no model selection recorded" in script2.transcript and "will configure anthropic/claude-opus-5, anthropic/claude-sonnet-5" in script2.transcript


def test_the_antigravity_engine_shows_its_own_id_and_the_risk_line():
    s = state.SetupState()
    s.cockpits["openclaw"] = state.CockpitState(installed=True)
    s.cockpits["agy"] = state.CockpitState(installed=True)
    s.set_selection(sel.Selection(providers={"google": sel.ProviderSel()},
                                  slots={"google:gemini-flash": sel.SlotSel("google/gemini-3.8-flash")}))
    actions = ms.plan_table(s.get_selection(), "single-model", None, ["openclaw", "agy"], engine="antigravity")
    line = ms.describe(next(a for a in actions if a.cockpit == "openclaw"))
    assert "gemini-3.8-flash-low (Antigravity id)" in line and "unrestricted code execution" in line
    agy = next(a for a in actions if a.cockpit == "agy")
    assert "Antigravity engine" in ms.describe(agy)

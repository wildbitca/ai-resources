"""The wizard asks when `ai-resources doctor` should report a gateway running older plugin code.

Measured 2026-09-29 on the live host right after 1.12.1: plugin linked, loaded and stale, engine
`keep`, `antigravity_applied` false, so doctor section 4c never ran and nothing was reported. The
gate is now the operator's answer (`stale_plugin_check`: linked | engine | off).

Fakes and tmp_path only: no gateway, no systemctl, nothing under ~/.openclaw. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests"))

from ai_resources import doctor  # noqa: E402
from ai_resources.setup import state, ui  # noqa: E402
from ai_resources.setup.cockpits import openclaw, _shared  # noqa: E402
from test_openclaw_stale_plugin import (  # noqa: E402
    KIT_INSTALLED_EPOCH, STARTED_EPOCH, _FakeConsole, plugin_tree, runtime_says,
)

TIME_MSG = "OpenClaw is running older ai-resources plugin code than the kit on disk (restart to load it)."
PATH_MSG = "OpenClaw is running the ai-resources plugin from an older kit directory."
BACKEND_MSG = "OpenClaw has not loaded the ai-resources plugin (no agy-cli backend)."


def run_doctor(monkeypatch, tmp_path, *, check=None, loaded=True, linked=False, applied=False,
               stale="time", backend=True, quota=None):
    """Run cmd_doctor against a state and a plugin tree; return (warn/detail lines, backend probes).

    `loaded` is what the (faked) gateway reports (True, False: unreadable, or "disabled"); `linked` is the state file's `plugin_linked`,
    False by default because that is the measured engine-`keep` host: the kit never linked the
    plugin, the gateway runs it anyway. `quota` collects calls of the antigravity quota check."""
    if stale == "path":
        plugin = plugin_tree(tmp_path, STARTED_EPOCH - 3600)
        old = tmp_path / "old" / "openclaw-plugin" / "ai-resources"
        old.mkdir(parents=True)
        runtime_says(monkeypatch, old)
    elif stale == "time":
        plugin = plugin_tree(tmp_path, KIT_INSTALLED_EPOCH)
        runtime_says(monkeypatch, plugin)
    else:
        plugin = plugin_tree(tmp_path, STARTED_EPOCH - 3600)
        runtime_says(monkeypatch, plugin)
    monkeypatch.setattr(openclaw, "_gateway_started_at", lambda: STARTED_EPOCH)
    monkeypatch.setattr(_shared, "stable_kit_root", lambda _root: plugin.parents[1])
    if loaded == "disabled":
        monkeypatch.setattr(openclaw, "plugin_runtime", lambda: {"status": "disabled", "rootDir": str(plugin)})
    elif not loaded:
        monkeypatch.setattr(openclaw, "plugin_runtime", lambda: {})
    monkeypatch.setattr(doctor, "_check_antigravity_quota", lambda _s: (quota.append(1), 0)[1] if quota is not None else 0)
    probes: list[str] = []
    monkeypatch.setattr(openclaw, "backend_registered", lambda b: (probes.append(b), backend)[1])
    s = state.SetupState()
    s.openclaw.plugin_linked = linked
    s.openclaw.antigravity_applied = applied
    if check is not None:
        s.openclaw.stale_plugin_check = check
    monkeypatch.setattr(ui, "require_deps", lambda: None)
    monkeypatch.setattr(ui, "console", lambda: _FakeConsole())
    monkeypatch.setattr(state, "load", lambda: s)
    lines: list[str] = []
    monkeypatch.setattr(ui, "warn", lines.append)
    monkeypatch.setattr(ui, "detail", lines.append)
    monkeypatch.setattr(ui, "ok", lines.append)
    doctor.cmd_doctor(argparse.Namespace(skip_smoke=True))
    return lines, probes


# --- the field (AC 2, 7, 8) -------------------------------------------------------------------

def test_the_field_defaults_to_linked():
    assert state.OpenClawState().stale_plugin_check == "linked"


def test_ac8_a_state_file_without_the_key_loads_as_linked(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    data = dataclasses.asdict(state.SetupState())
    del data["openclaw"]["stale_plugin_check"]
    path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(state, "state_path", lambda: path)
    assert state.load().openclaw.stale_plugin_check == "linked"


def test_a_saved_answer_round_trips(monkeypatch, tmp_path):
    data = dataclasses.asdict(state.SetupState())
    data["openclaw"]["stale_plugin_check"] = "off"
    assert state._from_dict(state.SetupState, data).openclaw.stale_plugin_check == "off"


# --- the question (AC 1, 2) -------------------------------------------------------------------

def test_ac1_the_question_offers_three_answers_and_records_the_pick(monkeypatch):
    seen: dict = {}

    def select(message, choices, default=None, instruction=""):
        seen.update(message=message, choices=choices, default=default, instruction=instruction)
        return "engine"

    monkeypatch.setattr(ui, "select", select)
    infos: list[str] = []
    monkeypatch.setattr(ui, "detail", infos.append)
    monkeypatch.setattr(ui, "info", infos.append)
    monkeypatch.setattr(ui, "warn", infos.append)
    s = state.SetupState()
    openclaw._prompt_stale_check(s)
    assert [c.value for c in seen["choices"]] == ["linked", "engine", "off"]
    assert seen["default"] == "linked"
    assert s.openclaw.stale_plugin_check == "engine"
    # the operator must be told the default can turn a silent doctor into a failing one
    text = " ".join(infos) + " " + seen["message"] + " " + seen["instruction"]
    assert "exit" in text and "non-zero" in text


def test_the_question_defaults_to_the_saved_answer(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(ui, "select", lambda m, c, default=None, instruction="": seen.setdefault("d", default))
    monkeypatch.setattr(ui, "detail", lambda *_a: None)
    s = state.SetupState()
    s.openclaw.stale_plugin_check = "off"
    openclaw._prompt_stale_check(s)
    assert seen["d"] == "off"


def test_ac2_non_interactive_keeps_the_default_without_a_saved_answer(monkeypatch):
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", True)
    monkeypatch.setattr(ui, "detail", lambda *_a: None)
    s = state.SetupState()
    openclaw._prompt_stale_check(s)
    assert s.openclaw.stale_plugin_check == "linked"


def test_ac2_non_interactive_uses_the_saved_answer(monkeypatch):
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", True)
    monkeypatch.setattr(ui, "detail", lambda *_a: None)
    s = state.SetupState()
    s.openclaw.stale_plugin_check = "engine"
    openclaw._prompt_stale_check(s)
    assert s.openclaw.stale_plugin_check == "engine"


def test_an_unknown_saved_value_falls_back_to_the_default(monkeypatch):
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", True)
    monkeypatch.setattr(ui, "detail", lambda *_a: None)
    s = state.SetupState()
    s.openclaw.stale_plugin_check = "bogus"
    openclaw._prompt_stale_check(s)
    assert s.openclaw.stale_plugin_check == "linked"


# --- the gate (AC 3-7) ------------------------------------------------------------------------

def test_ac3_linked_reports_a_stale_plugin_on_the_measured_engine_keep_host(monkeypatch, tmp_path):
    """Measured live: engine keep, plugin_linked False, antigravity_applied False, the plugin loaded
    in the gateway, time rule tripping. A gate reading `s.openclaw.plugin_linked` stays silent here."""
    lines, _ = run_doctor(monkeypatch, tmp_path, check="linked", loaded=True, linked=False, applied=False)
    assert TIME_MSG in lines
    assert "Run: openclaw gateway restart" in lines


def test_ac3_linked_says_nothing_when_the_runtime_cannot_be_read(monkeypatch, tmp_path):
    """No answer from `plugins inspect` is no evidence: a warning must not rest on a guess, even
    when the state file says the kit linked the plugin."""
    lines, probes = run_doctor(monkeypatch, tmp_path, check="linked", loaded=False, linked=True, applied=False)
    assert not any("OpenClaw" in ln for ln in lines)
    assert probes == []


def test_ac3_linked_says_nothing_when_the_gateway_has_not_loaded_the_plugin(monkeypatch, tmp_path):
    lines, _ = run_doctor(monkeypatch, tmp_path, check="linked", loaded="disabled", applied=False)
    assert TIME_MSG not in lines


def test_ac3_the_default_answer_reports_it_too(monkeypatch, tmp_path):
    lines, _ = run_doctor(monkeypatch, tmp_path, check=None, applied=False)
    assert TIME_MSG in lines


def test_linked_says_nothing_when_the_gateway_does_not_have_the_plugin(monkeypatch, tmp_path):
    lines, probes = run_doctor(monkeypatch, tmp_path, check="linked", loaded=False, applied=False)
    assert not any("OpenClaw" in ln for ln in lines)
    assert probes == []


def test_ac4_engine_reports_when_antigravity_is_applied(monkeypatch, tmp_path):
    lines, _ = run_doctor(monkeypatch, tmp_path, check="engine", applied=True)
    assert TIME_MSG in lines


def test_ac4_engine_says_nothing_on_an_engine_keep_host(monkeypatch, tmp_path):
    lines, probes = run_doctor(monkeypatch, tmp_path, check="engine", applied=False)
    assert not any("OpenClaw" in ln for ln in lines)
    assert probes == []


def test_ac4_engine_keeps_the_backend_arm_for_antigravity(monkeypatch, tmp_path):
    lines, probes = run_doctor(monkeypatch, tmp_path, check="engine", applied=True,
                               stale="none", backend=False)
    assert BACKEND_MSG in lines
    assert probes == ["agy-cli"]


@pytest.mark.parametrize("linked", [True, False])
def test_ac5_off_says_nothing_whatever_the_state(monkeypatch, tmp_path, linked):
    lines, probes = run_doctor(monkeypatch, tmp_path, check="off", linked=linked, applied=True)
    assert not any("OpenClaw" in ln for ln in lines)
    assert probes == []


def test_ac6_the_path_rule_wording_is_unchanged(monkeypatch, tmp_path):
    lines, _ = run_doctor(monkeypatch, tmp_path, check="linked", applied=False, stale="path")
    assert PATH_MSG in lines
    assert not any("kit on disk" in ln for ln in lines)


def test_ac7_the_backend_arm_never_fires_for_an_engine_keep_host(monkeypatch, tmp_path):
    lines, probes = run_doctor(monkeypatch, tmp_path, check="linked", applied=False,
                               stale="none", backend=False)
    assert BACKEND_MSG not in lines
    assert probes == []


def test_linked_and_fresh_on_an_engine_keep_host_prints_no_success_line_about_agy_cli(monkeypatch, tmp_path):
    lines, _ = run_doctor(monkeypatch, tmp_path, check="linked", applied=False, stale="none")
    assert not any("agy-cli" in ln for ln in lines)


@pytest.mark.parametrize("check", ["linked", "engine", "off"])
@pytest.mark.parametrize("applied", [True, False])
def test_the_quota_check_runs_exactly_when_antigravity_is_applied_whatever_the_answer(
        monkeypatch, tmp_path, check, applied):
    """4d is about the engine, not the plugin: moving it out of the 4c gate must not change when it runs."""
    calls: list[int] = []
    run_doctor(monkeypatch, tmp_path, check=check, applied=applied, quota=calls)
    assert bool(calls) is applied

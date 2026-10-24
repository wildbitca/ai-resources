"""The default path speaks the glossary (ADR-0009): tool, role, team, starting team, subscription, gateway.

A subscription operator reaches Apply in four screens and never reads cockpit, lane, preset, backend or engine on the
way (More options, Custom and the gateway path keep the old words). Offline; the ui is the scripted fake."""
from __future__ import annotations

import argparse
import re
import types

import pytest

from ai_resources.setup import detection, model_selection as ms, state, tools, ui, wizard
from wizard_fakes import Script, host_env

BANNED = re.compile(r"\b(cockpits?|lanes?|presets?|backends?|engines?)\b", re.I)
REPO_TOOLS = ("agy", "claude", "codex")


def _args(**kw):
    ns = dict(dry_run=False, yes=False, non_interactive=False, profile="", lanes=False, no_lanes=False,
              lanes_dry_run=False, lanes_orchestrator=[], allow_unverified=False, backend="", models="",
              shape="", providers="", smoke_path="", cockpits="")
    ns.update(kw)
    return argparse.Namespace(**ns)


def test_a_subscription_run_reaches_apply_in_four_screens_without_the_old_words(monkeypatch, tmp_path):
    real_offer = tools.offer
    host_env(monkeypatch, tmp_path)
    monkeypatch.setattr(tools, "offer", real_offer)
    monkeypatch.setattr(wizard, "_PRESET", wizard._PRESET)
    cockpits = {cid: types.SimpleNamespace(installed=True, version="1.0", binary_path=f"/b/{cid}", name=cid)
                for cid in REPO_TOOLS}
    monkeypatch.setattr(detection, "detect_all_cockpits", lambda: dict(cockpits))
    monkeypatch.setattr(detection, "agy_installed", lambda: True)
    found = {t: detection.Detected(t, t in REPO_TOOLS, "1.0", f"/b/{t}") for t in tools.TOOLS}
    monkeypatch.setattr(tools, "_detect_tools", lambda: found)
    monkeypatch.setattr(tools, "install_one", lambda *a, **k: (_ for _ in ()).throw(AssertionError("installed")))
    log: list[str] = []
    monkeypatch.setattr(ui, "section", lambda n, t, title: log.append(f"[{n}/{t}] {title}"))
    monkeypatch.setattr(ui, "review_table", lambda rows, title="": log.extend(f"{k}: {v}" for k, v in rows))
    monkeypatch.setattr(ui, "role_table", lambda rows, title="": log.append(title))
    monkeypatch.setattr(ui, "team_table", lambda rows, title="": log.extend(" ".join(r) for r in rows))
    script = Script(**{"Tick the tools": ["claude", "codex"], "Sign in now": [],
                       "Starting team": "balanced", "Apply this?": ms.APPLY})
    script.install(monkeypatch)
    assert wizard.run(_args()) == 0
    assert [m.split(":")[0] for m in script.asked] == ["select", "checkbox", "checkbox", "select", "select", "select"]   # run mode first; + the S14 sign-in list for codex
    assert [x for x in log if x.startswith("[")] == ["[1/5] Your tools", "[2/5] Main tool",
                                                     "[2/5] Sign-in", "[3/5] Your team", "[4/5] Audio",
                                                     "[5/5] Review"]
    text = "\n".join(script.asked + script.shown + script.titles + log)
    assert not BANNED.findall(text), BANNED.findall(text)


def test_each_on_its_own_asks_the_same_screens(monkeypatch, tmp_path):
    real_offer = tools.offer
    host_env(monkeypatch, tmp_path)
    monkeypatch.setattr(tools, "offer", real_offer)
    monkeypatch.setattr(wizard, "_PRESET", wizard._PRESET)
    monkeypatch.setattr(detection, "detect_all_cockpits", lambda: {
        "claude": types.SimpleNamespace(installed=True, version="1.0", binary_path="/b/claude", name="Claude Code")})
    found = {t: detection.Detected(t, t == "claude", "1.0", "/b/claude") for t in tools.TOOLS}
    monkeypatch.setattr(tools, "_detect_tools", lambda: found)
    monkeypatch.setattr(tools, "install_one", lambda *a, **k: (_ for _ in ()).throw(AssertionError("installed")))
    log: list[str] = []
    monkeypatch.setattr(ui, "section", lambda n, t, title: log.append(f"[{n}/{t}] {title}"))
    monkeypatch.setattr(ui, "review_table", lambda rows, title="": log.extend(f"{k}: {v}" for k, v in rows))
    monkeypatch.setattr(ui, "role_table", lambda rows, title="": log.append(title))
    script = Script(**{"Tick the tools": ["claude"], "Starting team": "balanced",
                       "Apply this?": ms.APPLY})
    script.install(monkeypatch)
    assert wizard.run(_args()) == 0
    assert [m.split(":")[0] for m in script.asked] == ["select", "checkbox", "select", "select", "select"]
    text = "\n".join(script.asked + script.shown + script.titles + log)
    assert not BANNED.findall(text), BANNED.findall(text)


def test_the_screens_are_numbered_out_of_four_while_the_ten_steps_stay():
    assert wizard.TOTAL_STEPS == 10 and wizard.PHASES == 5
    assert [wizard._phase_of(n) for n in range(1, 11)] == [1, 3, 3, 3, 3, 3, 3, 5, 5, 5]


def test_the_banner_and_the_docstring_describe_the_flow_without_the_old_words():
    assert "9-step" not in wizard.__doc__ and "LiteLLM" not in wizard.BANNER_SUBTITLE
    assert not BANNED.findall(wizard.BANNER_SUBTITLE)


def test_the_completion_banner_lists_a_line_for_every_configured_tool(monkeypatch):
    seen = {}
    monkeypatch.setattr(ui, "banner", lambda title, subtitle="", version="": seen.update(sub=subtitle))
    monkeypatch.setattr(ui, "console", lambda: types.SimpleNamespace(print=lambda *a, **k: None))
    s = state.SetupState()
    monkeypatch.setattr(wizard, "_APPLIED_TARGETS", ["claude", "codex", "cursor", "aider"])
    wizard._print_completion(s)
    sub = seen["sub"]
    for binary, label in (("claude", "Claude Code"), ("codex", "Codex CLI"), ("agent", "Cursor Agent CLI"),
                          ("aider", "Aider")):
        assert re.search(rf"^\s+{binary}\s+open {re.escape(label)}$", sub, re.M), (binary, sub)
    assert sub.index("claude") > sub.index("Try") and "ai-resources doctor" in sub
    assert not BANNED.findall(sub)

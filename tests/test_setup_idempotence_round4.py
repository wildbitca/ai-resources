"""ADR-0016 (AC-17): `ai-resources setup` run twice through `cli.cmd_setup` on a scratch HOME with stubs.

Live OpenClaw is all-Anthropic while the saved state says engine=antigravity; the operator's CLAUDE.md holds the five rule
blocks; there is no ANTHROPIC_API_KEY and no gate evidence. The first run asks, queues and applies; the second changes
nothing. No real `claude`, `openclaw` or `agy` runs, no gateway is restarted, no secret or email reaches the kit state, and
no path under the operator's real HOME is written."""
from __future__ import annotations

import argparse
import json
import re
import types

import pytest
import yaml

from ai_resources import cli
from ai_resources.setup import (cockpit_auth, credentials, detection, hygiene, live_auth, model_selection as ms,
                                operator_rules, state, tools, ui, wizard)
from ai_resources.setup.cockpits import _openclaw_host as section, claude, openclaw
from test_openclaw_host_wizard import ANSWERS, GATES, sim  # noqa: F401  (fixture)
from wizard_fakes import Script

EMAIL = "operator@example.invalid"
KEY = "sk-ant-api03-ROUND4-SENTINEL-0123456789abcdef"
STAMP = re.compile(r"\d{4}-\d\d-\d\dT[0-9:.+]+")


def block(title, extra=""):
    return f"## {title}\n\n" + (f"Regla de {title.lower()}: seguir siempre, con acentos y eñes. " * 12) + extra + "\n\n"


FIVE = (block("IDIOMA — REGLA GLOBAL") + block("COMMITS — REGLA GLOBAL") + block("CLICKUP — REGLAS GLOBALES")
        + block("PREGUNTAS AL USUARIO — REGLA GLOBAL") + block("CLUSTERS — SOLO WILDBIT"))


def tree(root) -> dict:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.name.endswith(".lock"):
            data = p.read_bytes()
            if p.name == "setup-state.yaml":
                data = STAMP.sub("<ts>", data.decode("utf-8")).encode("utf-8")
            out[str(p)] = data
    return out


def _args():
    return argparse.Namespace(dry_run=False, yes=False, non_interactive=False, profile="", lanes=False, no_lanes=False,
                              lanes_dry_run=False, lanes_orchestrator=[], allow_unverified=False, backend="", models="",
                              shape="", providers="", smoke_path="", cockpits="", restore_hygiene=False,
                              restore_agy_mcp=False, restore_claude_md=False)


@pytest.fixture
def world(sim, monkeypatch):
    claude.CLAUDE_MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    claude.CLAUDE_MD_PATH.write_text("# mine\n\nkeep this line\n\n" + FIVE, encoding="utf-8")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)          # no key: Review can never say "API key"
    monkeypatch.setattr(hygiene, "plan", lambda s, **k: [])
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", False)
    monkeypatch.setattr(ui, "require_deps", lambda: None)
    monkeypatch.setattr(ui, "banner", lambda *a, **k: None)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(ui, "detected_table", lambda rows: None)
    monkeypatch.setattr(ui, "review_table", lambda rows, title="": None)
    monkeypatch.setattr(detection, "detect_all_cockpits", lambda: {
        "claude": types.SimpleNamespace(installed=True, version="2.0", binary_path="/opt/claude", name="Claude Code"),
        "openclaw": types.SimpleNamespace(installed=True, version="2026.10", binary_path="/opt/openclaw",
                                          name="OpenClaw")})
    monkeypatch.setattr(tools, "offer", lambda *a, **k: [])
    monkeypatch.setattr(credentials, "get_key", lambda *a, **k: "")
    monkeypatch.setattr(credentials, "get_claude_code_keychain_key", lambda: None)
    monkeypatch.setattr(wizard, "_step10_verify", lambda s, t: 0)
    monkeypatch.setattr(wizard, "_print_completion", lambda s: None)
    # the OpenClaw host section and voice have their own suites; here the engine, the auth and the rules matter
    monkeypatch.setattr(openclaw.voice, "prompt", lambda s, **k: None)
    monkeypatch.setattr(section, "prompt", lambda s, doc: None)
    monkeypatch.setattr(openclaw.image, "prompt", lambda *a, **k: None)
    handed = []
    monkeypatch.setattr(live_auth, "handoff", lambda argv: handed.append(list(argv)) or 0)
    saved = state.SetupState()
    saved.last_run = "2026-10-20T00:00:00+00:00"
    saved.profile.name = "balanced"
    saved.openclaw.engine = "antigravity"                           # a previous run chose antigravity
    saved.openclaw.model = "gemini-3.8-flash-low"
    state.config_root().mkdir(parents=True, exist_ok=True)
    state.save(saved)
    return types.SimpleNamespace(sim=sim, home=sim.home, handed=handed, claude_root=claude.CONFIG_ROOT)


def answers():
    base = {**ANSWERS, **GATES}
    base.update({
        "Use previous answers": True, "Claude Code sign-in": "keep", "OpenClaw sign-in": "keep",
        "Starting team": "balanced", "Apply this?": ms.APPLY, "Configure detected cockpits": lambda v: list(v),
        "Which engine should OpenClaw": "keep", "Model for": lambda v: v[0],
        "Create ": True, "what should happen to this block": "keep", "standing rules": True,
    })
    return base


def run(monkeypatch):
    script = Script(**answers())
    script.install(monkeypatch)
    return cli.cmd_setup(_args()), script


def snapshot(world):
    return {"home": tree(world.home), "claude": tree(world.claude_root), "rules": operator_rules.path().read_bytes(),
            "oc": world.sim.cfg.read_bytes(), "patches": len(world.sim.oc.real_patches())}


def test_two_runs_second_is_a_no_op_and_nothing_leaks(world, monkeypatch, capsys):
    rc, script = run(monkeypatch)
    out = capsys.readouterr()
    assert rc == 0, out.err + out.out
    asked = " | ".join(script.asked)
    for question in ("Claude Code sign-in:", "OpenClaw sign-in:", "Create ", "Apply this?"):
        assert question in asked, question                      # the run really went through the new steps
    auth = state.load().cockpit_auth
    assert auth["claude"]["live"] == "subscription" and auth["claude"]["method"] == "subscription"   # never "api_key"
    # D2: the live OpenClaw is not repointed at antigravity
    assert not any("agy-cli" in json.dumps(p) for _a, p in world.sim.oc.real_patches() if p)
    assert state.load().openclaw.engine in ("keep", "")
    # D4/D6: the rules file exists once, and CLAUDE.md carries the managed block with the rules
    assert operator_rules.exists() and operator_rules.read().count("# Operator rules") == 1
    md = claude.CLAUDE_MD_PATH.read_text(encoding="utf-8")
    assert "keep this line" in md and "## Operator rules" in md
    # D1: no native flow ran; the kit state has no key and no email
    assert world.handed == []
    text = state.state_path().read_text(encoding="utf-8")
    assert KEY not in text and EMAIL not in text and not re.search(r"sk-[A-Za-z0-9_-]{8,}", text)
    assert cockpit_auth.secret_findings(state._to_dict(state.load())["cockpit_auth"]) == []
    # D3: no gate evidence, so no agy lane can read "on"
    from ai_resources import agy_gate_evidence as record_module
    assert record_module.load().status == "absent"

    first = snapshot(world)
    first_state = yaml.safe_load(text)

    rc2, _script2 = run(monkeypatch)
    out2 = capsys.readouterr()
    assert rc2 == 0, out2.err + out2.out
    second = snapshot(world)
    assert first["home"] == second["home"]
    assert first["claude"] == second["claude"] and first["rules"] == second["rules"] and first["oc"] == second["oc"]
    assert first["patches"] == second["patches"], "the second run sent an OpenClaw config patch"
    second_state = yaml.safe_load(state.state_path().read_text(encoding="utf-8"))
    for doc in (first_state, second_state):
        doc.pop("last_run", None)
    assert STAMP.sub("<ts>", json.dumps(first_state, sort_keys=True)) == STAMP.sub("<ts>", json.dumps(second_state, sort_keys=True))
    assert world.handed == []
    calls = [a for a, _p in world.sim.oc.calls]
    assert not [c for c in calls if "restart" in c or c[:1] == ["gateway"]]

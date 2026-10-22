"""AC-18: a full setup run twice on a scratch HOME and scratch OpenClaw state leaves the second run with no diff and no
gateway restart (ADR-0003). Nothing here reaches the operator's real ~/.claude, ~/.openclaw or a gateway."""
from __future__ import annotations

import json
import pathlib

import pytest

from ai_resources.setup import profiles, state
from ai_resources.setup.cockpits import _shared, claude, openclaw
from test_openclaw_host_wizard import _run_wizard, _state, script, sim  # noqa: F401  (fixtures)


def _tree(root: pathlib.Path) -> dict[str, bytes]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() or p.is_symlink():
            out[str(p.relative_to(root))] = (str(p.readlink()).encode() if p.is_symlink() else p.read_bytes())
    return out


# --- Claude Code ------------------------------------------------------------------------------------------------

def test_claude_cockpit_configure_twice_changes_nothing_the_second_time(tmp_path, monkeypatch):
    root = tmp_path / "claude"
    for attr, rel in (("CONFIG_ROOT", ""), ("SETTINGS_PATH", "settings.json"), ("CLAUDE_MD_PATH", "CLAUDE.md"),
                      ("AGENTS_DIR", "agents"), ("WORKFLOWS_DIR", "workflows")):
        monkeypatch.setattr(claude, attr, root / rel if rel else root)
    monkeypatch.setattr(claude, "_install_engram_plugin", lambda: False)
    monkeypatch.delenv("AI_RESOURCES_SKILLS", raising=False)
    kit = tmp_path / "kit"
    (kit / "skills").mkdir(parents=True)
    for name in ("workflow-bugfix", "kit-orchestration", "flutter-bloc", "common-tdd"):
        (kit / "skills" / name).mkdir()
        (kit / "skills" / name / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\n")
    monkeypatch.setattr(_shared, "stable_kit_root", lambda _root: kit)
    (root).mkdir()
    (root / "settings.json").write_text(json.dumps({"env": {"CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"}}))
    (root / "CLAUDE.md").write_text("# mine\n\n- keep this\n")
    s = state.SetupState()
    ex = profiles.to_executors(profiles.load_profile("claude-native"))
    ctx = {"state": s, "executors": ex, "master_key": "", "gateway_url": ""}
    claude.configure(ctx)
    first = _tree(root)
    claude.configure(ctx)
    assert _tree(root) == first
    env = json.loads((root / "settings.json").read_text())["env"]
    assert "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" not in env and env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "200000"
    assert "keep this" in (root / "CLAUDE.md").read_text()
    # only the lean include list is linked
    linked = {p.name for p in (root / "skills").iterdir()}
    assert linked == {"workflow-bugfix", "kit-orchestration", "common-tdd"}


# --- OpenClaw host -----------------------------------------------------------------------------------------------

def _restarts(sim) -> list:
    calls = [c for c in sim.systemd.calls if "restart" in c]
    calls += [a for a, _p in sim.oc.calls if "restart" in a]
    return calls


def test_openclaw_setup_twice_second_run_is_a_no_op_and_nothing_restarts(sim, script):
    sim.overrides_file.unlink()                      # fill-only, as on a real host
    doc = json.loads(sim.cfg.read_text(encoding="utf-8"))
    doc["agents"]["defaults"]["thinkingDefault"] = "medium"
    doc["agents"]["entries"]["main"]["thinkingDefault"] = "high"
    doc["agents"]["entries"]["main"]["model"] = {"primary": "anthropic/claude-sonnet-5-5",
                                                  "fallbacks": ["anthropic/claude-opus-5-5"]}
    sim.cfg.write_text(json.dumps(doc), encoding="utf-8")
    script.answers["Replace agents."] = True
    s = _state()
    _run_wizard(s)
    after_first = sim.cfg.read_bytes()
    patches_after_first = len(sim.oc.real_patches())
    state_first = state._to_dict(s)["openclaw"]
    _run_wizard(s)
    assert sim.cfg.read_bytes() == after_first
    assert len(sim.oc.real_patches()) == patches_after_first          # the second run sent no patch at all
    assert state._to_dict(s)["openclaw"]["cost_migrations_applied"] == state_first["cost_migrations_applied"]
    assert _restarts(sim) == []


def test_unattended_setup_applies_no_cost_migration_and_restarts_nothing(sim, script):
    sim.overrides_file.unlink()
    script.non_interactive = True
    doc = json.loads(sim.cfg.read_text(encoding="utf-8"))
    doc["agents"]["entries"]["main"]["thinkingDefault"] = "high"
    sim.cfg.write_text(json.dumps(doc), encoding="utf-8")
    s = _state()
    _run_wizard(s)
    live = json.loads(sim.cfg.read_text(encoding="utf-8"))
    assert live["agents"]["entries"]["main"]["thinkingDefault"] == "high"
    assert s.openclaw.cost_migrations_applied == [] and _restarts(sim) == []

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


# --- Round 2 (ADR-0014): the new writers are idempotent together -------------------------------------------------------

def test_claude_cockpit_round_2_second_run_has_zero_diff_and_registers_the_sleep_guard_once(tmp_path, monkeypatch):
    root = tmp_path / "claude"
    for attr, rel in (("CONFIG_ROOT", ""), ("SETTINGS_PATH", "settings.json"), ("CLAUDE_MD_PATH", "CLAUDE.md"),
                      ("AGENTS_DIR", "agents"), ("WORKFLOWS_DIR", "workflows")):
        monkeypatch.setattr(claude, attr, root / rel if rel else root)
    monkeypatch.setattr(claude, "_install_engram_plugin", lambda: False)
    monkeypatch.setenv("AI_RESOURCES_SKILLS", "angular")
    root.mkdir()
    (root / "CLAUDE.md").write_text("## Mine\n\nkeep this\n")
    s = state.SetupState()
    ctx = {"state": s, "executors": profiles.to_executors(profiles.load_profile("balanced", "single-model")),
           "master_key": "", "gateway_url": ""}
    claude.configure(ctx)
    first = _tree(root)
    claude.configure(ctx)
    assert _tree(root) == first
    hooks = json.loads((root / "settings.json").read_text())["hooks"]["PreToolUse"]
    guards = [h for entry in hooks for h in entry["hooks"] if "kit_sleep_guard.py" in h["command"]]
    assert len(guards) == 1 and [e for e in hooks if any("kit_sleep_guard.py" in h["command"] for h in e["hooks"])][0]["matcher"] == "Bash"
    agents = {p.stem for p in (root / "agents").glob("*.md")}
    assert "planner-angular" in agents and not {a for a in agents if a.endswith(("-symfony", "-devops", "-dart-flutter"))}
    assert s.tracking.persona_domains_active == ["angular"]
    # the routing matrix (ADR-0019 D2), not the legacy by_role table, reached the subagent files
    from ai_resources import generate
    for role in ("tester", "software-architect"):
        assert f"model: {generate.role_claude_model(role)}" in (root / "agents" / f"{role}.md").read_text(), role
    assert "keep this" in (root / "CLAUDE.md").read_text()


def test_hygiene_claude_md_and_agy_mcp_second_run_changes_and_asks_nothing(tmp_path, monkeypatch):
    import json as _json
    from datetime import datetime, timedelta, timezone
    from ai_resources import billing, usage_index
    from ai_resources.setup import agy_mcp, claude_md_migrate as cmm, hygiene, ui

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setattr(claude, "CLAUDE_MD_PATH", home / ".claude" / "CLAUDE.md")
    monkeypatch.setattr(claude, "CONFIG_ROOT", home / ".claude")
    monkeypatch.setattr(claude, "SETTINGS_PATH", home / ".claude" / "settings.json")
    claude_json = home / ".claude.json"
    claude_json.write_text(_json.dumps({"mcpServers": {"grafana": {"type": "stdio", "command": "npx", "args": ["g"]}}}))
    (home / ".claude" / "settings.json").write_text(_json.dumps({"enabledPlugins": {"idle@m": True}}))
    big = "## Big block\n\n" + ("regla importante. " * 120) + "\n"
    (home / ".claude" / "CLAUDE.md").write_text(big)
    mcp = home / ".gemini" / "config" / "mcp_config.json"
    mcp.parent.mkdir(parents=True)
    mcp.write_text(_json.dumps({"mcpServers": {"mine": {"command": "m"}}}))
    monkeypatch.setattr(billing, "claude_json_path", lambda: claude_json)
    monkeypatch.setattr(usage_index, "default_roots", lambda: [])
    monkeypatch.setattr(hygiene, "backup_dir", lambda: home / "private")
    monkeypatch.setattr(agy_mcp, "mcp_path", lambda: mcp)
    monkeypatch.setattr(hygiene, "is_headless", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(ui, "checkbox", lambda *a, **k: [])
    monkeypatch.setattr(ui, "select", lambda msg, choices, default=None, **k: "skill" if "Big block" in msg else default)
    monkeypatch.setattr(ui, "text", lambda msg, default="", validate=None: default)
    removed = []

    def runner(argv):
        removed.append(argv)
        if argv[:3] == ["claude", "mcp", "remove"]:
            _json.dump({"mcpServers": {}}, open(claude_json, "w"))
        return 0, ""

    s = state.SetupState()
    s.profile.customizations = {"__targets__": {"selected": ["claude", "agy"]}}
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)

    def one_run():
        hygiene.run_step(s, runner=runner, now=now)
        cmm.run_step(s, now=now)
        agy_mcp.run_step(s, candidates={}, now=now)

    one_run()
    snapshot = {str(p.relative_to(home)): p.read_bytes() for p in sorted(home.rglob("*")) if p.is_file()}
    rec = (list(s.tracking.hygiene_disabled), list(s.tracking.claude_md_migrations), dict(s.tracking.agy_mcp_added))
    calls = len(removed)
    assert len(s.tracking.hygiene_disabled) == 2 and len(s.tracking.claude_md_migrations) == 1 and "engram" in s.tracking.agy_mcp_added

    monkeypatch.setattr(ui, "confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the second run asks nothing")))
    monkeypatch.setattr(ui, "select", lambda *a, **k: (_ for _ in ()).throw(AssertionError("the second run asks nothing")))
    one_run()
    assert {str(p.relative_to(home)): p.read_bytes() for p in sorted(home.rglob("*")) if p.is_file()} == snapshot
    assert (list(s.tracking.hygiene_disabled), list(s.tracking.claude_md_migrations), dict(s.tracking.agy_mcp_added)) == rec
    assert len(removed) == calls

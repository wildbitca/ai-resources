"""Every cockpit setup configures passes its row of the round 1-2 improvement matrix (ADR-0015 S5, acceptance 16).

The matrix is one table: improvement -> cockpit -> a check over what that cockpit receives. A cockpit that is not selected
gets none of it (its files are untouched). Offline: every path is a scratch path, no host command runs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_resources import openclaw_host as host
from ai_resources.setup import profiles, state
from ai_resources.setup.cockpits import _shared, claude, cursor, gemini

WAITING = "Never block one tool call on a long wait"
COCKPITS = ("claude", "openclaw", "agy", "cursor")


@pytest.fixture
def rendered(tmp_path, monkeypatch):
    """What each cockpit wrote, under scratch paths: {cockpit: {"text": str, "files": {name: Path}}}."""
    cl = tmp_path / "claude"
    for attr, sub in (("SETTINGS_PATH", "settings.json"), ("CLAUDE_MD_PATH", "CLAUDE.md"), ("AGENTS_DIR", "agents"),
                      ("WORKFLOWS_DIR", "workflows")):
        monkeypatch.setattr(claude, attr, cl / sub)
    monkeypatch.setattr(claude, "CONFIG_ROOT", cl)
    monkeypatch.setattr(claude, "_install_engram_plugin", lambda: False)
    monkeypatch.setattr(claude, "_trim_engram_hooks", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(_shared, "link_agents_skills", lambda *a, **k: None)
    monkeypatch.setattr(_shared, "sync_skill_links", lambda *a, **k: {"removed": [], "skipped": [], "added": []})
    monkeypatch.setattr(claude, "_install_workflow_scripts", lambda *a, **k: ([], []))
    g = tmp_path / "g"
    monkeypatch.setattr(gemini, "AGY_MCP_PATH", g / "config" / "mcp_config.json")
    monkeypatch.setattr(gemini, "SETTINGS_PATH", g / "settings.json")
    monkeypatch.setattr(gemini, "GEMINI_MD_PATH", g / "GEMINI.md")
    cu = tmp_path / "cursor"
    monkeypatch.setattr(cursor, "INSTRUCTIONS_PATH", cu / "AGENT_KIT.md")
    monkeypatch.setattr(cursor, "MCP_PATH", cu / "mcp.json")
    monkeypatch.setattr(host.Path, "home", classmethod(lambda cls: tmp_path))

    s = state.SetupState()
    s.profile.name = "balanced"
    ex = profiles.to_executors(profiles.load_profile("balanced", "single-model"))
    claude.configure({"state": s, "executors": ex, "master_key": "", "gateway_url": ""})
    gemini.configure({"state": s})
    cursor.configure({"state": s})
    built = host.build_host_patch(host.load_host_profile(), {}, {"DOMAIN": "ai.example.org", "POD_CIDR": "10.9.0.0/24",
                                                                 "HOME": "/home/u"})
    return {
        "claude": {"text": (cl / "CLAUDE.md").read_text(), "settings": json.loads((cl / "settings.json").read_text())},
        "agy": {"text": (g / "GEMINI.md").read_text(), "mcp": json.loads((g / "config" / "mcp_config.json").read_text())},
        "cursor": {"text": (cu / "AGENT_KIT.md").read_text()},
        "openclaw": {"text": _shared.openclaw_agent_kit_md("/kit", delegates=True),
                     "paths": [".".join(c["path"]) for c in built["changes"]]},
    }


# improvement -> {cockpit: check(rendered[cockpit]) -> bool}; a cockpit absent from a row has nothing to receive.
MATRIX = {
    "waiting guidance in the instruction file": {
        "claude": lambda r: WAITING in r["text"],
        "agy": lambda r: WAITING in r["text"],
        "cursor": lambda r: WAITING in r["text"],
        "openclaw": lambda r: "Waiting without polling" in r["text"],
    },
    "lean instruction block (no multi-model section on the subscription path)": {
        "claude": lambda r: "Multi-model routing" not in r["text"] and len(r["text"]) < 4000,
        "agy": lambda r: "Multi-model routing" not in r["text"] and len(r["text"]) < 4000,
        "cursor": lambda r: "Multi-model routing" not in r["text"] and len(r["text"]) < 4000,
        "openclaw": lambda r: len(r["text"]) < 6000,
    },
    "auto-compact window and cost env": {
        "claude": lambda r: r["settings"]["env"].get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") == "200000",
    },
    "kit sleep guard hook (polling guard)": {
        "claude": lambda r: "kit_sleep_guard.py" in json.dumps(r["settings"].get("hooks", {})),
    },
    "engram MCP only if absent (backup and record)": {
        "agy": lambda r: "engram" in r["mcp"]["mcpServers"],
        "claude": lambda r: "engram" in r["settings"]["mcpServers"],
        "cursor": lambda r: True,       # Cursor keeps its current behaviour (ADR-0015 section 10): mcp.json merge, unchanged
    },
    "lean OpenClaw host keys (thinking, heartbeat, utility model, pruning, subagent limits)": {
        "openclaw": lambda r: all(any(p.startswith(k) for p in r["paths"]) for k in (
            "agents.defaults.thinkingDefault", "agents.defaults.utilityModel", "agents.defaults.contextPruning",
            "agents.entries.main.heartbeat.lightContext")),
    },
}


@pytest.mark.parametrize("improvement", sorted(MATRIX))
@pytest.mark.parametrize("cockpit", COCKPITS)
def test_the_cockpit_passes_its_row(rendered, improvement, cockpit):
    check = MATRIX[improvement].get(cockpit)
    if check is None:
        pytest.skip(f"{improvement}: nothing to receive on {cockpit}")
    assert check(rendered[cockpit]), (improvement, cockpit)


def test_every_cockpit_has_at_least_one_row():
    for cockpit in COCKPITS:
        assert any(cockpit in row for row in MATRIX.values()), cockpit


def test_an_unselected_cockpit_gets_no_file(tmp_path, monkeypatch):
    """Only the configured cockpit writes: the others' instruction files do not appear."""
    g = tmp_path / "g"
    monkeypatch.setattr(gemini, "AGY_MCP_PATH", g / "config" / "mcp_config.json")
    monkeypatch.setattr(gemini, "SETTINGS_PATH", g / "settings.json")
    monkeypatch.setattr(gemini, "GEMINI_MD_PATH", g / "GEMINI.md")
    cu = tmp_path / "cursor"
    monkeypatch.setattr(cursor, "INSTRUCTIONS_PATH", cu / "AGENT_KIT.md")
    monkeypatch.setattr(cursor, "MCP_PATH", cu / "mcp.json")
    monkeypatch.setattr(_shared, "link_agents_skills", lambda *a, **k: None)
    gemini.configure({"state": state.SetupState()})
    assert (g / "GEMINI.md").is_file() and not cu.exists()
    assert not (tmp_path / "claude").exists()


def test_the_single_waiting_text_is_shared_by_claude_and_the_other_cockpits():
    assert claude.POLLING_GUIDANCE == _shared.WAITING_GUIDANCE
    assert _shared.WAITING_GUIDANCE in _shared.kit_instructions_md("X", "/kit", "", "single-model", native_skills=True)
    assert _shared.WAITING_GUIDANCE not in _shared.kit_instructions_md("X", "/kit", "", "single-model",
                                                                      native_skills=True, waiting=False)

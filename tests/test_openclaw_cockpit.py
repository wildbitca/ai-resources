"""OpenClaw cockpit: engine choice, the openclaw.json patch, and teardown.

No OpenClaw binary is needed: `_openclaw` is replaced by a recorder, so these
tests pin the patch the kit hands to `openclaw config patch`, not OpenClaw's
own validation of it. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources.setup import state, ui  # noqa: E402
from ai_resources.setup.cockpits import ALL, openclaw  # noqa: E402

KIT_SKILLS = "/kit/skills"


def _state(*installed: str) -> state.SetupState:
    s = state.SetupState()
    for cid in installed:
        s.cockpits[cid] = state.CockpitState(installed=True)
    return s


class _Recorder:
    """Stands in for the openclaw binary and keeps every patch it was given."""

    def __init__(self, config: pathlib.Path):
        self.config = config
        self.calls: list[tuple[list[str], dict | None]] = []

    def __call__(self, args, stdin=None, timeout=120):
        self.calls.append((args, json.loads(stdin) if stdin else None))
        if args[:2] == ["config", "file"]:
            return 0, str(self.config)
        return 0, "ok"

    def patches(self):
        return [p for a, p in self.calls if a[:2] == ["config", "patch"]]


@pytest.fixture
def jarvis(tmp_path, monkeypatch):
    """A config shaped like a real bot: OpenRouter auto, Engram already declared."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("# Jarvis rules\n", encoding="utf-8")
    cfg = tmp_path / "openclaw.json"
    cfg.write_text(json.dumps({
        "agents": {"defaults": {
            "workspace": str(ws),
            "model": {"primary": "openrouter/auto"},
            "models": {"openrouter/auto": {"alias": "OpenRouter"}},
        }},
        "mcp": {"servers": {"engram": {"command": "/usr/bin/engram", "args": ["mcp"]}}},
    }), encoding="utf-8")
    rec = _Recorder(cfg)
    monkeypatch.delenv("OPENCLAW_CONFIG_PATH", raising=False)
    monkeypatch.setattr(openclaw, "_openclaw", rec)
    monkeypatch.setattr(openclaw._shared, "stable_kit_root", lambda _root: pathlib.Path("/kit"))
    monkeypatch.setattr(ui, "confirm", lambda *_a, **_k: False)
    return cfg, ws, rec


def test_openclaw_is_registered_last_so_its_engines_are_configured_first():
    assert list(ALL)[-1] == "openclaw"


def test_only_engines_with_an_installed_cli_are_offered():
    ids = [e.id for e in openclaw.available_engines(_state("claude"))]
    assert ids == ["claude-code", "direct", "keep"]


def test_an_unattended_first_run_never_repoints_a_live_bot(monkeypatch):
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    assert openclaw.default_engine(_state("claude")) == "keep"


def test_an_interactive_run_proposes_claude_code_when_installed(monkeypatch):
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    assert openclaw.default_engine(_state("claude")) == "claude-code"


def test_claude_code_engine_pins_the_model_to_the_claude_cli_runtime():
    doc = {"mcp": {"servers": {"engram": {"command": "engram"}}}}
    patch = openclaw.build_patch(openclaw.ENGINES["claude-code"], "anthropic/claude-sonnet-5",
                                 doc, KIT_SKILLS, "/bin/engram")
    assert patch["agents"]["defaults"]["model"] == {"primary": "anthropic/claude-sonnet-5"}
    assert patch["agents"]["defaults"]["models"]["anthropic/claude-sonnet-5"] == {
        "agentRuntime": {"id": "claude-cli"}}


def test_engram_is_added_when_missing_because_cli_runtimes_run_with_strict_mcp_config():
    patch = openclaw.build_patch(openclaw.ENGINES["claude-code"], "anthropic/claude-sonnet-5",
                                 {}, KIT_SKILLS, "/bin/engram")
    assert patch["mcp"]["servers"]["engram"]["command"] == "/bin/engram"


def test_an_existing_engram_server_is_left_alone():
    doc = {"mcp": {"servers": {"engram": {"command": "/custom/engram"}}}}
    patch = openclaw.build_patch(openclaw.ENGINES["claude-code"], "anthropic/claude-sonnet-5",
                                 doc, KIT_SKILLS, "/bin/engram")
    assert "mcp" not in patch


def test_direct_engine_adds_kit_skills_and_cli_engines_take_them_back_out():
    direct = openclaw.build_patch(openclaw.ENGINES["direct"], "openrouter/auto",
                                  {"skills": {"load": {"extraDirs": ["/mine"]}}}, KIT_SKILLS, "e")
    assert direct["skills"]["load"]["extraDirs"] == ["/mine", KIT_SKILLS]

    back = openclaw.build_patch(openclaw.ENGINES["claude-code"], "anthropic/claude-sonnet-5",
                                {"skills": {"load": {"extraDirs": ["/mine", KIT_SKILLS]}}},
                                KIT_SKILLS, "e")
    assert back["skills"]["load"]["extraDirs"] == ["/mine"]


def test_a_runtime_pin_from_a_previous_engine_is_cleared():
    doc = {"agents": {"defaults": {"models": {
        "google/gemini-3.7-flash": {"agentRuntime": {"id": "google-gemini-cli"}},
        "openrouter/auto": {"alias": "OpenRouter"},
    }}}}
    patch = openclaw.build_patch(openclaw.ENGINES["claude-code"], "anthropic/claude-sonnet-5",
                                 doc, KIT_SKILLS, "e")
    models = patch["agents"]["defaults"]["models"]
    assert models["google/gemini-3.7-flash"] == {"agentRuntime": None}
    assert "openrouter/auto" not in models


def test_configure_goes_through_openclaw_and_snapshots_what_it_replaces(jarvis):
    cfg, ws, rec = jarvis
    s = _state("claude", "openclaw")
    s.openclaw.engine, s.openclaw.model = "claude-code", "anthropic/claude-sonnet-5"

    openclaw.configure({"state": s})

    assert len(rec.patches()) == 1
    assert s.openclaw.applied
    assert s.openclaw.previous["model"] == {"primary": "openrouter/auto"}
    assert json.loads(cfg.read_text())["agents"]["defaults"]["model"]["primary"] == "openrouter/auto", \
        "the kit must never write openclaw.json itself"
    text = (ws / "AGENTS.md").read_text()
    assert "Engram is the memory of record" in text and "# Jarvis rules" in text
    assert "## Using the kit" not in text, "Claude Code already loads the kit block from CLAUDE.md"


def test_a_second_run_keeps_the_original_snapshot(jarvis):
    _cfg, _ws, _rec = jarvis
    s = _state("claude", "openclaw")
    s.openclaw.engine = "claude-code"
    openclaw.configure({"state": s})
    first = dict(s.openclaw.previous)
    s.openclaw.engine = "direct"
    openclaw.configure({"state": s})
    assert s.openclaw.previous == first


def test_direct_engine_writes_the_kit_block_into_the_workspace_agents_md(jarvis):
    _cfg, ws, _rec = jarvis
    s = _state("openclaw")
    s.openclaw.engine = "direct"
    openclaw.configure({"state": s})
    text = (ws / "AGENTS.md").read_text()
    assert "## Using the kit" in text and "# Jarvis rules" in text
    assert "Engram is the memory of record" in text


def test_a_rejected_patch_is_not_recorded_as_applied(jarvis, monkeypatch):
    _cfg, _ws, rec = jarvis
    monkeypatch.setattr(openclaw, "apply_patch", lambda *_a, **_k: (False, "invalid"))
    s = _state("claude", "openclaw")
    s.openclaw.engine = "claude-code"
    openclaw.configure({"state": s})
    assert not s.openclaw.applied


def test_teardown_restores_the_snapshot_and_removes_the_kit_block(jarvis):
    _cfg, ws, rec = jarvis
    s = _state("openclaw")
    s.openclaw.engine = "direct"
    openclaw.configure({"state": s})

    openclaw.teardown(s)

    restore = rec.patches()[-1]
    assert restore["agents"]["defaults"]["model"] == {"primary": "openrouter/auto"}
    assert "mcp" not in restore, "Engram existed before the kit and must survive teardown"
    assert "ai-resources" not in (ws / "AGENTS.md").read_text()
    assert not s.openclaw.applied


def test_openclaw_state_survives_a_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "state_path", lambda: tmp_path / "setup-state.yaml")
    s = state.SetupState()
    s.openclaw.engine, s.openclaw.applied = "claude-code", True
    s.openclaw.previous = {"model": {"primary": "openrouter/auto"}}
    state.save(s)
    loaded = state.load()
    assert isinstance(loaded.openclaw, state.OpenClawState)
    assert loaded.openclaw.engine == "claude-code"
    assert loaded.openclaw.previous == {"model": {"primary": "openrouter/auto"}}


def test_every_engine_makes_engram_the_memory_of_record(jarvis):
    _cfg, ws, _rec = jarvis
    for engine in ("claude-code", "direct"):
        s = _state("claude", "openclaw")
        s.openclaw.engine = engine
        openclaw.configure({"state": s})
        assert "Engram is the memory of record" in (ws / "AGENTS.md").read_text(), engine


def test_the_chosen_model_joins_an_existing_allowlist():
    doc = {"agents": {"defaults": {"modelPolicy": {"allow": ["openrouter/auto"]}}}}
    patch = openclaw.build_patch(openclaw.ENGINES["claude-code"], "anthropic/claude-sonnet-5",
                                 doc, KIT_SKILLS, "e")
    assert patch["agents"]["defaults"]["modelPolicy"] == {
        "allow": ["openrouter/auto", "anthropic/claude-sonnet-5"]}


@pytest.mark.parametrize("doc", [
    {},
    {"agents": {"defaults": {"modelPolicy": {"allow": ["anthropic/*"]}}}},
])
def test_no_allowlist_change_when_absent_or_already_covered(doc):
    patch = openclaw.build_patch(openclaw.ENGINES["claude-code"], "anthropic/claude-sonnet-5",
                                 doc, KIT_SKILLS, "e")
    assert "modelPolicy" not in patch["agents"]["defaults"]

"""ADR-0014 D3.4 / AC-16: the agy MCP file holds `engram` by default, opt-in servers only on a pick, merge-preserving,
fill-only, backed up, recorded, removed by teardown only where it still equals what the kit wrote; Flash-low only when agy is active
(installed and signed in)."""
from __future__ import annotations

import json
import types
from datetime import datetime, timezone

import pytest

from ai_resources import billing
from ai_resources.setup import agy_mcp, hygiene, state, ui, wizard
from ai_resources.setup.cockpits import _shared, gemini, openclaw

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
ENGRAM = {"command": "engram", "args": ["mcp"]}
MINE = {"command": "my-mcp", "args": ["--x"], "env": {"TOKEN": "keep-me"}}


@pytest.fixture
def mcp(tmp_path):
    p = tmp_path / "gemini" / "config" / "mcp_config.json"
    p.parent.mkdir(parents=True)
    return p


def _s(agy=True):
    s = state.SetupState()
    s.profile.customizations = {"__targets__": {"selected": ["claude", "agy"] if agy else ["claude"]}}
    return s


def _doc(p):
    return json.loads(p.read_text())


# --- the default: engram only -----------------------------------------------------------------------------------------

def test_an_absent_file_gets_engram_only_and_the_default_adds_nothing_else(mcp, monkeypatch):
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(ui, "checkbox", lambda *a, **k: [])
    monkeypatch.setattr(hygiene, "is_headless", lambda: False)
    s = _s()
    assert agy_mcp.run_step(s, path=mcp, candidates={"grafana": {"command": "g"}}, now=NOW) is True
    assert _doc(mcp) == {"mcpServers": {"engram": ENGRAM}}
    assert s.tracking.agy_mcp_added == {"engram": ENGRAM}
    assert list(mcp.parent.glob("*.ai-resources-backup*")) == []            # nothing existed, nothing to back up


def test_the_picks_are_opt_in_and_default_to_none(mcp, monkeypatch):
    asked = []
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(hygiene, "is_headless", lambda: False)

    def checkbox(message, choices, default=None, **k):
        asked.append((message, [c.value for c in choices], default))
        return []
    monkeypatch.setattr(ui, "checkbox", checkbox)
    agy_mcp.run_step(_s(), path=mcp, candidates={"grafana": {"command": "g", "args": []}, "figma": {"command": "f", "args": []}},
                     now=NOW)
    assert asked == [(asked[0][0], ["figma", "grafana"], [])] and "tool schemas" in asked[0][0]
    assert set(_doc(mcp)["mcpServers"]) == {"engram"}


def test_a_picked_server_is_added_and_an_unpicked_one_is_not(mcp, monkeypatch):
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(ui, "checkbox", lambda *a, **k: ["grafana"])
    monkeypatch.setattr(hygiene, "is_headless", lambda: False)
    s = _s()
    cands = {"grafana": {"command": "g", "args": ["-y"]}, "figma": {"command": "f", "args": []}}
    agy_mcp.run_step(s, path=mcp, candidates=cands, now=NOW)
    assert set(_doc(mcp)["mcpServers"]) == {"engram", "grafana"} and set(s.tracking.agy_mcp_added) == {"engram", "grafana"}


def test_declining_engram_writes_nothing(mcp, monkeypatch):
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: False)
    monkeypatch.setattr(ui, "checkbox", lambda *a, **k: [])
    monkeypatch.setattr(hygiene, "is_headless", lambda: False)
    s = _s()
    assert agy_mcp.run_step(s, path=mcp, candidates={}, now=NOW) is False
    assert not mcp.exists() and s.tracking.agy_mcp_added == {}


# --- merge-preserving, fill-only, backup ------------------------------------------------------------------------------

def test_the_operators_servers_and_keys_survive_byte_for_value_and_a_backup_is_made(mcp, monkeypatch):
    original = {"mcpServers": {"mine": MINE, "engram": {"command": "engram-custom", "args": ["x"]}}, "theme": {"a": [1, 2]}}
    mcp.write_text(json.dumps(original, indent=1))
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(ui, "checkbox", lambda *a, **k: ["grafana"])
    monkeypatch.setattr(hygiene, "is_headless", lambda: False)
    s = _s()
    agy_mcp.run_step(s, path=mcp, candidates={"grafana": {"command": "g", "args": []}}, now=NOW)
    doc = _doc(mcp)
    assert doc["mcpServers"]["mine"] == MINE and doc["theme"] == {"a": [1, 2]}
    assert doc["mcpServers"]["engram"] == {"command": "engram-custom", "args": ["x"]}      # fill-only: never replaced
    assert "grafana" in doc["mcpServers"] and s.tracking.agy_mcp_added == {"grafana": {"command": "g", "args": []}}
    (backup,) = mcp.parent.glob("mcp_config.json.ai-resources-backup-*")
    assert json.loads(backup.read_text()) == original


def test_no_question_when_everything_is_already_there(mcp, monkeypatch):
    mcp.write_text(json.dumps({"mcpServers": {"engram": ENGRAM}}))
    before = mcp.read_bytes()
    monkeypatch.setattr(hygiene, "is_headless", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no question")))
    monkeypatch.setattr(ui, "checkbox", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no question")))
    assert agy_mcp.run_step(_s(), path=mcp, candidates={}, now=NOW) is False
    assert mcp.read_bytes() == before


def test_a_second_run_changes_nothing(mcp, monkeypatch):
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(ui, "checkbox", lambda *a, **k: ["grafana"])
    monkeypatch.setattr(hygiene, "is_headless", lambda: False)
    s = _s()
    cands = {"grafana": {"command": "g", "args": []}}
    agy_mcp.run_step(s, path=mcp, candidates=cands, now=NOW)
    after = mcp.read_bytes()
    agy_mcp.run_step(s, path=mcp, candidates=cands, now=NOW)
    assert mcp.read_bytes() == after and len(list(mcp.parent.glob("*.ai-resources-backup*"))) == 0


def test_a_file_that_is_not_a_json_object_is_left_alone(mcp, monkeypatch):
    mcp.write_text("{ not json")
    monkeypatch.setattr(hygiene, "is_headless", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    warned = []
    monkeypatch.setattr(ui, "warn", warned.append)
    assert agy_mcp.run_step(_s(), path=mcp, candidates={}, now=NOW) is False
    assert mcp.read_text() == "{ not json" and warned
    mcp.write_text("[1]")
    assert agy_mcp.run_step(_s(), path=mcp, candidates={}, now=NOW) is False and mcp.read_text() == "[1]"


def test_headless_and_dry_runs_write_nothing(mcp, monkeypatch):
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no question")))
    monkeypatch.setattr(hygiene, "is_headless", lambda: True)
    assert agy_mcp.run_step(_s(), path=mcp, candidates={"x": {"command": "x"}}, now=NOW) is False
    monkeypatch.setattr(hygiene, "is_headless", lambda: False)
    assert agy_mcp.run_step(_s(), dry_run=True, path=mcp, candidates={"x": {"command": "x"}}, now=NOW) is False
    assert not mcp.exists()


def test_not_asked_when_agy_is_not_in_use(mcp, monkeypatch):
    monkeypatch.setattr(hygiene, "is_headless", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no question")))
    assert agy_mcp.run_step(_s(agy=False), path=mcp, candidates={}, now=NOW) is False
    spec = next(sp for sp in wizard._build_steps(False) if sp.id == "agy_mcp")
    assert spec.skip(types.SimpleNamespace(s=_s(agy=False), dry_run=False)) == ("agy MCP servers", "")


# --- candidates ---------------------------------------------------------------------------------------------------------

def test_only_plain_stdio_servers_without_env_are_offered(tmp_path):
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"mcpServers": {
        "ok": {"type": "stdio", "command": "npx", "args": ["-y", "x"], "env": {}},
        "bare": {"command": "tool"},
        "secret": {"type": "stdio", "command": "npx", "args": [], "env": {"API_KEY": "s3cret"}},
        "remote": {"type": "http", "url": "https://x.example/mcp"},
        "engram": {"type": "stdio", "command": "engram", "args": ["mcp"]},
        "ai-resources-lanes": {"type": "stdio", "command": "x"}}}))
    assert agy_mcp.portable_candidates(cj) == {"ok": {"command": "npx", "args": ["-y", "x"]}, "bare": {"command": "tool", "args": []}}
    assert "s3cret" not in json.dumps(agy_mcp.portable_candidates(cj))


# --- teardown ---------------------------------------------------------------------------------------------------------

def test_teardown_removes_only_what_still_equals_what_the_kit_wrote(mcp, monkeypatch):
    mcp.write_text(json.dumps({"mcpServers": {"mine": MINE}}))
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(ui, "checkbox", lambda *a, **k: ["grafana", "figma"])
    monkeypatch.setattr(hygiene, "is_headless", lambda: False)
    s = _s()
    cands = {"grafana": {"command": "g", "args": []}, "figma": {"command": "f", "args": []}}
    agy_mcp.run_step(s, path=mcp, candidates=cands, now=NOW)
    doc = _doc(mcp)
    doc["mcpServers"]["figma"]["args"] = ["edited-by-operator"]
    mcp.write_text(json.dumps(doc))
    removed = agy_mcp.remove_recorded(mcp, s.tracking)
    assert sorted(removed) == ["engram", "grafana"]
    live = _doc(mcp)["mcpServers"]
    assert set(live) == {"mine", "figma"} and live["mine"] == MINE and s.tracking.agy_mcp_added == {}


def test_teardown_with_nothing_recorded_touches_nothing(mcp):
    mcp.write_text(json.dumps({"mcpServers": {"engram": ENGRAM}}))
    before = mcp.read_bytes()
    assert agy_mcp.remove_recorded(mcp, state.InstallTracking()) == []
    assert mcp.read_bytes() == before


def test_the_gemini_cockpit_no_longer_merges_blindly(tmp_path, monkeypatch):
    p = tmp_path / "g" / "config" / "mcp_config.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"mcpServers": {"mine": MINE}}))
    monkeypatch.setattr(gemini, "AGY_MCP_PATH", p)
    monkeypatch.setattr(gemini, "SETTINGS_PATH", tmp_path / "g" / "settings.json")
    monkeypatch.setattr(gemini, "GEMINI_MD_PATH", tmp_path / "g" / "GEMINI.md")
    monkeypatch.setattr(_shared, "link_agents_skills", lambda ak: None)
    s = _s()
    gemini.configure({"state": s})
    assert _doc(p)["mcpServers"]["mine"] == MINE and "engram" in _doc(p)["mcpServers"]
    assert "engram" in s.tracking.agy_mcp_added and list(p.parent.glob("*.ai-resources-backup-*"))


# --- the hint and Flash-low -------------------------------------------------------------------------------------------

def test_the_hint_no_longer_claims_the_kit_never_writes_the_file():
    assert "never writes" not in openclaw.AGY_MCP_HINT and "never removes or edits" in openclaw.AGY_MCP_HINT
    assert "engram" in openclaw.AGY_MCP_HINT


def test_without_flash_low_the_other_entries_stay_and_the_other_entries_stay():
    with_low = openclaw._fallbacks_for("anthropic/claude-sonnet-5-5", "claude-code")
    without = openclaw._fallbacks_for("anthropic/claude-sonnet-5-5", "claude-code", flash_low=False)
    assert openclaw.AGY_ROUTINE_FALLBACK in with_low and openclaw.AGY_ROUTINE_FALLBACK not in without
    assert [c for c in with_low if c != openclaw.AGY_ROUTINE_FALLBACK] == without
    for engine, primary in (("antigravity", "gemini-3.8-flash-high"), ("antigravity", "claude-sonnet-5-5")):
        assert not any("flash-low" in c for c in openclaw._fallbacks_for(primary, engine, flash_low=False))


def test_build_patch_leaves_flash_low_out_of_every_chain_when_it_is_not_allowed():
    engine = openclaw.ENGINES["claude-code"]
    patch = openclaw.build_patch(engine, "anthropic/claude-sonnet-5-5", {}, "/kit/skills", "/bin/engram", fallbacks=True,
                                 flash_low=False)
    assert "flash-low" not in json.dumps(patch)
    patch = openclaw.build_patch(engine, "anthropic/claude-sonnet-5-5", {}, "/kit/skills", "/bin/engram", fallbacks=True)
    assert "flash-low" in json.dumps(patch)
    agy = openclaw.build_patch(openclaw.ENGINES["antigravity"], "gemini-3.8-flash-high", {}, "/kit/skills", "/bin/engram",
                               worker_model="claude-sonnet-5-5", ak_path="/kit", agy_bin="/bin/agy", claude_bin="/bin/claude",
                               python3_bin="/bin/python3", fallbacks=True, flash_low=False)
    assert "flash-low" not in json.dumps(agy)


def test_the_restore_command_removes_the_kits_servers_and_clears_the_record(mcp, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    mcp.write_text(json.dumps({"mcpServers": {"mine": MINE}}))
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(ui, "checkbox", lambda *a, **k: [])
    monkeypatch.setattr(hygiene, "is_headless", lambda: False)
    s = _s()
    agy_mcp.run_step(s, path=mcp, candidates={}, now=NOW)
    state.save(s)
    said = []
    assert agy_mcp.cmd_restore(path=mcp, say=said.append) == 0
    assert _doc(mcp)["mcpServers"] == {"mine": MINE} and state.load().tracking.agy_mcp_added == {}
    said.clear()
    assert agy_mcp.cmd_restore(path=mcp, say=said.append) == 0 and "Nothing to restore" in said[0]


def test_the_setup_flag_runs_the_restore_without_the_wizard(monkeypatch):
    from ai_resources import cli
    called = []
    monkeypatch.setattr(agy_mcp, "cmd_restore", lambda **k: called.append(1) or 0)
    monkeypatch.setattr(wizard, "run", lambda a: (_ for _ in ()).throw(AssertionError("the wizard must not run")))
    args = cli.build_parser().parse_args(["setup", "--restore-agy-mcp"])
    assert args.func(args) == 0 and called == [1]


def test_a_server_with_a_secret_looking_arg_is_not_offered(tmp_path):
    cj = tmp_path / "claude.json"
    cj.write_text(json.dumps({"mcpServers": {
        "flag": {"command": "srv", "args": ["--api-key=abc"]},
        "url": {"command": "pg", "args": ["postgres://user:pw@db/x"]},
        "prov": {"command": "srv", "args": ["sk-ant-123"]},
        "plain": {"command": "srv", "args": ["-y", "pkg"]},
    }}))
    assert set(agy_mcp.portable_candidates(cj)) == {"plain"}


def test_a_file_the_kit_creates_is_private(tmp_path):
    import stat
    from types import SimpleNamespace
    path = tmp_path / "config" / "mcp_config.json"
    agy_mcp.add_servers(path, {"engram": {"command": "engram", "args": ["mcp"]}}, SimpleNamespace(agy_mcp_added={}))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

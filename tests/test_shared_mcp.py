"""2.8.0: shared-token MCP servers (Sentry, Figma). One token in the kit's credential file, the same secret-free
`ai-resources mcp-run NAME` entry in Claude Code, OpenClaw and agy."""
from __future__ import annotations

import importlib
import json
import stat

import pytest
import yaml

from ai_resources import shared_mcp
from ai_resources.setup import agy_mcp, state, ui
from ai_resources.setup.cockpits import _openclaw_mcp as mcp

SECRET = "sntrys_" + "x" * 40
OAUTH = {"url": "https://mcp.sentry.dev/mcp", "auth": "oauth"}


def _state():
    s = state.SetupState()
    s.profile.customizations["__targets__"] = {"selected": ["agy"]}
    return s


def _runner(calls):
    def run(args, stdin=None, **_kw):
        calls.append((args, json.loads(stdin) if stdin else None))
        return 0, ""
    return run


def test_a_stored_token_lands_in_the_kit_credential_file_with_mode_0600():
    shared_mcp.store_token("SENTRY_ACCESS_TOKEN", SECRET)
    path = shared_mcp.tokens_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert shared_mcp.read_tokens() == {"SENTRY_ACCESS_TOKEN": SECRET}
    assert shared_mcp.configured() == ["sentry"]


@pytest.mark.parametrize("var,value", [("SENTRY_ACCESS_TOKEN", "a b"), ("SENTRY_ACCESS_TOKEN", "a\nX=1"),
                                       ("SENTRY_ACCESS_TOKEN", ""), ("PATH", "x")])
def test_a_malformed_token_or_a_foreign_variable_is_refused(var, value):
    with pytest.raises(ValueError):
        shared_mcp.store_token(var, value)
    assert shared_mcp.read_tokens() == {}


def test_the_entry_holds_no_secret_and_is_recognised_whatever_the_launcher_path():
    shared_mcp.store_token("FIGMA_API_KEY", SECRET)
    e = shared_mcp.entries()["figma"]
    assert e["args"] == ["mcp-run", "figma"] and SECRET not in json.dumps(e)
    assert shared_mcp.is_shared_entry({"command": "/opt/other/bin/ai-resources", "args": ["mcp-run", "figma"]})
    assert not shared_mcp.is_shared_entry({"command": "npx", "args": ["mcp-run", "figma"]})
    assert not shared_mcp.is_shared_entry(OAUTH)


def test_mcp_run_execs_the_pinned_package_with_the_token_only_in_its_environment(monkeypatch, capsys):
    shared_mcp.store_token("FIGMA_API_KEY", SECRET)
    monkeypatch.setattr(shared_mcp.shutil, "which", lambda n: "/usr/bin/npx" if n == "npx" else None)
    seen = {}

    def fake_exec(path, argv, env):
        seen.update(path=path, argv=argv, token=env.get("FIGMA_API_KEY"))
        raise SystemExit(0)
    monkeypatch.setattr(shared_mcp.os, "execvpe", fake_exec)
    with pytest.raises(SystemExit):
        shared_mcp.cmd_mcp_run("figma")
    assert seen["argv"] == ["/usr/bin/npx", "-y", "figma-developer-mcp@0.13.2", "--stdio"]
    assert seen["token"] == SECRET and SECRET not in " ".join(seen["argv"])
    assert SECRET not in capsys.readouterr().err


def test_mcp_run_without_a_token_says_how_to_add_one(monkeypatch, capsys):
    monkeypatch.delenv("SENTRY_ACCESS_TOKEN", raising=False)
    assert shared_mcp.cmd_mcp_run("sentry") == 1
    assert "ai-resources setup" in capsys.readouterr().err
    assert shared_mcp.cmd_mcp_run("nope") == 2


def test_setup_imports_a_token_the_host_has_without_a_question(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"SENTRY_ACCESS_TOKEN={SECRET}\n")
    importlib.reload(shared_mcp)   # the suite stubs the host lookup; this test reads only the file passed in
    monkeypatch.delenv("SENTRY_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(ui, "password", lambda *a, **k: pytest.fail("no question for a token already on the host"))
    s = _state()
    assert shared_mcp.collect(s, openclaw_env=env, in_use=set()) == ["sentry"]
    assert shared_mcp.read_tokens()["SENTRY_ACCESS_TOKEN"] == SECRET
    assert s.tracking.shared_mcp_asked == []


def test_setup_asks_once_only_for_a_server_a_cockpit_already_defines(monkeypatch):
    asked = []
    monkeypatch.setattr(ui, "password", lambda msg, *a, **k: asked.append(msg) or "")
    s = _state()
    assert shared_mcp.pending(s, in_use={"sentry"}) == ["sentry"]
    assert shared_mcp.collect(s, in_use={"sentry"}) == []
    assert len(asked) == 1 and "Sentry" in asked[0]
    assert s.tracking.shared_mcp_asked == ["sentry"]
    assert shared_mcp.pending(s, in_use={"sentry"}) == []
    shared_mcp.collect(s, in_use={"sentry"})
    assert len(asked) == 1


def test_a_dry_run_stores_nothing(monkeypatch):
    monkeypatch.setattr(ui, "password", lambda *a, **k: pytest.fail("a dry run never prompts"))
    s = _state()
    shared_mcp.collect(s, dry_run=True, in_use={"sentry", "figma"})
    assert shared_mcp.read_tokens() == {} and s.tracking.shared_mcp_asked == []


def test_openclaw_replaces_the_hosted_oauth_entry_and_teardown_restores_it():
    shared_mcp.store_token("SENTRY_ACCESS_TOKEN", SECRET)
    s, calls = _state(), []
    assert mcp.configure_shared(s, {"sentry": OAUTH}, _runner(calls)) is True
    written = calls[0][1]["mcp"]["servers"]["sentry"]
    assert shared_mcp.is_shared_entry(written) and SECRET not in json.dumps(calls)
    assert s.openclaw.mcp_shared["sentry"] == {"previous": OAUTH, "applied": written}
    assert mcp.configure_shared(s, {"sentry": written}, _runner(calls)) is False   # up to date: no second write
    assert mcp.teardown_shared(s, {"sentry": written}, _runner(calls)) is True
    assert calls[-1][1]["mcp"]["servers"]["sentry"] == OAUTH and s.openclaw.mcp_shared == {}


def test_openclaw_leaves_an_operator_definition_alone():
    shared_mcp.store_token("SENTRY_ACCESS_TOKEN", SECRET)
    s, calls = _state(), []
    mine = {"command": "my-sentry", "args": []}
    assert mcp.configure_shared(s, {"sentry": mine}, _runner(calls)) is False
    assert calls == [] and s.openclaw.mcp_shared == {}


def test_the_mirror_skips_a_shared_server_only_once_its_token_is_stored():
    src = [mcp.Source("sentry", "stdio", "settings.json", {"command": "x", "args": []})]
    assert mcp.plan(src, {}, {})[0].action == "add"
    shared_mcp.store_token("SENTRY_ACCESS_TOKEN", SECRET)
    item = mcp.plan(src, {}, {})[0]
    assert item.action == "skip" and "shared-token" in item.reason


def test_agy_gets_the_shared_servers_with_engram_and_no_extra_question(monkeypatch, tmp_path):
    shared_mcp.store_token("FIGMA_API_KEY", SECRET)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(ui, "checkbox", lambda *a, **k: pytest.fail("no checkbox: nothing else to offer"))
    from ai_resources.setup import hygiene
    monkeypatch.setattr(hygiene, "is_headless", lambda: False)
    path = tmp_path / "mcp_config.json"
    additions = agy_mcp.plan(_state(), path=path, candidates={})
    assert set(additions) == {"engram", "figma"}
    assert shared_mcp.is_shared_entry(additions["figma"])


def test_the_new_state_fields_are_absent_until_used():
    state.save(state.SetupState())
    doc = yaml.safe_load(state.state_path().read_text())
    assert "mcp_shared" not in doc.get("openclaw", {}) and "shared_mcp_asked" not in doc.get("tracking", {})

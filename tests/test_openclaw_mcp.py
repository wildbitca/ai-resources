"""Mirroring Claude Code's MCP servers into OpenClaw: collection, secret hygiene, the plan,
apply/adopt/no-overwrite, teardown and the setup question.

Every credential below is fake. `_openclaw` is a recorder, so nothing runs OpenClaw and
nothing touches the network. Run with:
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
from ai_resources.setup.cockpits import _openclaw_mcp as mcp, openclaw  # noqa: E402

# Split so secret scanners do not mistake the fixtures for real credentials.
FAKE_GRAFANA = "glsa" + "_FAKEfakeFAKEfake0123456789_abcdef"
FAKE_STRIPE = "sk_test" + "_FAKEfakeFAKEfake0123456789"
FAKE_GITHUB = "ghp" + "_FAKEfakeFAKEfake0123456789abcdefgh"
FAKE_SUPABASE = "sbp" + "_FAKEfakeFAKEfake0123456789abcdef"
FAKES = (FAKE_GRAFANA, FAKE_STRIPE, FAKE_GITHUB, FAKE_SUPABASE)

SENTRY = {"url": "https://mcp.sentry.dev/mcp", "transport": "streamable-http", "auth": "oauth"}
STRIPE = {"url": "https://mcp.stripe.com", "transport": "streamable-http", "auth": "oauth"}
CLICKUP = {"url": "https://mcp.clickup.com/mcp", "transport": "streamable-http", "auth": "oauth"}
SUPABASE_ADMIN = {"command": "npx", "args": ["-y", "@supabase/mcp-server-supabase@latest"],
                  "env": {"SUPABASE_ACCESS_TOKEN": "${SUPABASE_ACCESS_TOKEN}"}}


def _write(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def home(tmp_path):
    """A ~/.claude layout shaped like a real one, with fake secrets in the unsafe servers."""
    h = tmp_path / "home"
    _write(h / ".claude.json", {
        "mcpServers": {
            "sentry": {"type": "http", "url": "https://mcp.sentry.dev/mcp"},
            "grafana": {"type": "stdio", "command": "docker", "args": [
                "run", "--rm", "-i", "-e", "GRAFANA_URL=https://example.grafana.net",
                "-e", f"GRAFANA_SERVICE_ACCOUNT_TOKEN={FAKE_GRAFANA}", "mcp/grafana", "-t", "stdio"],
                "env": {}},
            "supabase-admin": {"type": "stdio", "command": "npx",
                               "args": ["-y", "@supabase/mcp-server-supabase@latest"],
                               "env": {"SUPABASE_ACCESS_TOKEN": "${SUPABASE_ACCESS_TOKEN}"}},
        },
        "claudeAiMcpEverConnected": ["claude.ai ClickUp", "claude.ai Claude Docs"],
        "projects": {"/work/app": {"mcpServers": {"project-only": {"command": "x"}}}},
    })
    _write(h / ".claude" / "settings.json", {
        "mcpServers": {"engram": {"command": "engram", "args": ["mcp"]}},
        "enabledPlugins": {"stripe@official": True, "disabled@official": False},
    })
    stripe_root = h / ".claude/plugins/cache/official/stripe/0.9.1"
    disabled_root = h / ".claude/plugins/cache/official/disabled/1.0.0"
    _write(stripe_root / ".mcp.json", {"mcpServers": {"stripe": {"type": "http",
                                                                 "url": "https://mcp.stripe.com"}}})
    _write(disabled_root / ".mcp.json", {"mcpServers": {"disabled-mcp": {"command": "nope"}}})
    _write(h / ".claude/plugins/installed_plugins.json", {"version": 2, "plugins": {
        "stripe@official": [{"scope": "user", "installPath": str(stripe_root)}],
        "disabled@official": [{"scope": "user", "installPath": str(disabled_root)}],
    }})
    return h


def _by_name(items):
    return {i.name: i for i in items}


# --- collection --------------------------------------------------------------------

def test_collector_reads_user_settings_enabled_plugins_and_connectors(home):
    sources = mcp.collect(home)
    got = {(s.name, s.kind) for s in sources}
    assert got == {
        ("sentry", "http"), ("grafana", "stdio"), ("supabase-admin", "stdio"),
        ("engram", "stdio"), ("stripe", "http"),
        ("clickup", "http"), ("claude.ai Claude Docs", "connector"),
    }
    origins = {s.name: s.origin for s in sources}
    assert origins["stripe"] == "plugin stripe@official"
    assert origins["engram"] == "~/.claude/settings.json"


def test_plugin_root_placeholder_is_expanded(home):
    root = home / ".claude/plugins/cache/official/stripe/0.9.1"
    _write(root / ".mcp.json", {"mcpServers": {"stripe": {
        "command": "${CLAUDE_PLUGIN_ROOT}/bin/server", "args": ["--root", "${CLAUDE_PLUGIN_ROOT}"]}}})
    src = _by_name(mcp.collect(home))["stripe"]
    assert src.spec["command"] == f"{root}/bin/server"
    assert src.spec["args"] == ["--root", str(root)]


def test_missing_files_collect_nothing(tmp_path):
    assert mcp.collect(tmp_path) == []


def test_claude_ai_clickup_connector_maps_to_the_public_endpoint(home):
    items = _by_name(mcp.plan(mcp.collect(home), {}, {}))
    assert items["clickup"].entry == CLICKUP
    assert items["clickup"].action == "add"
    docs = items["claude.ai Claude Docs"]
    assert docs.action == "skip" and "connector" in docs.reason


# --- secret hygiene ----------------------------------------------------------------

@pytest.mark.parametrize("spec, var", [
    ({"command": "docker", "args": ["run", "-e", f"GRAFANA_SERVICE_ACCOUNT_TOKEN={FAKE_GRAFANA}"]},
     "GRAFANA_SERVICE_ACCOUNT_TOKEN"),
    ({"command": "srv", "env": {"GITHUB_TOKEN": FAKE_GITHUB}}, "GITHUB_TOKEN"),
    ({"command": "srv", "env": {"SOME_SETTING": FAKE_STRIPE}}, "SOME_SETTING"),
    ({"command": "srv", "args": [f"--api-key={FAKE_STRIPE}"]}, "API_KEY"),
    ({"command": "srv", "args": ["--token", "plain-looking-value"]}, "DEMO_TOKEN"),
    ({"command": "srv", "args": [FAKE_SUPABASE]}, "DEMO_TOKEN"),
    ({"type": "http", "url": "https://x.example/mcp",
      "headers": {"Authorization": f"Bearer {FAKE_GITHUB}"}}, "DEMO_AUTHORIZATION"),
    ({"type": "http", "url": f"https://x.example/mcp?api_key={FAKE_STRIPE}"}, "API_KEY"),
])
def test_literal_secrets_are_never_mirrored(spec, var):
    kind = "http" if "url" in spec else "stdio"
    item = _by_name(mcp.plan([mcp.Source("demo", kind, "test", spec)], {}, {}))["demo"]
    assert item.action == "attention"
    assert item.entry is None
    assert var in item.reason
    assert not any(f in item.reason for f in FAKES)


def test_docker_env_secret_advice_names_the_variable_and_the_valueless_flag():
    spec = {"command": "docker", "args": ["run", "-e", f"GRAFANA_SERVICE_ACCOUNT_TOKEN={FAKE_GRAFANA}"]}
    item = mcp.plan([mcp.Source("grafana", "stdio", "test", spec)], {}, {})[0]
    assert "-e GRAFANA_SERVICE_ACCOUNT_TOKEN" in item.reason
    assert "${GRAFANA_SERVICE_ACCOUNT_TOKEN}" in item.reason


def test_env_references_and_harmless_literals_are_copied():
    spec = {"command": "docker", "args": ["run", "-e", "GRAFANA_URL=https://example.grafana.net",
                                          "-e", "GRAFANA_SERVICE_ACCOUNT_TOKEN"],
            "env": {"GRAFANA_SERVICE_ACCOUNT_TOKEN": "${GRAFANA_SERVICE_ACCOUNT_TOKEN}",
                    "LOG_LEVEL": "debug"}}
    item = mcp.plan([mcp.Source("grafana", "stdio", "test", spec)], {}, {})[0]
    assert item.action == "add"
    assert item.entry == {"command": "docker", "args": spec["args"], "env": spec["env"]}


def test_header_with_an_env_reference_is_mirrored_without_oauth():
    spec = {"type": "http", "url": "https://x.example/mcp",
            "headers": {"Authorization": "Bearer ${X_TOKEN}"}}
    item = mcp.plan([mcp.Source("x", "http", "test", spec)], {}, {})[0]
    assert item.entry == {"url": "https://x.example/mcp", "transport": "streamable-http",
                          "headers": {"Authorization": "Bearer ${X_TOKEN}"}}


def test_sse_servers_keep_their_transport():
    spec = {"type": "sse", "url": "https://x.example/sse"}
    item = mcp.plan([mcp.Source("x", "http", "test", spec)], {}, {})[0]
    assert item.entry["transport"] == "sse"


def test_env_references_are_listed_by_name():
    entries = [SUPABASE_ADMIN, {"command": "a", "args": ["--x", "${OTHER_VAR}"]}, SENTRY]
    assert mcp.env_refs(entries) == ["OTHER_VAR", "SUPABASE_ACCESS_TOKEN"]


# --- plan ----------------------------------------------------------------------------

def test_plan_on_a_real_shaped_home_against_a_bot_with_hand_added_servers(home):
    servers = {"engram": {"command": "/bin/engram", "args": ["mcp", "--tools=agent"]},
               "sentry": SENTRY, "stripe": STRIPE, "clickup": CLICKUP,
               "supabase-admin": {**SUPABASE_ADMIN, "args": ["-y", "--read-only"]}}
    items = _by_name(mcp.plan(mcp.collect(home), servers, {}))
    assert {n: i.action for n, i in items.items()} == {
        "sentry": "adopt", "stripe": "adopt", "clickup": "adopt",
        "grafana": "attention",
        "supabase-admin": "skip",
        "engram": "skip",
        "claude.ai Claude Docs": "skip",
    }
    assert "different" in items["supabase-admin"].reason
    assert "engine" in items["engram"].reason


def test_a_duplicate_name_keeps_the_first_source():
    first = mcp.Source("x", "http", "~/.claude.json", {"type": "http", "url": "https://a.example"})
    second = mcp.Source("x", "http", "plugin p@m", {"type": "http", "url": "https://b.example"})
    items = mcp.plan([first, second], {}, {})
    assert [i.action for i in items] == ["add", "skip"]
    assert "~/.claude.json" in items[1].reason


def test_kit_managed_servers_are_updated_but_hand_edits_are_respected():
    src = [mcp.Source("sentry", "http", "t", {"type": "http", "url": "https://new.example/mcp"})]
    record = {"sentry": {"previous": None, "applied": SENTRY}}
    assert mcp.plan(src, {"sentry": SENTRY}, record)[0].action == "update"

    edited = {**SENTRY, "requestTimeoutMs": 5000}
    item = mcp.plan(src, {"sentry": edited}, record)[0]
    assert item.action == "skip" and "by hand" in item.reason


def test_a_mirrored_server_gone_from_claude_code_is_removed():
    record = {"old": {"previous": None, "applied": SENTRY}}
    items = mcp.plan([], {"old": SENTRY}, record)
    assert [(i.name, i.action) for i in items] == [("old", "remove")]


def test_unchanged_kit_servers_need_no_write():
    src = [mcp.Source("sentry", "http", "t", {"type": "http", "url": SENTRY["url"]})]
    record = {"sentry": {"previous": None, "applied": SENTRY}}
    assert mcp.plan(src, {"sentry": SENTRY}, record)[0].action == "unchanged"


# --- apply and teardown ------------------------------------------------------------------

class _Recorder:
    def __init__(self, config: pathlib.Path, fail_first: int = 0):
        self.config = config
        self.fail_first = fail_first
        self.calls: list[tuple[list[str], dict | None]] = []

    def __call__(self, args, stdin=None, timeout=120):
        self.calls.append((args, json.loads(stdin) if stdin else None))
        if args[:2] == ["config", "file"]:
            return 0, str(self.config)
        if args[:2] == ["config", "patch"] and self.fail_first:
            self.fail_first -= 1
            return 1, "SQLite read-only worker for state did not stabilize"
        return 0, "ok"

    def patches(self):
        return [(a, p) for a, p in self.calls if a[:2] == ["config", "patch"]]


def test_apply_writes_new_servers_records_adoptions_and_never_touches_the_rest(tmp_path):
    rec = _Recorder(tmp_path / "openclaw.json")
    s = state.SetupState()
    items = [
        mcp.Item("clickup", "http", "t", "add", entry=CLICKUP),
        mcp.Item("stripe", "http", "t", "adopt", entry=STRIPE),
        mcp.Item("supabase-admin", "stdio", "t", "skip", reason="different"),
        mcp.Item("grafana", "stdio", "t", "attention", reason="secret"),
    ]
    assert mcp.apply(items, s, rec)

    [(args, patch)] = rec.patches()
    assert patch == {"mcp": {"servers": {"clickup": CLICKUP}}}
    assert args[args.index("--replace-path") + 1] == "mcp.servers.clickup"
    assert s.openclaw.mcp_mirrored == {
        "clickup": {"previous": None, "applied": CLICKUP},
        "stripe": {"previous": STRIPE, "applied": STRIPE},
    }


def test_apply_skips_servers_the_user_unticked(tmp_path):
    rec = _Recorder(tmp_path / "openclaw.json")
    s = state.SetupState()
    s.openclaw.mcp_skipped = ["clickup"]
    assert mcp.apply([mcp.Item("clickup", "http", "t", "add", entry=CLICKUP)], s, rec)
    assert rec.patches() == [] and s.openclaw.mcp_mirrored == {}


def test_apply_retries_when_openclaw_state_is_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp.time, "sleep", lambda _s: None)
    rec = _Recorder(tmp_path / "openclaw.json", fail_first=2)
    s = state.SetupState()
    assert mcp.apply([mcp.Item("clickup", "http", "t", "add", entry=CLICKUP)], s, rec)
    assert len(rec.patches()) == 3
    assert "clickup" in s.openclaw.mcp_mirrored


def test_a_rejected_apply_records_nothing(tmp_path):
    s = state.SetupState()
    ok = mcp.apply([mcp.Item("clickup", "http", "t", "add", entry=CLICKUP)], s,
                   lambda *_a, **_k: (1, "invalid config"))
    assert not ok and s.openclaw.mcp_mirrored == {}


def test_update_keeps_the_original_previous_and_remove_restores_it(tmp_path):
    rec = _Recorder(tmp_path / "openclaw.json")
    s = state.SetupState()
    hand = {**SENTRY, "requestTimeoutMs": 1}
    s.openclaw.mcp_mirrored = {"sentry": {"previous": hand, "applied": SENTRY}}
    newer = {**SENTRY, "url": "https://new.example/mcp"}
    mcp.apply([mcp.Item("sentry", "http", "t", "update", entry=newer)], s, rec)
    assert s.openclaw.mcp_mirrored["sentry"] == {"previous": hand, "applied": newer}

    mcp.apply([mcp.Item("sentry", "http", "t", "remove")], s, rec)
    assert rec.patches()[-1][1] == {"mcp": {"servers": {"sentry": hand}}}
    assert s.openclaw.mcp_mirrored == {}


def test_teardown_removes_added_servers_restores_replaced_ones_and_spares_hand_edits(tmp_path):
    rec = _Recorder(tmp_path / "openclaw.json")
    s = state.SetupState()
    s.openclaw.mcp_mirrored = {
        "clickup": {"previous": None, "applied": CLICKUP},
        "stripe": {"previous": STRIPE, "applied": STRIPE},
        "sentry": {"previous": None, "applied": SENTRY},
    }
    servers = {"clickup": CLICKUP, "stripe": STRIPE, "sentry": {**SENTRY, "enabled": False}}
    assert mcp.teardown(s, servers, rec)

    [(args, patch)] = rec.patches()
    assert patch == {"mcp": {"servers": {"clickup": None}}}, \
        "adopted servers stay as they were; a hand-edited one is left alone"
    assert s.openclaw.mcp_mirrored == {}


# --- env and login report --------------------------------------------------------------

def test_missing_gateway_env_is_reported_by_name_only(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path))
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=fake\n", encoding="utf-8")
    monkeypatch.setattr(mcp, "gateway_process_env_names", lambda: None)
    mcp.report_env([SUPABASE_ADMIN, {"command": "x", "env": {"OPENROUTER_API_KEY": "${OPENROUTER_API_KEY}"}}], {})
    out = capsys.readouterr()
    text = out.out + out.err
    assert "SUPABASE_ACCESS_TOKEN" in text and "OPENROUTER_API_KEY" not in text
    # rich wraps at the console width (80 columns off a TTY) and folds a long tmp path, so compare
    # with all whitespace removed rather than depending on where the lines happen to break.
    squashed = "".join(text.split())
    assert "".join(str(tmp_path / ".env").split()) in squashed
    assert "".join("openclaw gateway restart".split()) in squashed
    assert "fake" not in text


def test_login_commands_cover_oauth_servers_only():
    items = [mcp.Item("clickup", "http", "t", "add", entry=CLICKUP),
             mcp.Item("stripe", "http", "t", "adopt", entry=STRIPE),
             mcp.Item("x", "stdio", "t", "add", entry=SUPABASE_ADMIN)]
    assert mcp.login_commands(items) == ["openclaw mcp login clickup", "openclaw mcp login stripe"]


# --- setup question -------------------------------------------------------------------------

def test_an_unattended_first_run_keeps_the_bots_servers(monkeypatch):
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    assert mcp.default_answer(state.SetupState()) == "keep"


def test_a_saved_answer_wins(monkeypatch):
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    s = state.SetupState()
    s.openclaw.mcp = "mirror"
    assert mcp.default_answer(s) == "mirror"


def test_prompt_stores_unticked_servers(home, monkeypatch):
    monkeypatch.setattr(ui, "select", lambda *_a, **_k: "mirror")
    monkeypatch.setattr(ui, "checkbox", lambda _m, choices, default=None, **_k: ["sentry"])
    s = state.SetupState()
    mcp.prompt(s, {}, home)
    assert s.openclaw.mcp == "mirror"
    assert "sentry" not in s.openclaw.mcp_skipped
    assert {"clickup", "stripe", "supabase-admin"} <= set(s.openclaw.mcp_skipped)


def test_no_secret_value_reaches_any_output(home, monkeypatch, capsys, tmp_path):
    """The whole flow — preview, prompt, apply, report — never prints a credential."""
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(mcp, "gateway_process_env_names", lambda: set())
    seen: list[str] = []

    def select(message, choices, default=None, **_k):
        seen.append(message + repr([getattr(c, "title", c) for c in choices]))
        return "mirror"

    def checkbox(message, choices, default=None, **_k):
        seen.append(message + repr([getattr(c, "title", c) for c in choices]))
        return default

    monkeypatch.setattr(ui, "select", select)
    monkeypatch.setattr(ui, "checkbox", checkbox)
    s = state.SetupState()
    mcp.prompt(s, {}, home)
    rec = _Recorder(tmp_path / "openclaw.json")
    mcp.configure(s, {}, rec, home=home)

    out = capsys.readouterr()
    text = out.out + out.err + "".join(seen) + json.dumps([p for _a, p in rec.calls])
    assert "grafana" in text, "the unsafe server is reported"
    for fake in FAKES:
        assert fake not in text


def test_openclaw_prompt_asks_only_for_the_claude_code_engine(monkeypatch, tmp_path):
    asked: list[str] = []
    monkeypatch.setattr(openclaw, "_prompt_engine", lambda s, **_k: None)
    monkeypatch.setattr(openclaw.voice, "prompt", lambda s: None)
    monkeypatch.setattr(openclaw, "config_path", lambda: tmp_path / "missing.json")
    monkeypatch.setattr(mcp, "prompt", lambda s, servers, home=None: asked.append(s.openclaw.engine))
    # The host section asks its master question after the engine sections; answer it "no".
    host_asked: list[str] = []
    monkeypatch.setattr(ui, "confirm", lambda msg, default=False, **_k: host_asked.append(msg) or False)
    for engine in ("claude-code", "direct", "keep"):
        s = state.SetupState()
        s.openclaw.engine = engine
        openclaw.prompt(s)
        assert s.openclaw.host is False
    assert asked == ["claude-code"]
    assert len(host_asked) == 3 and all("OpenClaw host" in m for m in host_asked)


def test_mcp_state_survives_a_save_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr(state, "state_path", lambda: tmp_path / "setup-state.yaml")
    s = state.SetupState()
    s.openclaw.mcp, s.openclaw.mcp_skipped = "mirror", ["grafana"]
    s.openclaw.mcp_mirrored = {"clickup": {"previous": None, "applied": CLICKUP}}
    state.save(s)
    loaded = state.load().openclaw
    assert (loaded.mcp, loaded.mcp_skipped, loaded.mcp_mirrored) == (
        "mirror", ["grafana"], {"clickup": {"previous": None, "applied": CLICKUP}})


def test_cockpit_teardown_undoes_mirrored_servers(tmp_path, monkeypatch):
    cfg = tmp_path / "openclaw.json"
    cfg.write_text(json.dumps({"mcp": {"servers": {"clickup": CLICKUP}}}), encoding="utf-8")
    rec = _Recorder(cfg)
    monkeypatch.delenv("OPENCLAW_CONFIG_PATH", raising=False)
    monkeypatch.setattr(openclaw, "_openclaw", rec)
    monkeypatch.setattr(openclaw._shared, "stable_kit_root", lambda _root: pathlib.Path("/kit"))
    s = state.SetupState()
    s.openclaw.mcp_mirrored = {"clickup": {"previous": None, "applied": CLICKUP}}

    openclaw.teardown(s)

    assert rec.patches()[-1][1] == {"mcp": {"servers": {"clickup": None}}}
    assert s.openclaw.mcp_mirrored == {}


@pytest.mark.parametrize("engine, mirrored", [("claude-code", True), ("direct", False)])
def test_cockpit_configure_mirrors_only_for_the_claude_code_engine(engine, mirrored, home, tmp_path,
                                                                   monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    cfg = tmp_path / "openclaw.json"
    cfg.write_text(json.dumps({"agents": {"defaults": {"workspace": str(ws)}},
                               "mcp": {"servers": {"stripe": STRIPE}}}), encoding="utf-8")
    rec = _Recorder(cfg)
    monkeypatch.delenv("OPENCLAW_CONFIG_PATH", raising=False)
    monkeypatch.setenv("OPENCLAW_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(openclaw, "_openclaw", rec)
    monkeypatch.setattr(openclaw._shared, "stable_kit_root", lambda _root: pathlib.Path("/kit"))
    real_collect = mcp.collect
    monkeypatch.setattr(mcp, "collect", lambda _home=None: real_collect(home))
    monkeypatch.setattr(mcp, "gateway_process_env_names", lambda: None)
    s = state.SetupState()
    s.cockpits["claude"] = state.CockpitState(installed=True)
    s.openclaw.engine, s.openclaw.mcp = engine, "mirror"

    openclaw.configure({"state": s})

    servers = {n for _a, p in rec.patches() for n in ((p or {}).get("mcp") or {}).get("servers", {})}
    assert ("clickup" in servers) is mirrored
    assert ("stripe" in s.openclaw.mcp_mirrored) is mirrored, "identical hand-added server is adopted"
    assert "grafana" not in servers

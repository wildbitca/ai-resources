"""The OpenClaw writer for subscription lane agents (ADR-0007, plan P2-S5): fixture config, fake `openclaw`."""
from __future__ import annotations

import copy
import json
import shutil

import pytest

from ai_resources import openclaw_host
from ai_resources.setup import state
from ai_resources.setup.cockpits import openclaw as cockpit, openclaw_lanes

SONNET = "anthropic/claude-sonnet-5-5"
OPUS = "anthropic/claude-opus-5-5"
LANES = {
    "agents": {
        "kit-lane-cli-sonnet55": {"backend": "claude-cli", "model": SONNET, "pool": "claude-subscription"},
        "kit-lane-cli-opus55": {"backend": "claude-cli", "model": OPUS, "pool": "claude-subscription"},
    },
    "roles": {"implementer": {"candidates": ["openclaw-agent:kit-lane-cli-sonnet55", "claude-code"]},
              "verifier": {"candidates": ["claude-code", "openclaw-agent:kit-lane-cli-opus55"]}},
    "second": {},
    "orchestrators": ["main"],
    "verify_capability": {}, "pool_caps": {}, "max_remote_concurrency": 2, "workspace_root": "",
}


def _merge(dst, src):
    for k, v in src.items():
        if v is None:
            dst.pop(k, None)
        elif isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = copy.deepcopy(v)


class FakeOpenClaw:
    """`cockpit._openclaw` stand-in: a JSON config file plus `agents add|delete|list`. It records every call
    and refuses anything that would spawn a session or talk to the gateway."""

    def __init__(self, cfg_path, doc):
        self.path, self.calls = cfg_path, []
        self.path.write_text(json.dumps(doc, indent=2))

    def doc(self):
        return json.loads(self.path.read_text())

    def __call__(self, args, stdin=None, timeout=120):
        self.calls.append(list(args))
        head = args[0]
        assert head not in ("agent", "sessions", "message", "gateway", "chat"), args
        if args[:2] == ["config", "file"]:
            return 0, str(self.path)
        if args[:2] == ["config", "patch"]:
            if "--dry-run" in args:
                return 0, "ok"
            doc = self.doc()
            _merge(doc, json.loads(stdin))
            self.path.write_text(json.dumps(doc, indent=2))
            return 0, "patched"
        if args[:2] == ["agents", "list"]:
            return 0, json.dumps([{"id": i} for i in self.doc().get("agents", {}).get("entries", {})])
        if args[:2] == ["agents", "add"]:
            aid, ws = args[2], args[args.index("--workspace") + 1]
            model = args[args.index("--model") + 1] if "--model" in args else ""
            doc = self.doc()
            doc.setdefault("agents", {}).setdefault("entries", {})[aid] = {
                "workspace": ws, **({"model": {"primary": model}} if model else {})}
            self.path.write_text(json.dumps(doc, indent=2))
            return 0, "added"
        if args[:2] == ["agents", "delete"]:
            aid = args[2]
            doc = self.doc()
            ent = doc.get("agents", {}).get("entries", {}).pop(aid, None)
            self.path.write_text(json.dumps(doc, indent=2))
            if ent and ent.get("workspace"):
                shutil.rmtree(ent["workspace"], ignore_errors=True)
            return 0, "deleted"
        return 0, ""

    def verbs(self):
        return [tuple(c[:2]) for c in self.calls]


BASE = {
    "agents": {
        "defaults": {"model": {"primary": "agy-cli/gemini-3.8-flash-low"},
                     "subagents": {"allowAgents": ["claude"]}},
        "entries": {
            "main": {"name": "main", "model": {"primary": "claude-cli/claude-sonnet-5-5"},
                     "tools": {"exec": {"mode": "full"}}, "subagents": {"allowAgents": ["claude"]}},
            "other": {"name": "other", "subagents": {"allowAgents": ["claude"]}},
            "claude": {"name": "claude"},
        },
    },
}


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(tmp_path / "openclaw.json"))
    fake = FakeOpenClaw(tmp_path / "openclaw.json", copy.deepcopy(BASE))
    monkeypatch.setattr(cockpit, "_openclaw", fake)
    monkeypatch.setattr(cockpit.ui, "is_non_interactive", lambda: True)
    monkeypatch.setattr(cockpit.openclaw_host, "resolve_openclaw_bin", lambda: "openclaw")
    return fake


def _s(lanes=None):
    s = state.SetupState()
    s.profile.lanes = copy.deepcopy(LANES if lanes is None else lanes)
    return s


def _run(s, host, **kw):
    return cockpit._configure_lanes(s, host.doc(), dry_run=kw.pop("dry_run", False), run_changes=kw.pop("run_changes", []))


# --- scope ---------------------------------------------------------------------------------------------

def test_writes_only_kit_entries_catalog_and_allow_agents_on_named_orchestrators(host):
    s = _s()
    assert _run(s, host) is True
    after, before = host.doc(), BASE
    assert set(after["agents"]["entries"]) == set(before["agents"]["entries"]) | set(LANES["agents"])
    assert after["agents"]["defaults"]["subagents"] == before["agents"]["defaults"]["subagents"]
    assert after["agents"]["defaults"]["model"] == before["agents"]["defaults"]["model"]
    assert after["agents"]["entries"]["other"] == before["agents"]["entries"]["other"]
    assert after["agents"]["entries"]["claude"] == before["agents"]["entries"]["claude"]
    allow = after["agents"]["entries"]["main"]["subagents"]["allowAgents"]
    assert allow == ["claude", "kit-lane-cli-opus55", "kit-lane-cli-sonnet55"]
    models = after["agents"]["defaults"]["models"]
    assert models[SONNET] == {"agentRuntime": {"id": "claude-cli"}}
    assert models[OPUS] == {"agentRuntime": {"id": "claude-cli"}}
    entry = after["agents"]["entries"]["kit-lane-cli-sonnet55"]
    assert entry["model"]["primary"] == SONNET


def test_patch_is_fill_only_and_never_touches_channels(host):
    doc = host.doc()
    doc["agents"]["defaults"]["models"] = {SONNET: {"alias": "mine"}}
    host.path.write_text(json.dumps(doc))
    patch = openclaw_lanes.build_patch(LANES, doc, {i: host.path.parent / i for i in LANES["agents"]})
    assert SONNET not in patch["agents"]["defaults"]["models"]          # present: left alone
    assert "channels" not in json.dumps(patch)
    openclaw_host.assert_channels_safe(patch, [])
    assert "subagents" not in patch["agents"].get("defaults", {})        # agents.defaults.subagents untouched


def test_unnamed_agents_and_non_kit_ids_are_never_written(host):
    lanes = copy.deepcopy(LANES)
    lanes["agents"]["operator-agent"] = {"backend": "claude-cli", "model": SONNET, "pool": "claude-subscription"}
    s = _s(lanes)
    _run(s, host)
    assert "operator-agent" not in host.doc()["agents"]["entries"]
    assert "operator-agent" not in host.doc()["agents"]["entries"]["main"]["subagents"]["allowAgents"]


def test_dry_run_writes_nothing(host):
    s = _s()
    before = host.path.read_text()
    assert _run(s, host, dry_run=True) is False
    assert host.path.read_text() == before
    assert not any(c[:2] in (["agents", "add"], ["config", "patch"]) for c in host.calls)
    assert s.openclaw.lanes_applied == {}


def test_second_run_is_idempotent(host):
    s = _s()
    _run(s, host)
    snapshot = host.path.read_text()
    applied = copy.deepcopy(s.openclaw.lanes_applied)
    host.calls.clear()
    _run(s, host)
    assert host.path.read_text() == snapshot
    assert s.openclaw.lanes_applied == applied
    assert ("agents", "add") not in host.verbs()


def test_classified_before_agents_add_and_restart_required_is_deferred(host, monkeypatch):
    s = _s()
    order = []
    real = cockpit._lanes_classify
    monkeypatch.setattr(cockpit, "_lanes_classify", lambda p: (order.append("classify"), real(p))[1])
    real_reg = cockpit._lanes_register
    monkeypatch.setattr(cockpit, "_lanes_register", lambda *a: (order.append("add"), real_reg(*a))[1])
    _run(s, host)
    assert order.index("classify") < order.index("add")

    s2 = _s()
    host.path.write_text(json.dumps(BASE))
    host.calls.clear()
    monkeypatch.setattr(cockpit, "_lanes_classify", lambda p: ["agents.entries.kit-lane-cli-sonnet55.name"])
    assert _run(s2, host) is False
    assert json.loads(host.path.read_text()) == BASE
    assert ("agents", "add") not in host.verbs()
    assert s2.openclaw.lanes_applied["deferred"] == ["agents.entries.kit-lane-cli-sonnet55.name"]


# --- workspace and template ----------------------------------------------------------------------------

def test_lane_agent_kind_exists_and_the_workspace_is_created_before_agent_add(host, tmp_path):
    assert "lane-agent" in openclaw_host.AGENTS_TEMPLATES
    text = openclaw_host.render_agents_md("lane-agent", "kit-lane-cli-sonnet55")
    assert "kit-lane-cli-sonnet55" in text and "Co-Authored-By" in text
    s = _s()
    _run(s, host)
    root = cockpit.CONFIG_ROOT / "kit-lanes"
    ws = root / "kit-lane-cli-sonnet55"
    assert ws.is_dir() and (ws / "AGENTS.md").is_file()
    assert str(ws) in s.openclaw.lanes_applied["created_workspaces"]
    assert not str(ws).startswith("/tmp")


def test_workspace_root_under_tmp_is_refused(host):
    lanes = copy.deepcopy(LANES)
    lanes["workspace_root"] = "/tmp/kit-lanes"
    s = _s(lanes)
    before = host.path.read_text()
    assert _run(s, host) is False
    assert host.path.read_text() == before


# --- verify_capability and no spawn --------------------------------------------------------------------

@pytest.mark.parametrize("entry,mode,source", [
    ({"tools": {"exec": {"mode": "full"}}}, "exec", "static-config"),
    ({"tools": {"allow": ["ai-resources.lanes"]}}, "plugin-tool", "static-config"),
    ({"model": {"primary": "claude-cli/claude-sonnet-5-5"}}, "exec", "isolated-evidence"),
    ({"model": {"primary": "google/gemini-3.8-flash"}}, "none", "static-config"),
])
def test_verify_capability_is_static(entry, mode, source):
    cap = openclaw_lanes.verify_capability({"agents": {"entries": {"main": entry}}}, ["main"])
    assert cap["main"]["mode"] == mode and cap["main"]["source"] == source


CLI = {"primary": "claude-cli/claude-sonnet-5-5"}


@pytest.mark.parametrize("tools", [
    {"exec": {"mode": "deny"}}, {"exec": {"mode": "off"}}, {"deny": ["exec"]},
    {"deny": ["mcp__openclaw__exec"]}, {"deny": ["group:runtime"]},
])
def test_an_explicit_exec_deny_beats_the_claude_cli_isolated_evidence(tools):
    entry = {"model": CLI, "tools": tools}
    cap = openclaw_lanes.verify_capability({"agents": {"entries": {"main": entry}}}, ["main"])
    assert cap["main"] == {"mode": "none", "source": "static-config", "evidence_ref": ""}


def test_no_explicit_grant_or_deny_keeps_exec_by_isolated_evidence():
    entry = {"model": CLI, "tools": {"deny": ["bundle-mcp"]}}
    cap = openclaw_lanes.verify_capability({"agents": {"entries": {"main": entry}}}, ["main"])
    assert cap["main"]["mode"] == "exec" and cap["main"]["source"] == "isolated-evidence"


def test_setup_records_capability_and_never_spawns_or_calls_the_gateway(host):
    s = _s()
    _run(s, host)
    assert s.profile.lanes["verify_capability"]["main"]["mode"] == "exec"
    heads = {c[0] for c in host.calls}
    assert heads <= {"config", "agents"}, heads


# --- watch record --------------------------------------------------------------------------------------

def test_every_lane_leaf_enters_the_run_changes(host):
    s = _s()
    changes: list[dict] = []
    _run(s, host, run_changes=changes)
    paths = {".".join(c["path"]) for c in changes}
    assert "agents.entries.main.subagents.allowAgents" in paths
    assert f"agents.defaults.models.{SONNET}.agentRuntime.id" in paths
    assert all(c["action"] == "lanes" for c in changes)
    # and the restore patch of those records puts the previous values back
    patch, _ = openclaw_host.restore_patch(changes)
    assert patch["agents"]["entries"]["main"]["subagents"]["allowAgents"] == ["claude"]


# --- teardown ------------------------------------------------------------------------------------------

def test_teardown_removes_only_kit_ids_and_keeps_a_later_operator_id(host):
    s = _s()
    _run(s, host)
    doc = host.doc()
    doc["agents"]["entries"]["main"]["subagents"]["allowAgents"].append("operator-added")
    doc["agents"]["entries"]["operator-agent"] = {"name": "operator-agent"}
    host.path.write_text(json.dumps(doc))
    assert cockpit._teardown_lanes(s, host.doc()) is True
    after = host.doc()
    assert after["agents"]["entries"]["main"]["subagents"]["allowAgents"] == ["claude", "operator-added"]
    assert set(after["agents"]["entries"]) == {"main", "other", "claude", "operator-agent"}
    assert SONNET not in (after["agents"]["defaults"].get("models") or {})
    assert not (cockpit.CONFIG_ROOT / "kit-lanes" / "kit-lane-cli-sonnet55").exists()
    assert s.openclaw.lanes_applied == {}
    deletes = [c for c in host.calls if c[:2] == ["agents", "delete"]]
    assert {c[2] for c in deletes} == {"kit-lane-cli-sonnet55", "kit-lane-cli-opus55"}


def test_teardown_restores_the_config_byte_for_byte_without_operator_additions(host):
    s = _s()
    _run(s, host)
    cockpit._teardown_lanes(s, host.doc())
    after = host.doc()
    expected = copy.deepcopy(BASE)
    assert after["agents"]["entries"] == expected["agents"]["entries"]
    assert after["agents"]["defaults"].get("models") in (None, {})
    assert after["agents"]["defaults"]["subagents"] == expected["agents"]["defaults"]["subagents"]


def test_teardown_never_deletes_an_agent_the_kit_did_not_create(host):
    s = _s()
    doc = host.doc()
    doc["agents"]["entries"]["kit-lane-cli-sonnet55"] = {"name": "pre-existing"}
    host.path.write_text(json.dumps(doc))
    _run(s, host)
    assert "kit-lane-cli-sonnet55" not in s.openclaw.lanes_applied.get("created_agent_ids", [])
    cockpit._teardown_lanes(s, host.doc())
    assert "kit-lane-cli-sonnet55" in host.doc()["agents"]["entries"]
    assert ["agents", "delete", "kit-lane-cli-sonnet55", "--force"] not in host.calls


def test_declined_confirmation_keeps_the_agent_recorded(host, monkeypatch):
    s = _s()
    _run(s, host)
    monkeypatch.setattr(cockpit.ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(cockpit.ui, "confirm", lambda *a, **k: False)
    cockpit._teardown_lanes(s, host.doc())
    assert set(s.openclaw.lanes_applied["created_agent_ids"]) == set(LANES["agents"])
    assert "kit-lane-cli-sonnet55" in host.doc()["agents"]["entries"]


def test_no_lanes_in_state_tears_down_through_configure(host):
    s = _s()
    _run(s, host)
    s.profile.lanes = None
    assert _run(s, host) is True
    assert set(host.doc()["agents"]["entries"]) == {"main", "other", "claude"}
    assert s.openclaw.lanes_applied == {}


def test_nothing_resolves_the_live_openclaw_dir(tmp_path):
    import inspect
    assert "Path.home" not in inspect.getsource(openclaw_lanes)
    assert openclaw_lanes.workspace_root({}, tmp_path) == tmp_path / "kit-lanes"
    assert openclaw_lanes.workspace_root({"workspace_root": str(tmp_path / "x")}, tmp_path) == tmp_path / "x"


def test_lanes_writer_reads_the_live_config_after_the_engine_patch(host, monkeypatch, tmp_path):
    """The engine patch may add 'claude' to allowAgents; lanes must build from that, not the stale snapshot."""
    stale = host.doc()
    stale["agents"]["entries"]["main"]["subagents"]["allowAgents"] = []

    def engine(ctx, doc, path, ak_path, written):
        live = host.doc()
        live["agents"]["entries"]["main"]["subagents"]["allowAgents"] = ["claude"]
        host.path.write_text(json.dumps(live))
        return True

    seen = {}
    monkeypatch.setattr(cockpit, "_configure_engine", engine)
    for name in ("_configure_mcp", "_configure_voice", "_configure_skill_workshop", "_configure_workspace_blocks"):
        monkeypatch.setattr(cockpit, name, lambda *a, **k: False)
    monkeypatch.setattr(cockpit.host_section, "configure", lambda *a, **k: False)
    monkeypatch.setattr(cockpit, "_configure_lanes",
                        lambda s, doc, **k: seen.setdefault("doc", doc) and False)
    cockpit._configure({}, _s(), state.CockpitState(), False, [], host.path, stale, "", [])
    assert seen["doc"]["agents"]["entries"]["main"]["subagents"]["allowAgents"] == ["claude"]

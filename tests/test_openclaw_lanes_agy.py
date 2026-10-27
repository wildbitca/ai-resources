"""The OpenClaw writer for agy lane agents (ADR-0007 section 11, P3-S5): catalog stub, tool denials, revocation,
teardown. Fixture config under tmp_path, a fake `openclaw`, HOME redirected; nothing reaches the live host.

An agy write needs an absolute `agyBin` and nothing else: no flag, acknowledgement or gate (ADR-0019).
"""
from __future__ import annotations

import copy
import json
import stat

import pytest

import lane_symmetry as sym
from ai_resources import openclaw_host
from ai_resources.setup.cockpits import openclaw as cockpit, openclaw_lanes
from test_openclaw_lanes_setup import BASE, LANES, _run, _s, host  # noqa: F401

AID = "kit-lane-agy-sonnet55"
REF = "agy-cli-lane/claude-sonnet-5-5"
AGY_BIN = "/opt/agy/bin/agy"


def _lanes(roles=("tester",)):
    lanes = copy.deepcopy(LANES)
    lanes["agents"][AID] = {"backend": "agy-cli-lane", "model": REF, "pool": "agy-claude-gpt"}
    for r in roles:
        lanes["roles"][r] = {"candidates": [f"openclaw-agent:{AID}", "claude-code"]}
    return lanes


def _state(lanes=None):
    return _s(lanes or _lanes())


@pytest.fixture
def opened(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def _with_plugin(host, agy_bin=AGY_BIN):
    doc = host.doc()
    doc.setdefault("plugins", {}).setdefault("entries", {})["ai-resources"] = {"config": {"agyBin": agy_bin}}
    host.path.write_text(json.dumps(doc, indent=2))


def _agy_in(obj) -> bool:
    return "agy-cli-lane" in json.dumps(obj) or "kit-lane-agy" in json.dumps(obj)


def _no_agy_call(host):
    assert not [c for c in host.calls if c[0] in ("agy", "sessions", "agent", "message", "gateway")], host.calls


# --- the writer ----------------------------------------------------------------------------------------

def test_an_admitted_agy_agent_gets_its_entry_stub_and_denials(opened, host):
    _with_plugin(host)
    s = _state()
    assert _run(s, host) is True
    doc = host.doc()
    entry = doc["agents"]["entries"][AID]
    assert entry["model"]["primary"] == REF
    assert set(entry["tools"]["deny"]) == {"bundle-mcp", "message", "sessions_spawn", "cron"}
    stub = doc["models"]["providers"]["agy-cli-lane"]
    assert stub["apiKey"] == openclaw_lanes.STUB_API_KEY == "kit-lane-agy-placeholder"
    assert stub["baseUrl"] == "http://127.0.0.1:9/"
    assert stub["models"] == [{"id": "claude-sonnet-5-5", "name": "claude-sonnet-5-5"}]
    assert AID in doc["agents"]["entries"]["main"]["subagents"]["allowAgents"]
    applied = s.openclaw.lanes_applied
    assert applied["providers_added"] == {AID: "claude-sonnet-5-5"}
    assert applied["provider_container_absent"] is True
    assert set(applied["tool_deny_added"][AID]) == {"message", "sessions_spawn", "cron"}
    assert openclaw_host.is_secret_path(["models", "providers", "agy-cli-lane", "apiKey"])
    _no_agy_call(host)


def test_the_stub_is_written_only_for_an_agy_agent(opened, host):
    _with_plugin(host)
    assert _run(_s(), host) is True                                       # claude-cli lanes only
    assert "models" not in host.doc()


def test_the_dry_run_writes_nothing_and_does_not_print_the_key(opened, host, capsys):
    _with_plugin(host)
    before = host.path.read_text()
    assert _run(_state(), host, dry_run=True) is False
    assert host.path.read_text() == before
    out = capsys.readouterr().out
    assert "kit-lane-agy-placeholder" not in out
    # the one validated, restart-classified patch covers the admitted agy agent and the catalog stub
    assert AID in out and "agy-cli-lane" in out


def test_a_second_run_is_idempotent(opened, host):
    _with_plugin(host)
    s = _state()
    assert _run(s, host) is True
    snap = host.path.read_text()
    assert _run(s, host) is False
    assert host.path.read_text() == snap


@pytest.mark.parametrize("agy_bin", [None, "agy", "./agy", ""])
def test_a_missing_or_relative_agy_bin_skips_the_agent_with_the_drained_window_reason(opened, host, agy_bin):
    if agy_bin is not None:
        _with_plugin(host, agy_bin)
    s = _state()
    assert _run(s, host) is True                                          # claude-cli agents are unaffected
    doc = host.doc()
    assert not _agy_in(doc["agents"]) and "models" not in doc
    why = openclaw_lanes.skipped_agents(s.profile.lanes, doc)
    assert "agy binary not configured for the kit plugin" in why[AID] and "drained window" in why[AID]


def test_operator_tokens_and_models_are_kept_and_teardown_removes_only_what_the_kit_added(opened, host):
    doc = host.doc()
    doc["models"] = {"providers": {"agy-cli-lane": {"baseUrl": "http://127.0.0.1:9/", "api": "x", "apiKey": "k",
                                                    "models": [{"id": "operator-model", "name": "op"}]}}}
    doc["agents"]["entries"][AID] = {"name": AID, "tools": {"deny": ["operator-token"]}}
    host.path.write_text(json.dumps(doc))
    _with_plugin(host)
    s = _state()
    assert _run(s, host) is True
    live = host.doc()
    assert "operator-token" in live["agents"]["entries"][AID]["tools"]["deny"]
    assert [m["id"] for m in live["models"]["providers"]["agy-cli-lane"]["models"]] == \
        ["operator-model", "claude-sonnet-5-5"]
    assert live["models"]["providers"]["agy-cli-lane"]["apiKey"] == "k"       # fill-only: never overwritten
    deleted = []

    def oc(argv, **kw):
        deleted.append(argv)
        return host(argv)
    from ai_resources.setup.cockpits import openclaw_lanes as ol
    assert ol.teardown(s, host.doc(), apply_patch=cockpit._lanes_apply, oc=oc, confirm=lambda m: True) is True
    after = host.doc()
    assert [m["id"] for m in after["models"]["providers"]["agy-cli-lane"]["models"]] == ["operator-model"]
    assert after["agents"]["entries"]["main"]["subagents"]["allowAgents"] == ["claude"]


def test_teardown_removes_the_stub_and_its_container_when_the_kit_created_them(opened, host):
    _with_plugin(host)
    s = _state()
    assert _run(s, host) is True
    from ai_resources.setup.cockpits import openclaw_lanes as ol
    assert ol.teardown(s, host.doc(), apply_patch=cockpit._lanes_apply, oc=lambda a, **k: host(a)) is True
    doc = host.doc()
    assert "models" not in doc or "providers" not in (doc.get("models") or {})
    assert not _agy_in({k: v for k, v in doc.items() if k != "plugins"})


# --- revocation (R1, AC3.21) ------------------------------------------------------------------------------

# --- the kit-owned agy home -------------------------------------------------------------------------------

def test_the_agy_home_path_is_never_under_tmp_or_the_lane_workspaces(opened, tmp_path):
    p = openclaw_lanes.agy_home_path()
    assert p == tmp_path / "home" / ".local" / "share" / "ai-resources" / "agy-home"
    assert not str(p).startswith("/tmp") and ".openclaw" not in str(p)


def test_ensure_and_remove_the_agy_home_inside_a_tmp_path(tmp_path):
    home = tmp_path / "home" / ".local" / "share" / "ai-resources" / "agy-home"
    rec = openclaw_lanes.ensure_agy_home(home)
    assert rec == {"path": str(home), "created": True}
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert openclaw_lanes.ensure_agy_home(home) == {"path": str(home), "created": False}
    assert openclaw_lanes.remove_agy_home({"path": str(home), "created": True}, confirm=lambda m: False) is False
    assert home.exists()
    assert openclaw_lanes.remove_agy_home({"path": str(home), "created": True}, confirm=lambda m: True) is True
    assert not home.exists()
    assert openclaw_lanes.remove_agy_home({"path": str(tmp_path / "elsewhere"), "created": True},
                                          confirm=lambda m: True) is False        # not the kit-owned name


# --- AC3.9: the same keys except the allowlist ---------------------------------------------------------------

def _symmetric_lanes(label):
    lanes = copy.deepcopy(LANES)
    if label == "claude-cli":
        return lanes
    renamed = {"kit-lane-cli-sonnet55": ("kit-lane-agy-sonnet55", "agy-cli-lane/claude-sonnet-5-5"),
               "kit-lane-cli-opus55": ("kit-lane-agy-opus55", "agy-cli-lane/claude-opus-5-5")}
    lanes["agents"] = {new: {"backend": "agy-cli-lane", "model": model, "pool": "agy-claude-gpt"}
                       for _, (new, model) in renamed.items()}
    for role in lanes["roles"].values():
        role["candidates"] = [f"openclaw-agent:{renamed[c.split(':', 1)[1]][0]}" if c.startswith("openclaw-agent:")
                              else c for c in role["candidates"]]
    return lanes


def test_the_writer_gives_the_same_config_for_either_label_except_the_allowlist(opened, host):
    _with_plugin(host)
    initial = host.path.read_text()
    docs = {}
    for label in ("claude-cli", "agy-cli-lane"):
        host.path.write_text(initial)
        s = _s(_symmetric_lanes(label))
        assert _run(s, host) is True, label
        doc = host.doc()
        doc["_applied"] = {k: v for k, v in s.openclaw.lanes_applied.items()
                           if k in ("created_agent_ids", "tool_deny_added", "mcp_deny_added")}
        docs[label] = sym.normalize(doc)
    assert any(k.startswith("kit-lane-KIND-") for k in docs["agy-cli-lane"]["agents"]["entries"])
    assert sym.unexplained(docs["claude-cli"], docs["agy-cli-lane"]) == set(), sym.diff_paths(
        docs["claude-cli"], docs["agy-cli-lane"])
    # the allowed differences exist: the catalog stub and the extra tool denials (asymmetries 4 and 7)
    differing = sym.diff_paths(docs["claude-cli"], docs["agy-cli-lane"])
    assert "models" in differing and any("tools.deny" in p for p in differing)

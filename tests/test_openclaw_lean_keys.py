"""The lean-cost keys of the host profile never need a gateway restart (ADR-0003, AC-12)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_resources import openclaw_host as host
from ai_resources import openclaw_reload_rules as rr

LEAN = (
    "session.reset", "session.resetByType", "agents.defaults.contextPruning", "agents.defaults.models",
    "agents.defaults.thinkingDefault", "agents.defaults.utilityModel", "agents.defaults.subagents",
    "agents.entries.main.heartbeat.isolatedSession", "agents.entries.main.heartbeat.lightContext",
    "agents.entries.main.thinkingDefault",
)
VALUES = {"DOMAIN": "ai.example.org", "POD_CIDR": "10.9.0.0/24", "HOME": "/home/u"}


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(host.Path, "home", classmethod(lambda cls: tmp_path))


def _baseline_ruleset():
    return rr.RuleSet("baseline", "installed", rr.BASELINE_RULES, "")


def _lean_paths(built) -> list[str]:
    return [".".join(c["path"]) for c in built["changes"]
            if any(".".join(c["path"]).startswith(p) for p in LEAN)]


def test_every_lean_key_is_built_on_an_empty_config():
    built = host.build_host_patch(host.load_host_profile(), {}, VALUES)
    got = _lean_paths(built)
    for prefix in LEAN:
        assert any(p.startswith(prefix) for p in got), prefix


def test_no_lean_key_is_restart_required_under_the_baseline_table():
    built = host.build_host_patch(host.load_host_profile(), {}, VALUES)
    kinds = rr.classify(_lean_paths(built), ruleset=_baseline_ruleset())
    assert kinds and all(k in (rr.HOT, rr.NONE) for k in kinds.values()), kinds


def test_the_lean_patch_alone_has_no_restart_path():
    built = host.build_host_patch(host.load_host_profile(), {}, VALUES)
    lean_patch = {}
    for path in _lean_paths(built):
        cur, src = lean_patch, built["patch"]
        segs = [c for c in built["changes"] if ".".join(c["path"]) == path][0]["path"]
        for k in segs[:-1]:
            cur, src = cur.setdefault(k, {}), src[k]
        cur[segs[-1]] = src[segs[-1]]
    assert rr.restart_paths(lean_patch, ruleset=_baseline_ruleset()) == []


def test_an_unknown_path_still_fails_closed():
    assert rr.classify(["agents.defaults.somethingNew"], ruleset=_baseline_ruleset())["agents.defaults.somethingNew"] == rr.HOT
    assert rr.classify(["brandNewRoot.key"], ruleset=_baseline_ruleset())["brandNewRoot.key"] == rr.RESTART


def test_a_second_run_against_the_patched_config_sends_nothing_new():
    profile = host.load_host_profile()
    start = {"agents": {"entries": {"main": {}}}}
    first = host.build_host_patch(profile, start, VALUES)
    applied = rr.deep_merge(start, first["patch"])
    second = host.build_host_patch(profile, applied, VALUES)
    assert second["patch"] == {} and second["changes"] == []


def test_an_operator_value_is_kept_and_listed_and_force_replaces_it():
    profile = host.load_host_profile()
    doc = {"agents": {"defaults": {"thinkingDefault": "high"}, "entries": {"main": {"thinkingDefault": "high"}}}}
    kept = host.build_host_patch(profile, doc, VALUES)
    assert "agents.defaults.thinkingDefault" in kept["kept"] and "agents.entries.main.thinkingDefault" in kept["kept"]
    assert "thinkingDefault" not in json.dumps(kept["patch"].get("agents", {}).get("defaults", {}))
    forced = host.build_host_patch(profile, doc, VALUES, overrides={
        "force": frozenset({"agents.defaults.thinkingDefault", "agents.entries.main.thinkingDefault"}),
        "keep": frozenset()})
    assert forced["patch"]["agents"]["defaults"]["thinkingDefault"] == "low"
    assert forced["patch"]["agents"]["entries"]["main"]["thinkingDefault"] == "medium"


def test_the_profile_never_carries_secrets_deny_lists_or_channel_allowlists():
    built = host.build_host_patch(host.load_host_profile(), {}, VALUES)
    text = json.dumps(built["patch"])
    assert "tools" not in built["patch"] or "deny" not in built["patch"]["tools"]
    assert "allowFrom" not in text and "allowBots" not in text and "threadParticipation" not in text
    host.assert_channels_safe(built["patch"], built["replace_paths"])
    host.assert_tools_deny_safe(built["patch"], built["replace_paths"])


def test_the_utility_model_is_exempt_from_the_haiku_orchestrator_rule():
    built = host.build_host_patch(host.load_host_profile(), {}, VALUES)
    assert built["patch"]["agents"]["defaults"]["utilityModel"].endswith("haiku-5-5")
    entries = built["patch"]["agents"].get("entries", {})
    assert all("haiku" not in json.dumps(v.get("model", {})) for v in entries.values())

"""OpenClaw subagent limits, fill-only (ADR-0015 S10, item d, acceptance 12)."""
from __future__ import annotations

import copy
import json

import pytest

from ai_resources import openclaw_host as host
from ai_resources import openclaw_reload_rules as rr
from ai_resources.setup import state, ui
from ai_resources.setup.cockpits import _openclaw_host as section

VALUES = {"DOMAIN": "ai.example.org", "POD_CIDR": "10.9.0.0/24", "HOME": "/home/u"}
LIMITS = {"maxConcurrent": 3, "maxChildrenPerAgent": 3, "maxSpawnDepth": 2, "runTimeoutSeconds": 7200}
# The platform defaults read from `openclaw config schema` (2026.10.5): every kit value must be lower, so the kit only narrows.
PLATFORM_DEFAULT = {"maxConcurrent": 8, "maxChildrenPerAgent": 5, "maxSpawnDepth": 5}
BASELINE = rr.RuleSet("baseline", "installed", rr.BASELINE_RULES, "")


@pytest.fixture(autouse=True)
def _home(monkeypatch, tmp_path):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(host.Path, "home", classmethod(lambda cls: tmp_path))


def test_all_four_keys_are_filled_on_an_empty_config():
    built = host.build_host_patch(host.load_host_profile(), {}, VALUES)
    sub = built["patch"]["agents"]["defaults"]["subagents"]
    for key, value in LIMITS.items():
        assert sub[key] == value, key
        assert f"agents.defaults.subagents.{key}" in built["filled"]


def test_the_limits_only_narrow_the_platform_defaults():
    for key, default in PLATFORM_DEFAULT.items():
        assert LIMITS[key] < default, key


def test_an_operator_value_is_kept_and_listed():
    doc = {"agents": {"defaults": {"subagents": {"maxSpawnDepth": 4, "runTimeoutSeconds": 600}}}}
    built = host.build_host_patch(host.load_host_profile(), doc, VALUES)
    sub = built["patch"]["agents"]["defaults"]["subagents"]
    assert "maxSpawnDepth" not in sub and "runTimeoutSeconds" not in sub
    assert {"agents.defaults.subagents.maxSpawnDepth", "agents.defaults.subagents.runTimeoutSeconds"} <= set(built["kept"])
    assert sub["maxConcurrent"] == 3 and sub["maxChildrenPerAgent"] == 3          # the unset ones are still filled


def test_a_second_run_sends_nothing():
    profile = host.load_host_profile()
    first = host.build_host_patch(profile, {}, VALUES)
    second = host.build_host_patch(profile, rr.deep_merge({}, first["patch"]), VALUES)
    assert not any(p.startswith("agents.defaults.subagents") for p in second["filled"])
    assert "subagents" not in json.dumps(second["patch"].get("agents", {}).get("defaults", {}))


def test_each_key_is_hot_under_the_baseline_table_and_not_restart_class():
    paths = [f"agents.defaults.subagents.{k}" for k in LIMITS]
    kinds = rr.classify(paths, ruleset=BASELINE)
    assert all(kinds[p] in (rr.HOT, rr.NONE) for p in paths), kinds
    patch = {"agents": {"defaults": {"subagents": dict(LIMITS)}}}
    assert rr.restart_paths(patch, ruleset=BASELINE) == []


def test_a_restart_class_key_would_be_listed_pending_and_an_unreadable_table_fails_closed():
    restart_rules = (("agents.defaults.subagents.maxSpawnDepth", "restart"),) + rr.BASELINE_RULES
    ruleset = rr.RuleSet("test", "installed", restart_rules, "")
    patch = {"agents": {"defaults": {"subagents": dict(LIMITS)}}}
    assert rr.restart_paths(patch, ruleset=ruleset) == ["agents.defaults.subagents.maxSpawnDepth"]
    hot, restart = rr.split_patch(patch, ["agents.defaults.subagents.maxSpawnDepth"])
    assert "maxSpawnDepth" not in hot["agents"]["defaults"]["subagents"] and restart
    assert rr.restart_paths(patch, ruleset=None, reload_mode="off")        # reload mode off: every key counts as restart


def test_the_host_section_applies_the_limits_without_any_restart(monkeypatch):
    """ADR-0003: the fill goes through `config patch` only; no restart call is made and nothing live is touched."""
    o = state.OpenClawState()
    o.host_config = True
    sent: list[dict] = []

    def apply(patch, dry_run=False, replace_paths=None):
        if not dry_run:
            sent.append(copy.deepcopy(patch))
        return True, "ok"

    def forbidden(*a, **k):
        raise AssertionError("a restart or a gateway call was made")
    monkeypatch.setattr(host, "restart_gateway", forbidden, raising=False)
    monkeypatch.setattr(section, "_gate", lambda *a, **k: True)
    monkeypatch.setattr(section, "_offer_legacy_revert", lambda *a, **k: False)
    monkeypatch.setattr(host, "load_host_overrides", lambda: {"force": frozenset(), "keep": frozenset()})
    monkeypatch.setattr(section, "_values", lambda o: VALUES)
    seen_oc: list[list[str]] = []
    changed = section._configure_config_fill(o, {}, host.Path("/x"), [], dry_run=False, apply_patch=apply,
                                             oc=lambda argv, **k: seen_oc.append(list(argv)) or (1, ""))
    assert changed is True
    sub = sent[-1]["agents"]["defaults"]["subagents"]
    assert {k: sub[k] for k in LIMITS} == LIMITS
    assert not any("restart" in " ".join(a) for a in seen_oc)
    # the profile's other restart-class keys (gateway.bind) may be pending; none of the four limits is
    assert not [e for e in o.host_restart_pending if "subagents" in str(e.get("path"))]


def test_the_profile_text_documents_why():
    text = (host.Path(__file__).resolve().parents[1] / "profiles" / "openclaw-host.json5").read_text()
    assert "ADR-0015" in text and "maxSpawnDepth" in text and "runTimeoutSeconds" in text

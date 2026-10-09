"""The pinned restart-required table (scripts/ai_resources/openclaw_reload_rules.py, ADR-0003).

Offline except the drift test, which compares the pin with the installed OpenClaw package when
there is one.
"""
from __future__ import annotations

import glob
import json
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import openclaw_host as host  # noqa: E402
from ai_resources import openclaw_reload_rules as rr  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / f"openclaw_reload_rules_{rr.PINNED_OPENCLAW_VERSION}.json"
V = rr.PINNED_OPENCLAW_VERSION
PKG = pathlib.Path("/home/linuxbrew/.linuxbrew/lib/node_modules/openclaw")


def kind(path, **kw):
    return rr.classify([path], openclaw_version=kw.pop("openclaw_version", V), **kw)[path]


def test_gateway_bind_is_restart_required():
    assert kind("gateway.bind") == "restart"


@pytest.mark.parametrize("path", [
    "gateway.trustedProxies", "gateway.allowRealIpFallback", "gateway.controlUi.allowedOrigins",
    "gateway.controlUi.github.token", "gateway.publicOrigin", "agents.defaults.model.primary",
    "agents.defaults.heartbeat.every", "agents.entries.main.model.primary", "tools.profile",
    "channels.telegram.streaming.mode", "plugins.entries.device-pair.config.publicUrl",
])
def test_known_hot_keys(path):
    assert kind(path) == "hot"


def test_logging_follows_the_dist_table():
    assert kind("logging.file") == "none"


def test_version_mismatch_or_unknown_is_all_restart():
    for ver in ("2026.9.10", "2025.1.1", None, ""):
        assert kind("tools.profile", openclaw_version=ver) == "restart"
        assert kind("agents.defaults.model.primary", openclaw_version=ver) == "restart"


def test_reload_mode_off_is_all_restart():
    assert kind("tools.profile", reload_mode="off") == "restart"
    assert kind("tools.profile", reload_mode="hybrid") == "hot"


def test_unmatched_path_is_restart():
    assert kind("brandNewTopLevel.key") == "restart"


def test_longest_prefix_wins():
    # `gateway` is restart, `gateway.tools` hot, `gateway.auth.token` restart, `gateway.auth.rateLimit` hot.
    assert kind("gateway.tools.deny") == "hot"
    assert kind("gateway.auth.token") == "restart"
    assert kind("gateway.auth.rateLimit.max") == "hot"
    assert kind("agents.entries.x.decisionModel") == "hot"
    assert kind("mcp.apps.sandboxOrigin") == "hot"
    assert kind("mcp.apps.other") == "restart"


def test_host_profile_only_bind_is_restart():
    profile = host.load_host_profile()
    out = host.build_host_patch(profile, {}, {"DOMAIN": "ai.example.org", "POD_CIDR": "10.9.0.0/24",
                                              "HOME": "/home/u", "TAILNET": "1"})
    paths = rr.leaf_paths(out["patch"])
    assert "gateway.bind" in paths
    got = rr.classify(paths, openclaw_version=V)
    assert [p for p, k in got.items() if k == "restart"] == ["gateway.bind"]


def test_rules_equal_the_offline_fixture():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["openclaw_version"] == V
    assert [list(r) for r in rr.RULES] == data["rules"]


def _extract_from_dist(dist_file: pathlib.Path) -> list[list[str]]:
    src = dist_file.read_text(encoding="utf-8")
    auth = re.findall(r'"([^"]+)"', re.search(r"const AUTH_CREDENTIAL_PATHS = \[(.*?)\];", src).group(1))
    rules: list[list[str]] = []
    for name in ("CORE_RELOAD_POLICIES", "DEFAULT_RELOAD_POLICIES"):
        block = re.search(r"const %s = \[(.*?)\n\];" % name, src, re.S).group(1)
        for blk in re.finditer(r"\{\s*prefixes: \[(.*?)\],\s*kind: \"(\w+)\"", block, re.S):
            for tok in re.finditer(r'"([^"]+)"|\.\.\.(\w+)', blk.group(1)):
                if tok.group(1):
                    rules.append([tok.group(1), blk.group(2)])
                else:
                    rules += [[p, blk.group(2)] for p in auth]
    return rules


def test_pin_matches_the_installed_dist():
    dists = glob.glob(str(PKG / "dist" / "config-reload-plan-*.mjs"))
    if not dists:
        pytest.skip("OpenClaw is not installed here (GitHub CI checks the fixture only)")
    version = json.loads((PKG / "package.json").read_text(encoding="utf-8"))["version"]
    how = ("refresh scripts/ai_resources/openclaw_reload_rules.py (RULES, PINNED_OPENCLAW_VERSION) and "
           "tests/fixtures/openclaw_reload_rules_<version>.json from dist/config-reload-plan-*.mjs")
    assert version == rr.PINNED_OPENCLAW_VERSION, f"OpenClaw {version} is installed: {how}"
    assert _extract_from_dist(pathlib.Path(dists[0])) == [list(r) for r in rr.RULES], f"the dist table moved: {how}"


def test_leaf_paths_counts_none_and_lists():
    patch = {"a": {"b": None, "c": [1, 2], "d": {"e": 1}}, "f": "x"}
    assert rr.leaf_paths(patch) == ["a.b", "a.c", "a.d.e", "f"]


def test_split_patch_round_trip():
    patch = {"gateway": {"bind": "tailnet", "trustedProxies": ["10.0.0.0/8"],
                         "controlUi": {"allowedOrigins": ["https://x"]}},
             "tools": {"profile": "coding"}}
    restart = rr.restart_paths(patch, openclaw_version=V)
    assert restart == ["gateway.bind"]
    hot, res = rr.split_patch(patch, restart)
    assert res == {"gateway": {"bind": "tailnet"}}
    assert "bind" not in hot["gateway"]
    assert rr.deep_merge(hot, res) == patch


def test_split_patch_with_nothing_restart():
    patch = {"tools": {"profile": "coding"}}
    assert rr.split_patch(patch, []) == (patch, {})


def test_split_replace_paths_and_changes():
    hot, res = rr.split_replace_paths(["gateway.bind", "tools.alsoAllow"], ["gateway.bind"])
    assert (hot, res) == (["tools.alsoAllow"], ["gateway.bind"])
    changes = [{"path": ["gateway", "bind"], "had": True}, {"path": ["tools", "profile"], "had": True}]
    h, r = rr.split_changes(changes, ["gateway.bind"])
    assert [c["path"] for c in h] == [["tools", "profile"]]
    assert [c["path"] for c in r] == [["gateway", "bind"]]


def test_installed_version_parse_and_failure():
    calls = []

    def ok(argv, **kw):
        calls.append(argv)
        return 0, "OpenClaw 2026.9.9 (bcfc888)"

    assert rr.installed_openclaw_version(ok) == "2026.9.9"
    assert rr.installed_openclaw_version(ok) == "2026.9.9"
    assert len(calls) == 1  # cached

    assert rr.installed_openclaw_version(lambda argv, **kw: (1, "boom")) is None
    assert rr.installed_openclaw_version(lambda argv, **kw: (0, "garbage")) is None


def test_reload_mode_of():
    assert rr.reload_mode_of({"gateway": {"reload": {"mode": "off"}}}) == "off"
    assert rr.reload_mode_of({"gateway": {}}) is None
    assert rr.reload_mode_of(None) is None

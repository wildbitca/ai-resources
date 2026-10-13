"""The reload-rule classifier (scripts/ai_resources/openclaw_reload_rules.py, ADR-0003).

Offline: every test builds its rule table from a checked-in dist excerpt, never from the OpenClaw that
happens to be installed on the machine running the suite.
"""
from __future__ import annotations

import json
import pathlib

import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests"))

from ai_resources import openclaw_host as host  # noqa: E402
from ai_resources import openclaw_reload_rules as rr  # noqa: E402
from reload_rules_fixture import (DIST_FILE, DIST_TEXT, FIXTURE_DIR, FIXTURE_VERSION,  # noqa: E402
                                  fixture_ruleset, mutated)

FIXTURE = REPO / "tests" / "fixtures" / "openclaw_reload_rules_baseline.json"
RS = fixture_ruleset()
# conftest replaces `current_ruleset` for every other test; these tests exercise the real one.
_REAL_CURRENT_RULESET = rr.current_ruleset


def kind(path, *, ruleset=RS, **kw):
    return rr.classify([path], ruleset=ruleset, **kw)[path]


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
    got = rr.classify(paths, ruleset=RS)
    assert [p for p, k in got.items() if k == "restart"] == ["gateway.bind"]


# --- TS-RR-01: the parser against the checked-in excerpt ---------------------------------------------------------------

def test_the_parser_reads_the_dist_excerpt_into_the_baseline_table():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["read_from"] == rr.BASELINE_READ_FROM
    parsed = rr.parse_reload_rules(DIST_TEXT)
    assert parsed is not None and len(parsed) == 108
    assert [list(r) for r in parsed] == data["rules"]
    assert parsed == rr.BASELINE_RULES


# --- TS-RR-02: the version is irrelevant ------------------------------------------------------------------------------

def test_the_rules_come_from_the_installed_table_whatever_its_version():
    assert RS.version == FIXTURE_VERSION and RS.source == "installed"
    for key in ("tools.profile", "agents.defaults.model.primary", "agents.defaults.timeoutSeconds",
                "mcp.sessionIdleTtlMs", "gateway.trustedProxies"):
        assert kind(key) == "hot", key
    assert kind("gateway.bind") == "restart"


# --- TS-RR-03..05: the installed table moves, the kit follows ----------------------------------------------------------

def test_a_key_that_became_restart_required_is_restart_at_once():
    moved = _move_prefix(DIST_TEXT, "tools", "restart")
    assert kind("tools.profile", ruleset=fixture_ruleset(moved)) == "restart"
    assert kind("tools.profile") == "hot"


def _move_prefix(text: str, prefix: str, new_kind: str) -> str:
    """The excerpt with `prefix` taken out of its policy object and put in a one-prefix object of `new_kind`."""
    for pat in (f'"{prefix}",\n', f', "{prefix}"', f'"{prefix}"'):
        if pat in text:
            text = text.replace(pat, "", 1)
            break
    else:
        raise AssertionError(prefix)
    return text.replace("const DEFAULT_RELOAD_POLICIES = [\n",
                        "const DEFAULT_RELOAD_POLICIES = [\n\t{\n\t\tprefixes: [\"%s\"],\n\t\tkind: \"%s\"\n\t},\n"
                        % (prefix, new_kind), 1)


def test_a_key_that_became_hot_stays_restart_and_is_listed_as_stricter():
    moved = _move_prefix(DIST_TEXT, "mcp.apps", "hot")
    rs = fixture_ruleset(moved)
    assert rs.source == "installed"
    assert kind("mcp.apps.x", ruleset=rs) == "restart"
    assert rs.stricter == ("mcp.apps",)
    assert RS.stricter == ()


@pytest.mark.parametrize("new_kind", ["hot", "none"])
@pytest.mark.parametrize("prefix", ["gateway.bind", "gateway.port"])
def test_a_new_installed_prefix_nested_under_a_baseline_restart_catch_all_stays_restart(prefix, new_kind):
    src = DIST_TEXT.replace("const DEFAULT_RELOAD_POLICIES = [\n",
                            'const DEFAULT_RELOAD_POLICIES = [\n\t{\n\t\tprefixes: ["%s"],\n'
                            '\t\tkind: "%s"\n\t},\n' % (prefix, new_kind), 1)
    rs = fixture_ruleset(src)
    assert rs.source == "installed"
    assert kind(prefix, ruleset=rs) == "restart"
    assert kind(prefix + ".x", ruleset=rs) == "restart"
    assert prefix in rs.stricter


def test_a_new_hot_key_is_hot_and_an_unknown_key_is_restart():
    src = DIST_TEXT.replace("const DEFAULT_RELOAD_POLICIES = [\n",
                            'const DEFAULT_RELOAD_POLICIES = [\n\t{\n\t\tprefixes: ["newFeature.knob"],\n'
                            '\t\tkind: "hot"\n\t},\n', 1)
    rs = fixture_ruleset(src)
    assert kind("newFeature.knob.x", ruleset=rs) == "hot"
    assert kind("brandNew.key", ruleset=rs) == "restart"


# --- TS-RR-06: a table the kit does not fully understand is never trusted ----------------------------------------------

def _drop_object_with(text: str, needle: str) -> str:
    i = text.index(needle)
    start = text.rindex("\t{\n", 0, i)
    end = text.index("\t},\n", i) + len("\t},\n")
    return text[:start] + text[end:]


_MUTATIONS = {
    "unknown spread": lambda: DIST_TEXT.replace("...AUTH_CREDENTIAL_PATHS", "...OTHER_PATHS", 1)
                              if DIST_TEXT.count("...AUTH_CREDENTIAL_PATHS") else None,
    "bare identifier": lambda: DIST_TEXT.replace('"gateway.tools",', "gatewayTools,", 1),
    "template literal": lambda: DIST_TEXT.replace('"gateway.tools",', "`gateway.${x}`,", 1),
    "other object shape": lambda: DIST_TEXT.replace(
        "const DEFAULT_RELOAD_POLICIES = [\n",
        "const DEFAULT_RELOAD_POLICIES = [\n\t{ kind: \"hot\", match: () => true },\n", 1),
    "block missing": lambda: DIST_TEXT.replace("const CORE_RELOAD_POLICIES", "const CORE_RELOAD_RENAMED", 1),
    "duplicated block": lambda: DIST_TEXT + "\n" + DIST_TEXT,
    "bad kind": lambda: DIST_TEXT.replace('kind: "none"', 'kind: "maybe"', 1),
    "prefix with a space": lambda: DIST_TEXT.replace('"gateway.tools"', '"gateway tools"', 1),
    "gateway catch-all removed": lambda: DIST_TEXT.replace('prefixes: ["gateway", "discovery"],',
                                                          'prefixes: ["discovery"],', 1),
    "discovery catch-all removed": lambda: DIST_TEXT.replace('prefixes: ["gateway", "discovery"],',
                                                            'prefixes: ["gateway"],', 1),
    "empty": lambda: "",
    "garbage": lambda: "\x00\x01\ufffd" * 50 + "{[(",
    "far too small": lambda: ('const AUTH_CREDENTIAL_PATHS = ["a.b"];\n'
                              'const CORE_RELOAD_POLICIES = [\n\t{\n\t\tprefixes: ["gateway"],\n\t\tkind: "restart"\n\t}\n];\n'
                              'const DEFAULT_RELOAD_POLICIES = [\n\t{\n\t\tprefixes: ["discovery"],\n\t\tkind: "restart"\n\t}\n];\n'),
}


@pytest.mark.parametrize("name", sorted(_MUTATIONS))
def test_a_table_the_parser_does_not_understand_is_all_restart(name):
    src = _MUTATIONS[name]()
    assert src is not None and src != DIST_TEXT, name
    assert rr.parse_reload_rules(src) is None
    rs = rr.ruleset_from_source(src, version=FIXTURE_VERSION)
    assert rs.source == "unavailable" and rs.rules is None and rs.reason
    assert set(rr.classify(["tools.profile", "gateway.bind", "agents.defaults.model.primary"],
                           ruleset=rs).values()) == {"restart"}


def test_the_parser_never_raises_on_a_non_string():
    assert rr.parse_reload_rules(None) is None  # type: ignore[arg-type]
    assert rr.parse_reload_rules(b"bytes") is None  # type: ignore[arg-type]


# --- TS-RR-07/08/09: reading the installed package ------------------------------------------------------------------

def _no_cli(argv, **kw):
    return 127, ""


def _package(tmp_path, *, dists=("config-reload-plan-aaa.mjs",), version="2099.1.1", text=None):
    pkg = tmp_path / "pkg"
    (pkg / "dist").mkdir(parents=True)
    (pkg / "package.json").write_text(json.dumps({"name": "openclaw", "version": version}), encoding="utf-8")
    for d in dists:
        (pkg / "dist" / d).write_text(DIST_TEXT if text is None else text, encoding="utf-8")
    return pkg


def test_current_ruleset_reads_the_installed_package(tmp_path, monkeypatch):
    pkg = _package(tmp_path)
    monkeypatch.setattr(rr, "_find_package_json", lambda: pkg / "package.json")
    monkeypatch.setattr(rr, "installed_openclaw_version", lambda runner, **kw: "2099.1.1")
    rs = _REAL_CURRENT_RULESET(_no_cli, refresh=True)
    assert (rs.source, rs.version, rs.installed_count) == ("installed", "2099.1.1", 108)
    assert rs.dist and rs.dist.endswith("config-reload-plan-aaa.mjs")
    assert rr.classify(["tools.profile", "gateway.bind"], ruleset=rs) == {"tools.profile": "hot",
                                                                          "gateway.bind": "restart"}


@pytest.mark.parametrize("case", ["no package", "no dist dir", "no dist file", "two dist files", "unparsable"])
def test_an_unreadable_package_is_unavailable_with_a_reason(tmp_path, monkeypatch, case):
    if case == "no package":
        pj = None
    else:
        pkg = _package(tmp_path, dists={"no dist dir": (), "no dist file": (), "two dist files":
                                        ("config-reload-plan-a.mjs", "config-reload-plan-b.mjs"),
                                        "unparsable": ("config-reload-plan-a.mjs",)}[case],
                       text="not a reload table" if case == "unparsable" else None)
        if case == "no dist dir":
            (pkg / "dist").rmdir()
        pj = pkg / "package.json"
    monkeypatch.setattr(rr, "_find_package_json", lambda: pj)
    rs = _REAL_CURRENT_RULESET(_no_cli, refresh=True)
    assert rs.source == "unavailable" and rs.rules is None and rs.reason
    assert rr.classify(["tools.profile"], ruleset=rs) == {"tools.profile": "restart"}


def test_a_dist_file_that_is_not_utf8_is_unavailable(tmp_path, monkeypatch):
    pkg = _package(tmp_path)
    (pkg / "dist" / "config-reload-plan-aaa.mjs").write_bytes(b"\xff\xfe\x00garbage")
    monkeypatch.setattr(rr, "_find_package_json", lambda: pkg / "package.json")
    rs = _REAL_CURRENT_RULESET(_no_cli, refresh=True)
    assert rs.source == "unavailable" and rs.reason


def test_the_parse_is_cached_until_the_dist_file_changes(tmp_path, monkeypatch):
    pkg = _package(tmp_path)
    dist = pkg / "dist" / "config-reload-plan-aaa.mjs"
    monkeypatch.setattr(rr, "_find_package_json", lambda: pkg / "package.json")
    monkeypatch.setattr(rr, "_PARSE_CACHE", {})
    parses = []
    real = rr.parse_reload_rules
    monkeypatch.setattr(rr, "parse_reload_rules", lambda src: parses.append(1) or real(src))
    _REAL_CURRENT_RULESET(_no_cli)
    _REAL_CURRENT_RULESET(_no_cli)
    assert len(parses) == 1
    dist.write_text(_move_prefix(DIST_TEXT, "tools", "restart"), encoding="utf-8")   # size and mtime change
    rs = _REAL_CURRENT_RULESET(_no_cli)
    assert len(parses) == 2
    assert rr.classify(["tools.profile"], ruleset=rs) == {"tools.profile": "restart"}


# --- TS-RR-18: no code path pins or compares a version -----------------------------------------------------------------

def test_nothing_in_the_kit_pins_or_compares_an_openclaw_version():
    skip = {pathlib.Path(__file__).resolve()}
    hits = []
    for root in ("scripts", "tests"):
        for path in (REPO / root).rglob("*.py"):
            if path.resolve() in skip:
                continue
            text = path.read_text(encoding="utf-8")
            if "PINNED_OPENCLAW_VERSION" in text:
                hits.append(f"{path.relative_to(REPO)}: PINNED_OPENCLAW_VERSION")
    module = (REPO / "scripts" / "ai_resources" / "openclaw_reload_rules.py").read_text(encoding="utf-8")
    for bad in ("== BASELINE_READ_FROM", "!= BASELINE_READ_FROM", "BASELINE_READ_FROM ==", "BASELINE_READ_FROM !="):
        if bad in module:
            hits.append(f"openclaw_reload_rules.py: {bad}")
    assert hits == []


def test_leaf_paths_counts_none_and_lists():
    patch = {"a": {"b": None, "c": [1, 2], "d": {"e": 1}}, "f": "x"}
    assert rr.leaf_paths(patch) == ["a.b", "a.c", "a.d.e", "f"]


def test_split_patch_round_trip():
    patch = {"gateway": {"bind": "tailnet", "trustedProxies": ["10.0.0.0/8"],
                         "controlUi": {"allowedOrigins": ["https://x"]}},
             "tools": {"profile": "coding"}}
    restart = rr.restart_paths(patch, ruleset=RS)
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

    assert rr.read_installed_version(ok) == "2026.9.9"
    assert rr.read_installed_version(ok) == "2026.9.9"
    assert len(calls) == 1  # cached

    assert rr.read_installed_version(lambda argv, **kw: (1, "boom")) is None
    assert rr.read_installed_version(lambda argv, **kw: (0, "garbage")) is None


def test_reload_mode_of():
    assert rr.reload_mode_of({"gateway": {"reload": {"mode": "off"}}}) == "off"
    assert rr.reload_mode_of({"gateway": {}}) is None
    assert rr.reload_mode_of(None) is None


# --- E3: the version comes from package.json, not the CLI --------------------------------------------------------------

def _pkg(tmp_path, body):
    p = tmp_path / "package.json"
    p.write_text(body, encoding="utf-8")
    return p


def test_package_json_version_never_calls_the_cli(tmp_path, monkeypatch):
    pj = _pkg(tmp_path, '{"name": "openclaw", "version": "2026.9.9"}')
    monkeypatch.setattr(rr, "_find_package_json", lambda: pj)

    def boom(argv, **kw):
        raise AssertionError("the CLI must not be called when package.json answers")

    assert rr.read_installed_version(boom, refresh=True) == "2026.9.9"


def test_a_missing_package_json_falls_back_to_the_cli(monkeypatch):
    monkeypatch.setattr(rr, "_find_package_json", lambda: None)
    assert rr.read_installed_version(lambda argv, **kw: (0, "OpenClaw 2026.9.9 (x)"), refresh=True) == "2026.9.9"


def test_garbage_package_json_and_garbage_cli_give_none(tmp_path, monkeypatch):
    pj = _pkg(tmp_path, '{"name": "openclaw", "version": "not-a-version"}')
    monkeypatch.setattr(rr, "_find_package_json", lambda: pj)
    assert rr.read_installed_version(lambda argv, **kw: (0, "garbage"), refresh=True) is None
    pj2 = _pkg(tmp_path, "{{{ not json")
    monkeypatch.setattr(rr, "_find_package_json", lambda: pj2)
    assert rr.read_installed_version(lambda argv, **kw: (1, "boom"), refresh=True) is None
    pj3 = _pkg(tmp_path, '{"name": "other", "version": "2026.9.9"}')
    monkeypatch.setattr(rr, "_find_package_json", lambda: pj3)
    assert rr.read_installed_version(lambda argv, **kw: (1, "boom"), refresh=True) is None


def test_locate_package_json_resolves_the_symlinked_entry_point(tmp_path, monkeypatch):
    import shutil
    pkg = tmp_path / "lib" / "node_modules" / "openclaw"
    pkg.mkdir(parents=True)
    (pkg / "openclaw.mjs").write_text("", encoding="utf-8")
    (pkg / "package.json").write_text('{"name": "openclaw", "version": "2026.9.9"}', encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "openclaw").symlink_to(pkg / "openclaw.mjs")
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: str(bin_dir / n))
    found = rr.locate_package_json()
    assert found is not None and found.name == "package.json" and found.parent.samefile(pkg)
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: None)
    assert rr.locate_package_json() is None


def test_the_two_resource_guard_keys_are_hot():
    """The keys the 2026-10-10 setup wrote: the watch must never route them to a drained window (D1 b)."""
    kinds = rr.classify(["agents.defaults.timeoutSeconds", "mcp.sessionIdleTtlMs"], ruleset=RS)
    assert kinds == {"agents.defaults.timeoutSeconds": "hot", "mcp.sessionIdleTtlMs": "hot"}

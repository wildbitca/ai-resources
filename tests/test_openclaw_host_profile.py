"""The canonical host configuration (profiles/openclaw-host.json5) and the patch built from it.

The fixture is a synthetic openclaw.json shaped like a real host (a haiku orchestrator, an
engine-owned worker agent, a Telegram allowlist and topic bindings). Nothing here calls
`openclaw`: `apply_patch` is recorded elsewhere, and these tests pin what the patch CONTAINS.
Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import copy
import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import openclaw_host as host  # noqa: E402
from ai_resources.setup import profiles  # noqa: E402
from ai_resources.setup.cockpits import openclaw  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "openclaw_host_config.json"
PROFILE = REPO / "profiles" / "openclaw-host.json5"
VALUES = {"DOMAIN": "ai.example.org", "POD_CIDR": "10.9.0.0/24", "HOME": "/home/u"}

# Every documented key (T04, T05, T14, T20, T26, T28, T29, T31), as dotted paths.
CANONICAL = {
    "agents.defaults.heartbeat.every",
    "agents.defaults.timeoutSeconds", "mcp.sessionIdleTtlMs",      # ADR-0004 resource guards
    "agents.entries.app.model.primary", "agents.entries.infra.model.primary",
    "agents.entries.main.thinkingDefault", "agents.entries.main.heartbeat.every",
    "tools.profile", "tools.alsoAllow",
    "logging.file", "logging.maxFileBytes",
    "gateway.bind", "gateway.publicOrigin", "gateway.trustedProxies", "gateway.allowRealIpFallback",
    "gateway.controlUi.allowedOrigins",
    "plugins.entries.device-pair.config.publicUrl",
    "channels.telegram.streaming.mode",
    "channels.telegram.streaming.preview.toolProgress",
    "channels.telegram.streaming.progress.toolProgress",
    "channels.telegram.streaming.progress.commentary",
    "channels.telegram.streaming.progress.maxLines",
}


@pytest.fixture
def doc():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def no_gh_token(monkeypatch, tmp_path):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(host.Path, "home", classmethod(lambda cls: tmp_path))


def build(doc, *, overrides=host.FORCE_ALL, **over):
    """The patch against `doc`. The fixture is a customised host, so by default every profile key is
    forced (`force: ["*"]`): these tests pin WHAT the profile says. Fill-only is tested separately."""
    return host.build_host_patch(host.load_host_profile(), doc, {**VALUES, **over}, overrides=overrides)


def paths(result) -> set[str]:
    return {".".join(c["path"]) for c in result["changes"]}


# --- AC-8.1: it is not a routing profile ---------------------------------------------------------------

def test_the_profile_is_json5_so_the_routing_picker_never_lists_it():
    assert PROFILE.suffix == ".json5" and PROFILE.is_file()
    assert not (REPO / "profiles" / "openclaw-host.yaml").exists()
    for mode in (None, "single-model", "multi-model"):
        for backend in (None, "litellm", "openrouter"):
            assert "openclaw-host" not in profiles.list_profiles(mode, backend)


# --- AC-8.2: key coverage ------------------------------------------------------------------------------------

def test_every_documented_key_is_built_and_nothing_else(doc):
    result = build(doc)
    assert paths(result) == CANONICAL
    assert result["skipped"] and all("GH_TOKEN" in r for r in result["skipped"])


def test_the_secret_ref_is_sent_only_when_the_token_is_resolvable(doc, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "ghp_SECRETVALUE")
    result = build(doc)
    assert "gateway.controlUi.github.token" in paths(result)
    ref = result["patch"]["gateway"]["controlUi"]["github"]["token"]
    assert ref == {"source": "env", "provider": "default", "id": "GH_TOKEN"}
    assert "SECRETVALUE" not in json.dumps(result["patch"])  # a reference, never the value


def test_a_second_run_against_the_patched_config_sends_nothing(doc):
    first = build(doc)
    patched = host_apply(doc, first["patch"])
    second = build(patched)
    assert second["patch"] == {} and second["changes"] == []


def host_apply(doc, patch):
    """What `openclaw config patch` does: objects merge, arrays/scalars replace, null deletes."""
    out = copy.deepcopy(doc)

    def merge(dst, src):
        for k, v in src.items():
            if v is None:
                dst.pop(k, None)
            elif isinstance(v, dict) and isinstance(dst.get(k), dict):
                merge(dst[k], v)
            else:
                dst[k] = copy.deepcopy(v)
    merge(out, patch)
    return out


def test_arrays_are_unioned_with_what_the_operator_already_had(doc):
    patched = host_apply(doc, build(doc)["patch"])
    assert patched["tools"]["alsoAllow"] == ["group:web", "group:messaging"]
    assert patched["gateway"]["trustedProxies"] == ["192.0.2.0/24", "10.9.0.0/24"]
    assert patched["gateway"]["controlUi"]["allowedOrigins"] == ["https://old.example.org",
                                                                 "https://ai.example.org"]


# --- AC-8.3: no orchestrator on haiku -----------------------------------------------------------------------------

def test_no_agent_entry_ends_up_on_haiku_while_the_utility_default_stays_untouched(doc):
    patched = host_apply(doc, build(doc)["patch"])
    for aid, entry in patched["agents"]["entries"].items():
        primary = (entry.get("model") or {}).get("primary", "")
        assert "haiku" not in primary, aid
    assert patched["agents"]["defaults"]["model"] == doc["agents"]["defaults"]["model"]
    assert "model" not in build(doc)["patch"]["agents"]["defaults"]  # engine-owned key, never sent


def test_a_deliberate_model_choice_and_the_engine_owned_worker_are_left_alone(doc):
    patched = host_apply(doc, build(doc)["patch"])
    assert patched["agents"]["entries"]["docs"]["model"]["primary"] == "anthropic/claude-opus-5"
    assert patched["agents"]["entries"]["claude"]["model"]["primary"] == "claude-kit/claude-sonnet-5"
    assert patched["agents"]["entries"]["app"]["model"]["primary"] == "anthropic/claude-sonnet-5"


# --- AC-8.4: channels safety ------------------------------------------------------------------------------------------

def test_the_patch_touches_only_channels_telegram_streaming(doc):
    patch = build(doc)["patch"]
    assert set(patch["channels"]) == {"telegram"} and set(patch["channels"]["telegram"]) == {"streaming"}
    text = json.dumps(patch)
    for forbidden in ("allowedUsers", "allowFrom", "groups", "topics", "agentId", "ownerAllowFrom", "bindings"):
        assert forbidden not in text


def test_applying_the_patch_leaves_the_allowlist_and_topic_bindings_byte_identical(doc):
    patched = host_apply(doc, build(doc)["patch"])
    assert patched["channels"]["telegram"]["allowedUsers"] == doc["channels"]["telegram"]["allowedUsers"]
    assert patched["channels"]["telegram"]["groups"] == doc["channels"]["telegram"]["groups"]
    assert patched["commands"] == doc["commands"]


@pytest.mark.parametrize("bad", [
    {"channels": {"telegram": {"allowedUsers": [1]}}},
    {"channels": {"telegram": {"groups": {}}}},
    {"channels": {"slack": {"streaming": {}}}},
    {"channels": {"telegram": {"streaming": {}, "allowedUsers": []}}},
])
def test_a_patch_that_reaches_beyond_streaming_is_refused(bad):
    with pytest.raises(ValueError):
        host.assert_channels_safe(bad)


@pytest.mark.parametrize("path", ["channels", "channels.telegram", "channels.telegram.streaming"])
def test_no_replace_path_may_name_the_channels_namespace(path):
    with pytest.raises(ValueError):
        host.assert_channels_safe({}, [path])


def test_the_cockpit_writer_enforces_the_same_invariant_before_calling_openclaw(monkeypatch):
    calls = []
    monkeypatch.setattr(openclaw, "_openclaw", lambda *a, **k: calls.append(a) or (0, ""))
    with pytest.raises(ValueError):
        openclaw.apply_patch({"channels": {"telegram": {"allowedUsers": [1]}}})
    with pytest.raises(ValueError):
        openclaw.apply_patch({"agents": {}}, replace_paths=["channels.telegram"])
    assert calls == []


# --- AC-8.5: hygiene ---------------------------------------------------------------------------------------------------------

def test_logging_resolves_outside_tmp_and_no_mcp_server_is_latest(doc):
    patch = build(doc)["patch"]
    assert patch["logging"]["file"] == "/home/u/.openclaw/logs/gateway.log"
    assert not patch["logging"]["file"].startswith("/tmp")
    assert "@latest" not in json.dumps(host.load_host_profile())
    assert "servers" not in patch.get("mcp", {}) and "apps" not in patch.get("mcp", {})  # only the ADR-0004 leaf


def test_the_profile_carries_no_host_literal():
    text = PROFILE.read_text(encoding="utf-8")
    for literal in ("wildbit", "bithome", "7961376547", "10.42.", "/home/bitgandtter"):
        assert literal not in text
    assert not [c for c in text if c.isalpha() and ord(c) > 127]


def test_unresolved_markers_skip_their_keys_instead_of_sending_a_literal(doc):
    result = build(doc, DOMAIN="", POD_CIDR="")
    text = json.dumps(result["patch"])
    assert "@" not in text
    assert "gateway.publicOrigin" not in paths(result)
    assert any("DOMAIN" in r for r in result["skipped"])


def test_a_plugin_that_is_not_configured_is_not_created(doc):
    del doc["plugins"]["entries"]["device-pair"]
    result = build(doc)
    assert "plugins.entries.device-pair.config.publicUrl" not in paths(result)
    assert any("device-pair" in r for r in result["skipped"])


def test_an_unpinned_mcp_server_is_reported_not_rewritten(doc):
    assert host.mcp_latest_findings(doc) == ["supa"]
    assert "servers" not in build(doc)["patch"].get("mcp", {})


# --- teardown reversal ------------------------------------------------------------------------------------------------------------

def test_restore_puts_every_changed_leaf_back_exactly(doc):
    result = build(doc)
    patched = host_apply(doc, result["patch"])
    restore, replace = host.restore_patch(result["changes"])
    assert host_apply(patched, restore) == doc
    assert replace == []


def test_restore_deletes_leaves_the_kit_created(doc):
    result = build(doc)
    restore, _ = host.restore_patch(result["changes"])
    assert restore["gateway"]["publicOrigin"] is None
    assert restore["tools"]["profile"] == "minimal"


def test_a_credential_leaf_records_that_it_existed_and_nothing_of_its_value(doc, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "x")
    doc["gateway"]["controlUi"]["github"] = {"token": "ghp_LITERALSECRET"}
    result = build(doc)
    [change] = [c for c in result["changes"] if c["path"][-1] == "token"]
    assert change == {"path": ["gateway", "controlUi", "github", "token"], "previous": None, "had": True,
                      "secret": True, "action": "forced"}
    assert "ghp_LITERALSECRET" not in json.dumps(result["changes"])
    restore, _ = host.restore_patch(result["changes"])
    assert "github" not in restore.get("gateway", {}).get("controlUi", {}), "a credential is never deleted or rewritten"


@pytest.mark.parametrize("path,secret", [
    (["gateway", "controlUi", "github", "token"], True), (["x", "apiKey"], True), (["x", "api_key"], True),
    (["x", "password"], True), (["x", "clientSecret"], True), (["tools", "profile"], False),
    (["gateway", "trustedProxies"], False)])
def test_which_leaves_count_as_credentials(path, secret):
    assert host.is_secret_path(path) is secret


def test_restore_never_names_channels_in_a_replace_path(doc, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "x")
    result = build(doc)
    _restore, replace = host.restore_patch(result["changes"])
    assert not any(p.startswith("channels") for p in replace)


# --- validation of what the wizard asks ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("values,ok", [
    ({"DOMAIN": "ai.example.org"}, True), ({"DOMAIN": "https://ai.example.org"}, False),
    ({"DOMAIN": "localhost"}, False), ({"POD_CIDR": "10.42.0.0/24"}, True), ({"POD_CIDR": "10.42.0"}, False),
    ({"OWNER_TELEGRAM_ID": "123456789"}, True), ({"OWNER_TELEGRAM_ID": "@me"}, False),
    ({"BACKUP_DIR": "/srv/openclaw-backups"}, True), ({"BACKUP_DIR": "relative/dir"}, False),
    ({"BACKUP_DIR": "/srv/a b"}, False), ({"BACKUP_DIR": "/srv/$(id)"}, False), ({}, True),
])
def test_host_values_are_validated(values, ok):
    assert (host.validate_host_values(values) == []) is ok


def test_the_json5_loader_keeps_urls_and_drops_comments_and_trailing_commas():
    doc = host.load_json5('{ // a comment\n "u": "https://x.org/a", /* b */ "l": [1, 2,], }')
    assert doc == {"u": "https://x.org/a", "l": [1, 2]}


# --- E2: `model` may be a string (`openclaw agents add --model` writes that form) ----------------------------

@pytest.mark.parametrize("raw,expands", [
    ("anthropic/claude-haiku-4-5", True),
    ({"primary": "anthropic/claude-haiku-4-5"}, True),
    ("anthropic/claude-sonnet-5", False),
    ({"primary": "anthropic/claude-sonnet-5"}, False),
    (None, True),
])
def test_expand_wildcards_reads_both_spellings_of_model(raw, expands):
    tree = {"agents": {"entries": {"*": {"model": {"primary": "anthropic/claude-sonnet-5"}}}}}
    entry = {} if raw is None else {"model": raw}
    doc = {"agents": {"entries": {"app": entry}}}
    out = host.expand_wildcards(tree, doc)
    assert ("app" in out["agents"]["entries"]) is expands


def test_model_helpers_are_total():
    assert host.model_spec("a/b") == {"primary": "a/b"}
    assert host.model_spec({"primary": "a/b", "fallbacks": []}) == {"primary": "a/b", "fallbacks": []}
    for junk in (None, 3, [], ""):
        assert host.model_primary({"model": junk}) is None
    assert host.model_primary(None) is None
    assert host.model_primary({"model": "a/b"}) == "a/b"


# --- fill-only (ADR-0003): the profile never overwrites a value the operator set -----------------------------

def _fill_only(doc, **over):
    return build(doc, overrides=None, **over)


def test_an_operator_bind_is_kept_not_overwritten(doc):
    doc["gateway"]["bind"] = "loopback"
    built = _fill_only(doc, TAILNET="1")
    assert "bind" not in built["patch"].get("gateway", {})
    assert "gateway.bind" in built["kept"]
    assert not any(c["path"] == ["gateway", "bind"] for c in built["changes"])


def test_force_writes_the_profile_value_and_records_the_previous_one(doc):
    doc["gateway"]["bind"] = "loopback"
    built = build(doc, overrides={"keep": frozenset(), "force": frozenset({"gateway.bind"})}, TAILNET="1")
    assert built["patch"]["gateway"]["bind"] == "tailnet"
    [ch] = [c for c in built["changes"] if c["path"] == ["gateway", "bind"]]
    assert ch["previous"] == "loopback" and ch["had"] is True and ch["action"] == "forced"
    assert "gateway.bind" in built["forced"]


def test_force_accepts_a_prefix_and_a_wildcard_segment(doc):
    built = build(doc, overrides={"keep": frozenset(), "force": frozenset({"tools"})})
    assert built["patch"]["tools"]["profile"] == "coding"
    built = build(doc, overrides={"keep": frozenset(), "force": frozenset({"agents.entries.*.heartbeat"})})
    assert built["patch"]["agents"]["entries"]["main"]["heartbeat"]["every"] == "2h"
    assert "profile" not in built["patch"].get("tools", {})       # not forced, so the operator's value stays


def test_a_customised_host_keeps_every_differing_value_and_fills_the_rest(doc):
    built = _fill_only(doc, TAILNET="1")
    kept = set(built["kept"])
    assert {"tools.profile", "gateway.bind", "logging.file"} <= kept
    filled = set(built["filled"])
    assert "gateway.publicOrigin" in filled              # absent in the fixture: filled
    assert "gateway.trustedProxies" in filled            # array union is an add-only fill
    assert not kept & filled
    # Whatever is sent is only ever an addition: applying it never changes a kept value.
    patched = host_apply(doc, built["patch"])
    assert patched["gateway"]["bind"] == doc["gateway"]["bind"]
    assert patched["tools"]["profile"] == doc["tools"]["profile"]


def test_a_haiku_primary_is_still_replaced_because_of_t29(doc):
    built = _fill_only(doc)
    assert built["patch"]["agents"]["entries"]["app"]["model"]["primary"].startswith("anthropic/")
    assert "agents.entries.app.model.primary" in built["forced"]
    assert "agents.entries.docs.model.primary" not in built["forced"] + built["filled"]   # a deliberate opus stays


def test_keep_blocks_an_array_union(doc):
    built = build(doc, overrides={"keep": frozenset({"gateway.trustedProxies"}), "force": frozenset()})
    assert "trustedProxies" not in built["patch"]["gateway"]
    assert "gateway.trustedProxies" in built["kept"]


def test_a_never_customised_host_gets_the_same_patch_either_way():
    # `{}` has nothing to keep, so fill-only and the 2.0.x behaviour agree byte for byte.
    a = host.build_host_patch(host.load_host_profile(), {}, {**VALUES, "TAILNET": "1"})
    b = host.build_host_patch(host.load_host_profile(), {}, {**VALUES, "TAILNET": "1"}, overrides=host.FORCE_ALL)
    assert json.dumps(a["patch"], sort_keys=True) == json.dumps(b["patch"], sort_keys=True)
    assert a["kept"] == []


def test_the_second_run_sends_nothing_fill_only(doc):
    first = _fill_only(doc, TAILNET="1")
    patched = host_apply(doc, first["patch"])
    second = _fill_only(patched, TAILNET="1")
    assert second["patch"] == {} and second["changes"] == []


def test_restore_ignores_kept_records(doc):
    changes = [{"path": ["gateway", "bind"], "previous": "loopback", "had": True, "action": "kept"},
               {"path": ["tools", "profile"], "previous": "minimal", "had": True, "action": "forced"}]
    restore, _ = host.restore_patch(changes)
    assert restore == {"tools": {"profile": "minimal"}}


# --- the overrides file ------------------------------------------------------------------------------------------

def _overrides(tmp_path, text):
    p = tmp_path / "kit-host-overrides.json5"
    p.write_text(text, encoding="utf-8")
    return p


def test_a_missing_overrides_file_means_no_overrides(tmp_path):
    got = host.load_host_overrides(tmp_path / "nope.json5")
    assert got == {"keep": frozenset(), "force": frozenset()}


def test_a_valid_overrides_file_loads_with_comments(tmp_path):
    p = _overrides(tmp_path, '// mine\n{"keep": ["gateway.controlUi"], "force": ["gateway.bind", "tools",],}\n')
    got = host.load_host_overrides(p)
    assert got == {"keep": frozenset({"gateway.controlUi"}), "force": frozenset({"gateway.bind", "tools"})}


@pytest.mark.parametrize("text", ['{"force": ["gateway bind"]}', '{"force": ["a..b"]}', '{"force": [3]}',
                                  '{"force": "gateway.bind"}', '{"nope": []}', '[]', 'not json'])
def test_invalid_overrides_are_refused_naming_the_file(tmp_path, text):
    p = _overrides(tmp_path, text)
    with pytest.raises(ValueError, match="kit-host-overrides.json5"):
        host.load_host_overrides(p)


def test_a_secret_path_cannot_be_listed_and_its_value_is_never_printed(tmp_path):
    p = _overrides(tmp_path, '{"force": ["gateway.controlUi.github.token"]}')
    with pytest.raises(ValueError) as e:
        host.load_host_overrides(p)
    assert "gateway.controlUi.github.token" in str(e.value) and "credential" in str(e.value)


def test_a_path_in_both_lists_is_refused(tmp_path):
    p = _overrides(tmp_path, '{"keep": ["gateway.bind"], "force": ["gateway.bind"]}')
    with pytest.raises(ValueError, match="both"):
        host.load_host_overrides(p)


def test_the_overrides_file_is_never_shell_sourced():
    # kit-host.env is sourced by bash; the overrides file must stay out of it and out of _check_env_pair.
    assert host.host_overrides_path().name == "kit-host-overrides.json5"
    assert host.host_overrides_path() != host.HOST_ENV_PATH


# --- ADR-0004: the resource guards ---------------------------------------------------------------------------------

def test_profile_guard_keys_fill_an_empty_config_and_classify_hot():
    from ai_resources import openclaw_reload_rules as rr
    built = host.build_host_patch(host.load_host_profile(), {}, dict(VALUES))
    assert built["patch"]["mcp"] == {"sessionIdleTtlMs": 1800000}
    assert built["patch"]["agents"]["defaults"]["timeoutSeconds"] == 14400
    assert rr.restart_paths({"mcp": {"sessionIdleTtlMs": 1800000},
                             "agents": {"defaults": {"timeoutSeconds": 14400}}},
                            openclaw_version=rr.PINNED_OPENCLAW_VERSION) == []
    assert "mcp.sessionIdleTtlMs" in built["filled"] and "agents.defaults.timeoutSeconds" in built["filled"]


def test_profile_never_sends_mcp_apps_or_servers():
    built = host.build_host_patch(host.load_host_profile(), {}, dict(VALUES))
    assert set(built["patch"]["mcp"]) == {"sessionIdleTtlMs"}


def test_an_operator_value_for_a_guard_key_is_kept_and_listed_including_zero():
    doc = {"mcp": {"sessionIdleTtlMs": 0}, "agents": {"defaults": {"timeoutSeconds": 600}}}
    built = host.build_host_patch(host.load_host_profile(), doc, dict(VALUES))
    assert "mcp" not in built["patch"] and "timeoutSeconds" not in built["patch"].get("agents", {}).get("defaults", {})
    assert {"mcp.sessionIdleTtlMs", "agents.defaults.timeoutSeconds"} <= set(built["kept"])


def test_resource_guards_off_fills_neither_key(monkeypatch):
    monkeypatch.delenv("OPENCLAW_RESOURCE_GUARDS", raising=False)
    built = host.build_host_patch(host.load_host_profile(), {}, {**VALUES, "RESOURCE_GUARDS": "off"})
    assert "mcp" not in built["patch"]
    assert "timeoutSeconds" not in built["patch"]["agents"]["defaults"]
    assert any("resource guards are off" in n for n in built["skipped"])
    # the process environment wins over the host env file value
    monkeypatch.setenv("OPENCLAW_RESOURCE_GUARDS", "off")
    built = host.build_host_patch(host.load_host_profile(), {}, {**VALUES, "RESOURCE_GUARDS": "on"})
    assert "mcp" not in built["patch"]
    monkeypatch.setenv("OPENCLAW_RESOURCE_GUARDS", "on")
    built = host.build_host_patch(host.load_host_profile(), {}, {**VALUES, "RESOURCE_GUARDS": "off"})
    assert built["patch"]["mcp"] == {"sessionIdleTtlMs": 1800000}


def test_resource_guards_env_is_an_allowed_host_env_key():
    host._check_env_pair("OPENCLAW_RESOURCE_GUARDS", "off")

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


def build(doc, **over):
    return host.build_host_patch(host.load_host_profile(), doc, {**VALUES, **over})


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
    assert "mcp" not in patch


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
    assert "mcp" not in build(doc)["patch"]


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
                      "secret": True}
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

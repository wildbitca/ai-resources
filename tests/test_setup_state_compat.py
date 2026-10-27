"""Old setup-state files keep their meaning (ADR-0009, AC1).

The fixtures under tests/fixtures/setup_state/ are hand-built state files in the shape older kits wrote. Each is
loaded and saved without edits (the text must be byte-equal apart from `last_run`), and each is replayed through
`setup --non-interactive --dry-run`, which must leave mode, backend, cli_team, profile.*, lanes, lane_acks and
cockpits as they were. The state schema does not change in this feature; a failure here means it did."""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import types

import pytest

from ai_resources.setup import credentials, detection, state, tools, ui, wizard

FIX = pathlib.Path(__file__).parent / "fixtures" / "setup_state"
NAMES = sorted(p.stem for p in FIX.glob("*.yaml"))
# Nothing: a non-interactive run leaves every key of every fixture as it was (AC1). Two things used to drift and
# were fixed by ADR-0009: the single-model profile step rebuilt `profile` (dropping a customized or composed team
# and the lanes with their acknowledgements), and the OpenClaw prompt normalised `openclaw.model` and filled
# `voice_correction` on every run (it is now asked on the first configure only).
# ADR-0015 (round 3): the one deliberate drift is the per-cockpit sign-in migration. `cockpit_auth` is filled
# (additive, fill-only) and the old global fields are derived from it: the team is always on (`cli_team`), and a saved
# gateway state (`mode: multi-model`) becomes the single-model team with the gateway profile replaced by the default.
# Nothing else may move.
MIGRATION = ("cockpit_auth", "cli_team")
GATEWAY_MIGRATION = MIGRATION + ("mode", "profile.name")
KNOWN_TODAY: dict[str, tuple[str, ...]] = {
    name: (GATEWAY_MIGRATION if name.startswith("multi_") else MIGRATION) for name in
    ("account_picked_selection", "cli_lanes_anthropic_ack", "cli_lanes_google_ack", "cli_team_balanced",
     "cli_team_customized", "composed_team", "gemini_cockpit", "multi_litellm", "multi_openrouter",
     "live_anthropic_saved_antigravity", "openclaw-pre-image", "openclaw_host_voice", "round2_fields",
     "single_claude_native")}
_LAST_RUN = re.compile(r"^last_run: .*$", re.M)


def test_the_fixture_matrix_is_complete():
    assert NAMES == sorted([
        "account_picked_selection", "cli_lanes_anthropic_ack", "cli_lanes_google_ack", "cli_team_balanced",
        "cli_team_customized", "composed_team", "gemini_cockpit", "multi_litellm", "multi_openrouter",
        "live_anthropic_saved_antigravity", "openclaw-pre-image", "openclaw_host_voice", "round2_fields",
        "single_claude_native"])


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "xdg").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(h / "xdg"))
    return h


def _seed(name):
    state.config_root().mkdir(parents=True, exist_ok=True)
    shutil.copy(FIX / f"{name}.yaml", state.state_path())


OLD_LANES = ("cli_lanes_anthropic_ack", "cli_lanes_google_ack")      # pre-3.2 `profile.lanes` shape (ADR-0019 migration)


@pytest.mark.parametrize("name", [n for n in NAMES if n not in OLD_LANES])
def test_load_then_save_is_byte_equal(home, name):
    _seed(name)
    before = state.state_path().read_text(encoding="utf-8")
    state.save(state.load())
    after = state.state_path().read_text(encoding="utf-8")
    assert _LAST_RUN.sub("", after) == _LAST_RUN.sub("", before)


@pytest.mark.parametrize("name", NAMES)
def test_non_interactive_dry_run_leaves_the_answers_unchanged(home, monkeypatch, name):
    _seed(name)
    want = state._to_dict(state.load())
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", False)
    monkeypatch.setattr(ui, "require_deps", lambda: None)
    monkeypatch.setattr(ui, "banner", lambda *a, **k: None)
    monkeypatch.setattr(ui, "detected_table", lambda rows: None)
    monkeypatch.setattr(detection, "detect_all_cockpits", lambda: {
        cid: types.SimpleNamespace(installed=True, version="1.0", binary_path=f"/opt/{cid}", name=cid)
        for cid in want["cockpits"]})
    monkeypatch.setattr(detection, "detect_container_runtime",
                        lambda: ("docker", types.SimpleNamespace(installed=False, version="", binary_path="")))
    monkeypatch.setattr(tools, "offer", lambda *a, **k: [])
    monkeypatch.setattr(credentials, "get_key", lambda *a, **k: "stored")
    monkeypatch.setattr(credentials, "get_claude_code_keychain_key", lambda: None)
    seen = {}
    monkeypatch.setattr(wizard, "_step9_apply", lambda s, dry_run=False: seen.setdefault("s", s) and 0)
    monkeypatch.setattr(wizard, "_step10_verify", lambda s, t: 0)
    monkeypatch.setattr(wizard, "_print_completion", lambda s: None)
    args = argparse.Namespace(dry_run=True, yes=False, non_interactive=True, profile="", lanes=False, no_lanes=False,
                              lanes_dry_run=False, lanes_orchestrator=[], allow_unverified=False)
    rc = wizard.run(args)
    assert rc == 0 and "s" in seen
    got = state._to_dict(seen["s"])
    a, b = _flat(want), _flat(got)
    drift = {k for k in set(a) | set(b) if a.get(k) != b.get(k)}
    # The transient stash of the ticked tools is not part of a saved file, and `last_run` is stamped on save.
    drift = {k for k in drift if not k.startswith("profile.customizations.__targets__") and k != "last_run"}
    allowed = [pfx for pfx in KNOWN_TODAY.get(name, ())]
    unexplained = sorted(k for k in drift if not any(k == p or k.startswith(p + ".") for p in allowed))
    assert not unexplained, unexplained


def _flat(doc, prefix=""):
    if not isinstance(doc, dict):
        return {prefix: doc}
    out = {}
    for k, v in doc.items():
        out.update(_flat(v, f"{prefix}.{k}" if prefix else k))
    return out


# --- ADR-0014 (round 2): additive fields ---------------------------------------------------------------------------

ROUND2_TRACKING = ("hygiene_disabled", "claude_md_migrations", "persona_domains_active", "agy_mcp_added")


def test_the_schema_version_stays_1_0():
    assert state.SCHEMA_VERSION == "1.0"


@pytest.mark.parametrize("name", [n for n in NAMES if n != "round2_fields"])
def test_a_state_from_before_round_2_gains_no_new_key(home, name):
    _seed(name)
    doc = state._to_dict(state.load())
    for key in ROUND2_TRACKING:
        assert key not in doc["tracking"]
    assert "notification_route_changes" not in doc["openclaw"] and "roles_seen" not in doc["profile"]


def test_the_round_2_fields_are_loaded_and_saved_unchanged(home):
    _seed("round2_fields")
    s = state.load()
    assert s.tracking.hygiene_disabled[0]["name"] == "grafana" and s.tracking.hygiene_disabled[0]["previous"]["command"] == "npx"
    assert s.tracking.claude_md_migrations[0]["kind"] == "skill"
    assert s.tracking.persona_domains_active == ["angular"]
    assert s.tracking.agy_mcp_added["engram"]["command"] == "engram"
    assert s.openclaw.notification_route_changes[0]["path"] == "bindings"
    assert s.profile.roles_seen["tester"] == {"model": "haiku", "fallbacks": ["sonnet"]}


def test_a_state_with_round_2_fields_survives_an_unrelated_save(home):
    _seed("round2_fields")
    s = state.load()
    s.profile.customized = False
    state.save(s)
    again = state.load()
    assert again.tracking.hygiene_disabled == s.tracking.hygiene_disabled
    assert again.tracking.claude_md_migrations == s.tracking.claude_md_migrations
    assert again.openclaw.notification_route_changes == s.openclaw.notification_route_changes


@pytest.mark.parametrize("name", NAMES)
def test_the_migration_fills_only_the_declared_sign_in_surface(home, name):
    """The drift above is exactly the migration: nothing outside `cockpit_auth` and the derived legacy fields moves."""
    from ai_resources.setup import cockpit_auth
    _seed(name)
    s = state.load()
    want = _flat(state._to_dict(s))
    cockpit_auth.migrate_legacy(s)
    cockpit_auth.apply_legacy_flags(s)
    got = _flat(state._to_dict(s))
    moved = sorted(k for k in set(want) | set(got) if want.get(k) != got.get(k))
    allowed = ("cockpit_auth", "cli_team", "cli_team_api", "mode")
    assert all(any(k == a or k.startswith(a + ".") for a in allowed) for k in moved), moved


@pytest.mark.parametrize("name", OLD_LANES)
def test_an_old_lanes_shape_loads_migrates_one_way_and_is_then_stable(home, name):
    """ADR-0019: `profile.lanes` agents/roles/second/held/... are dropped on load; orchestrators, routing and the gate
    stay, and a second load/save changes nothing; `gates` and `lane_acks` are dropped (ADR-0019 O1)."""
    import yaml
    _seed(name)
    old = yaml.safe_load(state.state_path().read_text(encoding="utf-8"))["profile"]
    assert "agents" in old["lanes"] or "roles" in old["lanes"]
    loaded = state.load()
    lanes = loaded.profile.lanes
    assert set(lanes) <= set(state.LANES_KEPT_KEYS) and state.lanes_enabled(loaded)
    assert lanes["orchestrators"] == (old["lanes"].get("orchestrators") or [])
    assert "gates" not in lanes and not hasattr(loaded.profile, "lane_acks")
    state.save(loaded)
    once = _LAST_RUN.sub("", state.state_path().read_text(encoding="utf-8"))
    state.save(state.load())
    assert _LAST_RUN.sub("", state.state_path().read_text(encoding="utf-8")) == once


def test_migrate_lanes_is_a_no_op_on_the_matrix_shape_and_on_nothing():
    p = state.ProfileState()
    assert state.migrate_lanes(p) is False and p.lanes is None
    p.lanes = {"orchestrators": ["main"], "routing": {"explore": {"primary": "claude-code:haiku"}}}
    assert state.migrate_lanes(p) is False
    p.lanes = {"orchestrators": ["main"], "gates": {"antigravity": True}}
    assert state.migrate_lanes(p) is True and p.lanes == {"orchestrators": ["main"]}
    p.lanes = {"agents": {"a": {}}, "roles": {"x": {}}, "second": {}, "held": [], "pool_caps": {}}
    assert state.migrate_lanes(p) is True and p.lanes == {"orchestrators": []}

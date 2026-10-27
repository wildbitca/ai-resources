"""ADR-0010 S4a/PD-6: preset ids, old names that still load, and the one resolver that knows the layout.

An alias is admitted only when the old and the new name resolve to the same runtime team with the lane gates closed;
the only permitted differences are the `name:` line and the ADR-0010 content fixes. Offline: no network, no real home."""
from __future__ import annotations

import argparse
import ast
import copy
import pathlib
import types

import pytest
import yaml

from ai_resources.setup import cockpit_auth, credentials, detection, profile_names, profiles, state, tools, ui, wizard

REPO = pathlib.Path(__file__).resolve().parents[1]
LEGACY = pathlib.Path(__file__).parent / "fixtures" / "setup_state" / "legacy_names"


def _team(doc: dict) -> dict:
    """What runs: every key except the display name and the prose (which names the renamed presets), and the read-only
    `role_effort` metadata: ADR-0015 completed that table in `balanced` and `performance` (it never moves a role to another
    model; `tests/test_effort_resolver.py` pins the new tables)."""
    return {k: v for k, v in doc.items() if k not in ("name", "description", "role_effort")}


SNAPSHOTS = pathlib.Path(__file__).parent / "fixtures" / "profile_snapshots"


def _frozen(old: str) -> dict:
    """The pre-rename preset, frozen from commit a256c9d: the old name no longer has a file of its own, so
    comparing `load_profile(old)` with `load_profile(new)` would read one file twice and could never fail."""
    return yaml.safe_load((SNAPSHOTS / f"{old}.yaml").read_text(encoding="utf-8"))


def _after_adr_0010_fixes(doc: dict) -> dict:
    """The only content changes ADR-0010 allows between a frozen preset and its renamed file: no agy writer
    candidate on a preset (the `agy:claude-sonnet-5-5` entries), and no `fallbacks` on the Opus gateway roles.
    Anything else that differs is a regression of the alias."""
    doc = copy.deepcopy(doc)
    for cands_by_role in ((doc.get("lanes") or {}).get("roles") or {}, ):
        for role, cands in list(cands_by_role.items()):
            if role != "code-reviewer":          # the Gemini review lane keeps its Claude-over-agy member (ADR-0019)
                cands_by_role[role] = [c for c in cands if c != "agy:claude-sonnet-5-5"]
    for role, spec in (doc.get("by_role") or {}).items():
        if isinstance(spec, dict) and spec.get("model") == "anthropic/claude-opus-5.5":
            spec.pop("fallbacks", None)
    return doc


def test_subscription_claude_55_is_not_an_alias_because_balanced_diverged():
    """ADR-0010 S6 gave balanced Gemini candidates; a host saved under the old name keeps its own file."""
    assert "subscription-claude-55" not in profile_names.ALIASES
    assert profile_names.LEGACY_PRESET["subscription-claude-55"] == "balanced"
    assert _team(profiles.load_profile("subscription-claude-55")) == _team(_frozen("subscription-claude-55"))


def _provider_paths(doc: dict) -> set[str]:
    """Every provider or data path a profile can reach: the by_role model providers and the lane candidates' family
    (`agy:gemini-...` is a Google path, `agy:claude-...` an Anthropic one over Antigravity)."""
    out: set[str] = set()
    for spec in (doc.get("by_role") or {}).values():
        model = spec.get("model") if isinstance(spec, dict) else spec
        if isinstance(model, str) and "/" in model:
            out.add(model.split("/", 1)[0])
    lanes = doc.get("lanes") or {}
    for table in (lanes.get("roles") or {}, lanes.get("second") or {}):
        for cands in table.values():
            for c in cands:
                if isinstance(c, str) and c.startswith("agy:"):
                    out.add("agy-" + c.split(":", 1)[1].split("-", 1)[0])
    return out


def _alias_admissible(old: dict, new: dict) -> bool:
    """ADR-0010 N4 wording: same runtime team with the gates closed AND no provider or data path the old one lacked."""
    return _team(_after_adr_0010_fixes(old)) == _team(new) and _provider_paths(new) <= _provider_paths(old)


def test_the_alias_rule_rejects_subscription_claude_55_for_balanced():
    old = _frozen("subscription-claude-55")
    new = profiles.load_profile("balanced", "single-model")
    assert "agy-gemini" in _provider_paths(new) - _provider_paths(old)
    assert not _alias_admissible(old, new)


@pytest.mark.parametrize("old,new", sorted(profile_names.ALIASES.items()))
def test_every_shipped_alias_is_admissible_under_the_rule(old, new):
    mode = "multi-model" if new.startswith("gateway/") else "single-model"
    assert _provider_paths(profiles.load_profile(new, mode)) <= _provider_paths(_frozen(old))


@pytest.mark.parametrize("old,new", sorted(profile_names.ALIASES.items()))
def test_an_old_name_and_its_new_name_resolve_to_the_same_runtime_team(old, new):
    """AC-S4a. The gates are closed in the shipped tree, so the lanes block is read as declared. The old side is the
    frozen pre-rename file, not a second read of the new one."""
    from ai_resources import agy_lane_gates
    assert not agy_lane_gates.all_proven()
    mode = "multi-model" if new.startswith("gateway/") else "single-model"
    frozen = _after_adr_0010_fixes(_frozen(old))
    now = profiles.load_profile(old)
    assert _team(frozen) == _team(now)
    assert _team(frozen) == _team(profiles.load_profile(new, mode))
    assert profiles.to_executors(frozen)["by_role"] == profiles.to_executors(now)["by_role"]


def test_a_bare_preset_id_without_a_mode_raises_the_named_error():
    """AC-S4e."""
    for preset in profile_names.PRESET_IDS:
        with pytest.raises(profile_names.ProfileModeRequired):
            profiles.load_profile(preset)
    assert profiles.load_profile("balanced", "single-model")["by_role"]


def test_a_preset_id_resolves_to_the_family_of_its_mode():
    root = pathlib.Path("/kit/profiles")
    single = profile_names.resolve("performance", "single-model", root)
    multi = profile_names.resolve("performance", "multi-model", root)
    assert single != multi
    assert profile_names.canonical("performance-claude-55") == "gateway/performance"
    # An old name already says which family it was, whatever mode the caller passes.
    assert profile_names.resolve("performance-claude-55", None, root) == multi
    assert profile_names.resolve("subscription-performance", None, root) == single


def test_old_names_load_without_a_mode():
    for old in profile_names.ALIASES:
        assert profiles.load_profile(old)["by_role"]


def test_the_renamed_preset_key_is_still_accepted():
    assert profile_names.preset_key("low-pricing") == "low-cost"
    assert profile_names.preset_key("balanced") == "balanced"
    s = state.SetupState()
    assert wizard.PRESET_PROFILES["cli"]["low-pricing"] == wizard.PRESET_PROFILES["cli"]["low-cost"]
    assert wizard.preset_profile(s, "low-pricing") == wizard.preset_profile(s, "low-cost")


def test_list_profiles_hides_aliases_and_shows_the_new_names():
    single = profiles.list_profiles(mode="single-model")
    multi = profiles.list_profiles(mode="multi-model")
    for old, new in profile_names.ALIASES.items():
        assert old not in single + multi
        assert new in (multi if new.startswith("gateway/") else single)
    for kept in ("claude-native", "balanced-claude-55", "openrouter-balanced", "all-claude", "measured-best"):
        assert kept in single + multi


def test_legacy_names_load_their_own_file():
    """AC-S4d: their content differs from the new preset, so they are not aliases."""
    for name in profile_names.LEGACY_PRESET:
        assert name not in profile_names.ALIASES
        assert profiles.load_profile(name)["name"] == name


def test_recommended_profile_for_a_state_with_no_profile_is_unchanged():
    assert profiles.saved_or_recommended("", "litellm") == "balanced-claude-55"
    assert profiles.saved_or_recommended("", "openrouter") == profiles.recommended_profile("openrouter")
    assert profiles.saved_or_recommended("x", "litellm") == "x"


def test_every_load_profile_call_site_is_accounted_for():
    """AC-S4e: a call that loads a name without a mode may only receive a name that is not a bare preset id. The
    sites below are the ones that do; any other call must pass the mode."""
    allowed_without_mode = {
        ("setup/cockpits/claude.py", "'claude-native'"),
        ("scoring/flow.py", "name"),                 # BASELINE_PROFILE / a shipped reference name
        ("setup/profiles.py", "name, mode"),         # load_profile itself
    }
    found = set()
    unaccounted = []
    for path in sorted((REPO / "scripts" / "ai_resources").rglob("*.py")):
        rel = path.relative_to(REPO / "scripts" / "ai_resources").as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "load_profile"):
                continue
            has_mode = len(node.args) >= 2 or any(k.arg == "mode" for k in node.keywords)
            if has_mode:
                continue
            key = (rel, ast.unparse(node.args[0]) if node.args else "")
            found.add(key)
            if key not in allowed_without_mode:
                unaccounted.append((rel, node.lineno, key[1]))
    assert not unaccounted, unaccounted


# --- saved states per old name and row ----------------------------------------------------------------------------

EXPECTED_PRESET = {
    "subscription-claude-55": "balanced", "subscription-performance": "performance",
    "subscription-economy": "low-cost", "performance-claude-55": "performance", "economy-claude-55": "low-cost",
    "claude-native": "balanced", "balanced-claude-55": "balanced", "openrouter-balanced": "balanced",
}
FIXTURES = sorted(LEGACY.glob("*.yaml"))


def test_one_fixture_per_old_name_and_row():
    assert len(FIXTURES) == 15
    seen = {p.stem.split("__")[1] for p in FIXTURES}
    assert seen == set(EXPECTED_PRESET)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    (h / "xdg").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(h / "xdg"))
    return h


def _seed(path):
    state.config_root().mkdir(parents=True, exist_ok=True)
    state.state_path().write_text(path.read_text(encoding="utf-8"), encoding="utf-8")


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_the_preset_question_defaults_to_the_preset_of_an_old_name(home, path):
    """AC-S4c: never Custom."""
    _seed(path)
    s = state.load()
    assert s.profile.name == path.stem.split("__")[1]
    assert wizard._preset_default(s) == EXPECTED_PRESET[s.profile.name]


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_a_non_interactive_dry_run_keeps_the_saved_profile_and_loads_its_team(home, monkeypatch, path, capsys):
    """AC-S4b: the saved state is replayed with --dry-run: the answers and the team are as before, nothing is
    rewritten, and an old name prints its new one once."""
    _seed(path)
    before_text = state.state_path().read_text(encoding="utf-8")
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
    assert wizard.run(args) == 0
    got = state._to_dict(seen["s"])
    # The profile step may restate an old name under its new one in memory; it is the same team, and the file on
    # disk (checked below) is not rewritten by a dry run.
    # ADR-0015: a saved GATEWAY state is migrated to the single-model team, so a gateway-only profile is replaced by the
    # default (and the operator was told); every other state keeps its profile.
    saved = profile_names.canonical(want["profile"]["name"])
    gateway_state = path.stem.split("__")[0] in ("litellm", "openrouter")
    replaced = gateway_state and saved not in profiles.list_profiles(mode="single-model")
    assert profile_names.canonical(got["profile"]["name"]) == (cockpit_auth.TEAM_PROFILE if replaced else saved)
    assert got["profile"]["customized"] is False
    assert state.state_path().read_text(encoding="utf-8") == before_text
    out = capsys.readouterr().out
    old = want["profile"]["name"]
    note = f"Profile {old} is now called {profile_names.canonical(old)}"
    assert (note in out) == (old in profile_names.ALIASES)


def test_a_real_run_saves_the_new_name_for_an_alias_only(home):
    s = state.SetupState()
    s.profile.name = "subscription-economy"
    assert profile_names.canonical(s.profile.name) == "low-cost"
    for kept in ("claude-native", "balanced-claude-55", "openrouter-balanced", "quality-first"):
        assert profile_names.canonical(kept) == kept
    # The save line in wizard._run is `s.profile.name = profile_names.canonical(s.profile.name)`.
    src = pathlib.Path(wizard.__file__).read_text(encoding="utf-8")
    assert "s.profile.name = profile_names.canonical(s.profile.name)" in src


def test_the_flag_path_accepts_an_old_profile_name_through_the_listing():
    assert profile_names.canonical("economy-claude-55") in profiles.list_profiles(mode="multi-model")


def test_preset_files_sit_in_the_canonical_layout_without_a_version_suffix():
    """AC-S4f: profiles/<id>.yaml for the no-gateway rows, profiles/gateway/<id>.yaml for a gateway."""
    root = REPO / "profiles"
    for preset in profile_names.PRESET_IDS:
        assert (root / f"{preset}.yaml").is_file(), preset
    for preset in ("performance", "low-cost"):
        assert (root / "gateway" / f"{preset}.yaml").is_file(), preset
    for path in [root / f"{p}.yaml" for p in profile_names.PRESET_IDS] + sorted((root / "gateway").glob("*.yaml")):
        assert not any(ch.isdigit() for ch in path.stem), path.name
        assert yaml_name(path) in (path.stem, f"gateway/{path.stem}")
    for old in profile_names.ALIASES:
        assert not (root / f"{old}.yaml").exists()


def yaml_name(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8").splitlines()[0].removeprefix("name:").strip()

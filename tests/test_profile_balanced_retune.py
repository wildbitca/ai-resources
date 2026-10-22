"""ADR-0014 D1 (operator decision 2026-10-22): the existing `balanced` single-model profile is retuned; no new profile,
nothing renamed, saved choices kept, the other profiles' teams unchanged."""
from __future__ import annotations

import pathlib
import re
import types

import pytest

from ai_resources.setup import profile_names, profiles, state, ui, wizard

REPO = pathlib.Path(__file__).resolve().parents[1]
HAIKU = ("explore", "tester", "doc-writer", "package-upgrade")
SONNET = ("planner", "software-architect", "implementer", "code-reviewer", "generalPurpose", "crashlytics-fixer",
          "sentry-fixer", "terraform-maintainer", "crossplane-upjet-maintainer")
OPUS = ("verifier", "security-auditor")
CHECKERS = OPUS
WRITERS = ("implementer", "tester", "doc-writer", "package-upgrade", "generalPurpose", "crashlytics-fixer",
           "sentry-fixer", "terraform-maintainer", "crossplane-upjet-maintainer")


def _balanced() -> dict:
    return profiles.load_profile("balanced", "single-model")


def test_the_role_table_is_the_approved_mix():
    by_role = _balanced()["by_role"]
    assert {r for r, c in by_role.items() if c["model"] == "haiku"} == set(HAIKU)
    assert {r for r, c in by_role.items() if c["model"] == "sonnet"} == set(SONNET)
    assert {r for r, c in by_role.items() if c["model"] == "opus"} == set(OPUS)
    assert set(by_role) == set(profiles.KNOWN_ROLES)


def test_independence_no_writer_on_opus_and_checkers_have_no_fallback():
    by_role = _balanced()["by_role"]
    for role in WRITERS:
        assert by_role[role]["model"] != "opus", role
    for role in CHECKERS:
        assert not by_role[role].get("fallbacks"), role


def test_no_role_falls_back_to_opus_and_haiku_roles_fall_back_only_to_sonnet():
    by_role = _balanced()["by_role"]
    for role, cfg in by_role.items():
        assert "opus" not in (cfg.get("fallbacks") or []), role
    for role in HAIKU:
        assert set(by_role[role].get("fallbacks") or []) <= {"sonnet"}, role


def test_lane_candidates_follow_the_new_table():
    lanes = _balanced()["lanes"]
    assert "claude-cli:haiku" in lanes["roles"]["tester"] and "claude-cli:haiku" in lanes["roles"]["doc-writer"]
    assert "claude-cli:sonnet" in lanes["roles"]["software-architect"]
    assert "claude-cli:opus" not in lanes["roles"]["software-architect"]
    assert "claude-cli:opus" not in lanes["second"]["software-architect"]
    assert "claude-cli:opus" in lanes["roles"]["verifier"] and "claude-cli:opus" in lanes["roles"]["security-auditor"]


def test_no_new_profile_nothing_renamed_and_the_recommendation_is_unchanged():
    assert profile_names.PRESET_IDS == ("balanced", "performance", "low-cost")
    assert not (REPO / "profiles" / "pro-lowcost.yaml").exists()
    assert "pro-lowcost" not in profiles.list_profiles()
    assert "balanced" in profiles.list_profiles(mode="single-model")
    assert _balanced()["name"] == "balanced"
    assert profiles.RECOMMENDED_PROFILE == "balanced-claude-55"
    assert wizard.PRESET_PROFILES["single"]["balanced"] == "balanced"
    assert wizard.PRESET_PROFILES["cli"]["balanced"] == "balanced"


def test_low_cost_and_the_other_profiles_keep_their_teams():
    low = profiles.load_profile("low-cost", "single-model")["by_role"]
    assert low["tester"] == {"model": "sonnet", "fallbacks": ["haiku"]}
    assert low["software-architect"] == {"model": "sonnet"}
    assert low["doc-writer"] == {"model": "haiku"} and low["package-upgrade"] == {"model": "haiku"}
    assert low["explore"] == {"model": "haiku"} and low["planner"] == {"model": "sonnet"}
    assert low["verifier"] == {"model": "opus"} and low["security-auditor"] == {"model": "opus"}
    gateway = profiles.load_profile("balanced-claude-55")["by_role"]
    assert gateway["planner"]["model"] == "anthropic/claude-opus-5.5"
    assert gateway["tester"]["model"] == "anthropic/claude-haiku-5.5"


def test_the_description_keeps_the_numbers_the_other_tests_pin():
    text = _balanced()["description"]
    for needle in ("74.6", "55.4", "59.6", "CVP", "100k", "ADR-0014"):
        assert needle in text, needle


# --- the old -> new diff for a host that saved `balanced` (ADR-0001) ------------------------------------------------

def _state(name="balanced", seen=None):
    s = state.SetupState()
    s.profile.name = name
    s.profile.roles_seen = dict(seen or {})
    return s


@pytest.fixture
def captured(monkeypatch):
    out = {"info": [], "diff": []}
    monkeypatch.setattr(ui, "info", out["info"].append)
    monkeypatch.setattr(profiles, "print_role_diff",
                        lambda before, after, title="": out["diff"].append((before, after, title)) or True)
    return out


def test_a_saved_balanced_with_no_record_shows_the_pre_retune_table_once(monkeypatch, captured):
    monkeypatch.setattr(wizard, "_PREV_STATE", _state())
    s = _state()
    wizard._note_profile_table_change(s, dry_run=False)

    assert len(captured["diff"]) == 1
    before, after, title = captured["diff"][0]
    assert before["tester"]["model"] == "sonnet" and after["tester"]["model"] == "haiku"
    assert before["software-architect"]["model"] == "opus" and after["software-architect"]["model"] == "sonnet"
    assert "balanced" in title
    assert s.profile.name == "balanced"                              # nothing renamed or replaced
    assert s.profile.roles_seen == after                             # recorded for the next run

    # the next run: the record is the new table, so nothing is shown
    captured["diff"].clear()
    monkeypatch.setattr(wizard, "_PREV_STATE", _state(seen=s.profile.roles_seen))
    wizard._note_profile_table_change(_state(seen=s.profile.roles_seen), dry_run=False)
    assert captured["diff"] == []


def test_a_dry_run_shows_the_diff_and_records_nothing(monkeypatch, captured):
    monkeypatch.setattr(wizard, "_PREV_STATE", _state())
    s = _state()
    wizard._note_profile_table_change(s, dry_run=True)
    assert len(captured["diff"]) == 1
    assert s.profile.roles_seen == {}


def test_a_first_run_shows_no_diff_but_records_the_table(monkeypatch, captured):
    monkeypatch.setattr(wizard, "_PREV_STATE", state.SetupState())
    s = _state()
    wizard._note_profile_table_change(s, dry_run=False)
    assert captured["diff"] == [] and s.profile.roles_seen["tester"]["model"] == "haiku"


def test_a_profile_without_a_pre_retune_table_is_recorded_silently(monkeypatch, captured):
    monkeypatch.setattr(wizard, "_PREV_STATE", _state("low-cost"))
    s = _state("low-cost")
    wizard._note_profile_table_change(s, dry_run=False)
    assert captured["diff"] == [] and s.profile.roles_seen["tester"]["model"] == "sonnet"


def test_no_profile_chosen_does_nothing(monkeypatch, captured):
    monkeypatch.setattr(wizard, "_PREV_STATE", state.SetupState())
    s = state.SetupState()
    wizard._note_profile_table_change(s, dry_run=False)
    assert captured["diff"] == [] and s.profile.roles_seen == {}


def test_roles_seen_is_omitted_from_the_file_while_empty_and_round_trips():
    empty = state._to_dict(state.SetupState())
    assert "roles_seen" not in empty["profile"]
    s = state.SetupState()
    s.profile.roles_seen = {"tester": {"model": "haiku"}}
    again = state._from_dict(state.SetupState, state._to_dict(s))
    assert again.profile.roles_seen == {"tester": {"model": "haiku"}}


def test_the_legacy_table_matches_the_profile_file_before_the_retune():
    legacy = profiles.LEGACY_PROFILE_TABLES["balanced"]
    assert set(legacy) == set(profiles.KNOWN_ROLES)
    assert legacy["verifier"] == {"model": "opus"} and legacy["planner"]["fallbacks"] == ["opus"]

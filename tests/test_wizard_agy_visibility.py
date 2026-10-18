"""Where Gemini shows up in setup: the Custom model list and the preset role tables (ADR-0007 section 11).

Antigravity (agy) is a subscription CLI, not an API account. Its models are shown as a lane group that is
informational in the Custom list and as extra rows in the CLI-mode preset tables; none of it is a Candidate, a
Selection slot or a credential prompt, the lane stays off by default and agy never writes. Everything is offline:
agy detection is faked and agy is never run."""
from __future__ import annotations

import argparse

import pytest

from ai_resources import roles_policy, model_pins, selection as selection_mod
from ai_resources import agy_lane_gates
from ai_resources.setup import detection, lanes_render, model_selection as ms, profiles, state, ui, wizard
from test_wizard_presets import ASK, CONFIRM, KEYS, args as preset_args, make_state
from test_wizard_model_selection import args as sel_args, host, _state  # noqa: F401 - host is a fixture
from wizard_fakes import Script, one

GROUP = "Antigravity (agy, subscription)"
OFF = "Antigravity lane - off until you enable it in step 6c"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(wizard, "_REQUESTED_PROFILE", "")
    monkeypatch.setattr(wizard, "_COMPOSE", {})
    monkeypatch.setattr(wizard, "_PRESET", "")
    monkeypatch.setattr(ui, "section", lambda *a, **k: None)


def agy(monkeypatch, installed):
    monkeypatch.setattr(detection, "agy_installed", lambda: installed)


# --- detection never runs agy ---------------------------------------------------------------------------

def test_agy_installed_looks_the_binary_up_and_never_runs_it(monkeypatch):
    monkeypatch.undo()
    monkeypatch.setattr(detection, "_which_extra", lambda b: "/x/agy" if b == "agy" else "")
    monkeypatch.setattr(detection, "_run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran a binary")))
    assert detection.agy_installed() is True
    monkeypatch.setattr(detection, "_which_extra", lambda b: "")
    assert detection.agy_installed() is False


# --- the pure group ---------------------------------------------------------------------------------------

def test_the_group_lists_every_lane_slug_labelled_unconfirmed_lane_opt_in():
    rows = ms.agy_group_rows()
    assert [r.slug for r in rows] == list(model_pins.AGY_LANE_MODELS)
    assert any(r.slug.startswith("gemini-") for r in rows)
    for r in rows:
        assert "lane, opt-in" in r.label
        assert ("unconfirmed slug" in r.label) is (not model_pins.AGY_LANE_MODELS_CONFIRMED)


def test_the_unconfirmed_label_follows_the_flag(monkeypatch):
    monkeypatch.setattr(model_pins, "AGY_LANE_MODELS_CONFIRMED", True)
    assert all("unconfirmed" not in r.label for r in ms.agy_group_rows())


def test_the_group_is_never_a_candidate_nor_a_provider():
    from ai_resources import model_providers
    cands = ms.candidates(wizard_accounts(), None)
    assert "agy" not in cands and "antigravity" not in cands
    assert not any("agy" in p or "antigravity" in p for p in model_providers.REGISTRY)
    with pytest.raises(ms.SelectionError):
        ms.build_selection("single", [ms.Candidate("agy", "gemini-flash", "gemini-3.8-flash-low")], None)


def wizard_accounts():
    from wizard_fakes import accounts_for
    return accounts_for(anthropic=True, google=True)


def test_why_lines_name_the_absence_and_the_off_lane():
    absent = ms.agy_why_lines(installed=False, lanes_on=False)
    assert len(absent) == 1 and "Antigravity (agy) not installed" in absent[0] and "step 1" in absent[0]
    off = ms.agy_why_lines(installed=True, lanes_on=False)
    assert len(off) == 1 and "Antigravity lanes are off" in off[0] and "step 6c" in off[0]
    assert ms.agy_why_lines(installed=True, lanes_on=True) == []


# --- the Custom list --------------------------------------------------------------------------------------

def custom_run(monkeypatch, host, installed):
    agy(monkeypatch, installed)
    script = Script(**{"How do you want": "pick", "Add a key": [], "What do you want": "single",
                       "Which model": one("gemini-3.8-flash"), "Apply": "Apply"})
    script.install(monkeypatch)
    s = _state("claude")
    rc = wizard._step_models(s, sel_args())
    return s, rc, script


def test_custom_shows_the_agy_group_only_when_installed(host, monkeypatch):
    s, rc, script = custom_run(monkeypatch, host, True)
    text = "\n".join(script.shown)
    assert rc == 0 and GROUP in text and "gemini-3.8-flash-high" in text
    assert "unconfirmed slug" in text and "lane, opt-in" in text and "step 6c" in text
    s, rc, script = custom_run(monkeypatch, host, False)
    text = "\n".join(script.shown)
    assert GROUP not in text and "gemini-3.8-flash-high" not in text
    assert "Antigravity (agy) not installed" in text


def test_custom_asks_no_credential_for_agy_and_treats_it_as_no_api_provider(host, monkeypatch):
    s, rc, script = custom_run(monkeypatch, host, True)
    assert not [a for a in script.asked if a.startswith("password") or "agy" in a.lower()
                or "antigravity" in a.lower()]
    sel = s.get_selection()
    assert sorted(sel.slots) == ["google:gemini-flash"] and set(sel.providers) == {"google"}
    assert not any("agy" in k or "antigravity" in k for k in list(sel.slots) + list(sel.providers))
    assert not any(isinstance(c, dict) and "agy" in str(c) for c in [sel.to_dict()["slots"]])


def test_an_installed_agy_changes_nothing_in_the_selection_or_the_plan_table(host, monkeypatch):
    s1, _, _ = custom_run(monkeypatch, host, True)
    s2, _, _ = custom_run(monkeypatch, host, False)
    assert s1.get_selection().to_dict() == s2.get_selection().to_dict()
    sel = s1.get_selection()
    agy(monkeypatch, True)
    t1 = ms.plan_table(sel, "multi-model", "litellm", ["claude"], allow_unverified=True)
    agy(monkeypatch, False)
    t2 = ms.plan_table(sel, "multi-model", "litellm", ["claude"], allow_unverified=True)
    assert t1 == t2
    assert "agy" not in str(t1).lower()


# --- the presets ------------------------------------------------------------------------------------------

def run_preset(monkeypatch, key, preset, installed, lanes=None):
    agy(monkeypatch, installed)
    shown = []
    monkeypatch.setattr(ui, "role_table", lambda rows, title="": shown.append((title, rows)))
    s = make_state(key)
    if lanes is not None:
        s.profile.lanes = lanes
    script = Script(**{ASK: preset, CONFIRM: True})
    script.install(monkeypatch)
    assert wizard._step_preset(s, preset_args()) == 0
    return s, shown, script


def agy_tables(shown):
    return [(t, rows) for t, rows in shown if OFF in t]


@pytest.mark.parametrize("preset", KEYS)
def test_cli_mode_with_agy_adds_a_marked_agy_table_after_the_role_table(monkeypatch, preset):
    s, shown, script = run_preset(monkeypatch, "cli", preset, True)
    assert len(shown) == 2 and OFF not in shown[0][0]
    title, rows = shown[1]
    assert OFF in title and rows
    flat = " ".join(" ".join(r) for r in rows)
    assert "unconfirmed slug" in flat and "antigravity" in flat.lower()
    assert script.asked == [f"select: {ASK}"]
    writer = [r for r in rows if r[0].startswith(roles_policy.DEFAULT_INTEGRATING_WRITER)]
    assert writer and all("writer stays on Claude Code" in r[2] for r in writer)
    # the rows are exactly the agy candidates of the profile's lanes block
    doc = profiles.load_profile(wizard.preset_profile(s, preset))
    want = {c.split(":", 1)[1] for key in ("roles", "second") for cs in doc["lanes"][key].values()
            for c in cs if c.startswith("agy:")}
    assert {r[2].split(" ")[0] for r in rows} == want


@pytest.mark.parametrize("preset", ("performance", "low-pricing"))
def test_performance_and_low_pricing_show_gemini_as_the_code_review_second_opinion(monkeypatch, preset):
    _, shown, _ = run_preset(monkeypatch, "cli", preset, True)
    rows = agy_tables(shown)[0][1]
    gem = [r for r in rows if "gemini" in r[2]]
    assert gem and all(r[0].startswith("code-reviewer") and "second" in r[0] for r in gem)


@pytest.mark.parametrize("key", ["single", "litellm", "openrouter"])
@pytest.mark.parametrize("preset", KEYS)
def test_other_modes_never_show_agy_rows(monkeypatch, key, preset):
    _, shown, script = run_preset(monkeypatch, key, preset, True)
    assert len(shown) == 1 and not agy_tables(shown)
    assert "Antigravity" not in "\n".join(script.shown)


@pytest.mark.parametrize("preset", KEYS)
def test_cli_mode_without_agy_shows_no_rows_and_one_line_saying_why(monkeypatch, preset):
    _, shown, script = run_preset(monkeypatch, "cli", preset, False)
    assert len(shown) == 1
    lines = [m for m in script.shown if "Antigravity (agy) not installed" in m]
    assert len(lines) == 1 and "Gemini" in lines[0] and "step 1" in lines[0]


def test_cli_mode_with_agy_and_lanes_off_says_so_once(monkeypatch):
    _, shown, script = run_preset(monkeypatch, "cli", "performance", True)
    assert len([m for m in script.shown if "Antigravity lanes are off" in m]) == 1


def test_the_question_stays_four_choices_with_agy_installed(monkeypatch):
    agy(monkeypatch, True)
    seen = {}
    monkeypatch.setattr(ui, "select", lambda m, choices, default=None, **k: (
        seen.update(values=[c.value for c in choices]) or "custom"))
    wizard._step_preset(make_state("cli"), preset_args())
    assert seen["values"] == ["balanced", "performance", "low-pricing", "custom"]


# --- profiles and the gates -------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["subscription-performance", "subscription-economy"])
def test_the_gemini_second_opinion_is_declared_valid_pinned_and_held_while_the_gates_are_open(name):
    doc = profiles.load_profile(name)
    block = doc["lanes"]
    assert lanes_render.validate_block(block) == []
    gem = [c for c in block["second"]["code-reviewer"] if c.startswith("agy:gemini-")]
    assert gem and gem[0][4:] in model_pins.AGY_LANE_MODELS
    assert set(block["pins"]) == {"code-reviewer"} and gem[0] in block["pins"]["code-reviewer"]
    # no Gemini on a role that writes, a checker or a role without Google egress acceptance
    for key in ("roles", "second"):
        for role, cs in block[key].items():
            if any("gemini" in c for c in cs):
                assert key == "second" and role == "code-reviewer"
    frag = lanes_render.state_from_block(block, claude_ids={"haiku": "claude-haiku-5-5",
                                         "sonnet": "claude-sonnet-5-5", "opus": "claude-opus-5-5"})
    assert not [a for a in frag["agents"].values() if a["backend"] != "claude-cli"]
    assert any(h["candidate"] == gem[0] and h["reason"].startswith("held:") for h in frag["held"])


def test_a_fresh_install_keeps_lanes_off_and_agy_not_writable(monkeypatch):
    s, shown, _ = run_preset(monkeypatch, "cli", "performance", True)
    assert not state.lanes_enabled(s)
    from ai_resources.setup.cockpits import openclaw_lanes
    assert agy_lane_gates.AGY_LANE_BACKEND not in openclaw_lanes.writable_backends()
    assert not agy_lane_gates.all_proven()

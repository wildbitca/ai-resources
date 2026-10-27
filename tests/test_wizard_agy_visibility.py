"""Where Gemini shows up in setup: the Custom model list and the review table (ADR-0007 section 11, ADR-0009).

Antigravity (agy) is a subscription CLI, not an API account. Its models are shown as a group that is informational in
the Custom list and as the matrix rows of the review table; none of it is a Candidate, a Selection
slot or a credential prompt, it is off until ticked on "Your tools". Everything is offline: agy
detection is faked and agy is never run."""
from __future__ import annotations

import argparse

import pytest

from ai_resources import model_pins, selection as selection_mod
from ai_resources.setup import detection, model_selection as ms, profiles, state, ui, wizard
from test_wizard_presets import ASK, KEYS, args as preset_args, make_state
from test_wizard_model_selection import args as sel_args, host, _state  # noqa: F401 - host is a fixture
from wizard_fakes import Script, one

GROUP = "Antigravity (agy, subscription)"
OFF = "Antigravity - off until you tick it on Your tools"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(wizard, "_REQUESTED_PROFILE", "")
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

def test_the_group_lists_every_lane_slug_labelled_lane_opt_in():
    rows = ms.agy_group_rows()
    assert [r.slug for r in rows] == list(model_pins.AGY_LANE_MODELS)
    assert any(r.slug.startswith("gemini-") for r in rows)
    for r in rows:
        assert "lane, opt-in" in r.label


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
    assert len(absent) == 1 and "Antigravity (agy) not installed" in absent[0] and "Your tools" in absent[0]
    off = ms.agy_why_lines(installed=True, lanes_on=False)
    assert len(off) == 1 and "Antigravity is not used" in off[0] and "tick it on Your tools" in off[0]
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
    assert "lane, opt-in" in text and "Your tools" in text
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

def run_preset(monkeypatch, key, preset, installed, ticked=False, lanes=None):
    agy(monkeypatch, installed)
    shown = []
    monkeypatch.setattr(ui, "runs_table", lambda rows, title="": shown.append((title, rows)))
    s = make_state(key)
    if ticked:
        s.profile.customizations = {"__targets__": {"selected": ["claude", "agy"]}}
    if lanes is not None:
        s.profile.lanes = lanes
    script = Script(**{ASK: preset})
    script.install(monkeypatch)
    assert wizard._step_preset(s, preset_args()) == 0
    return s, shown, [], script


@pytest.mark.parametrize("key", ["cli", "single", "litellm", "openrouter"])
@pytest.mark.parametrize("preset", KEYS)
def test_every_row_shows_one_role_table_and_no_antigravity_team_table(monkeypatch, key, preset):
    _, shown, _, script = run_preset(monkeypatch, key, preset, True, ticked=True)
    assert len(shown) == 1 and shown[0][1]
    assert not any("Antigravity" in m for m in script.shown)


def test_the_question_stays_four_choices_with_agy_installed(monkeypatch):
    agy(monkeypatch, True)
    seen = {}
    monkeypatch.setattr(ui, "select", lambda m, choices, default=None, **k: (
        seen.update(values=[c.value for c in choices]) or "custom"))
    wizard._step_preset(make_state("cli"), preset_args())
    assert seen["values"] == ["balanced", "performance", "low-cost", "custom"]


# --- profiles ---------------------------------------------------------------------------------------------

def test_a_fresh_install_keeps_lanes_off(monkeypatch):
    s, shown, _, _ = run_preset(monkeypatch, "cli", "performance", True)
    assert not state.lanes_enabled(s)

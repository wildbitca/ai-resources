"""The recommended run (2.6.3): one up-front question, then every prompt with an answer takes it and says so."""
from __future__ import annotations

import argparse

import pytest

from ai_resources.setup import ui, wizard


@pytest.fixture
def auto(monkeypatch):
    shown: list[str] = []
    monkeypatch.setattr(ui, "detail", shown.append)
    monkeypatch.setattr(ui, "_ask", lambda q: pytest.fail("a prompt with an answer was asked"))
    ui.set_auto(True)
    yield shown
    ui.set_auto(False)


def test_a_confirm_takes_its_recommended_answer_else_its_default(auto):
    assert ui.confirm("Move the worker?", default=False, recommended=True) is True
    assert ui.confirm("Restore the engine?", default=False) is False
    assert auto == ["Move the worker? -> yes", "Restore the engine? -> no"]


def test_a_select_takes_its_default_and_prints_the_choice_title(auto):
    choices = [ui.Choice("Keep it", value="keep"), ui.Choice("Change it", value="change")]
    assert ui.select("Claude Code sign-in:", choices, default="keep") == "keep"
    assert auto[-1].endswith("-> Keep it")


def test_a_prompt_with_no_answer_is_still_asked(monkeypatch):
    monkeypatch.setattr(ui, "_ask", lambda q: "typed")
    monkeypatch.setattr(ui, "_HAS_QUESTIONARY", True)
    monkeypatch.setattr(ui.questionary, "text", lambda *a, **k: object())
    ui.set_auto(True)
    try:
        assert ui.text("Google account", default="") == "typed"
    finally:
        ui.set_auto(False)


def test_non_interactive_wins_over_auto(monkeypatch):
    monkeypatch.setattr(ui, "detail", lambda m: pytest.fail("non-interactive prints no decision"))
    ui.set_non_interactive(True)
    ui.set_auto(True)
    try:
        assert ui.confirm("x", default=False, recommended=True) is False
    finally:
        ui.set_auto(False)
        ui.set_non_interactive(False)


@pytest.mark.parametrize("flag,expected,auto_on", [(True, wizard.RUN_AUTO, True), (False, "picked", False)])
def test_the_run_mode_is_asked_once_unless_auto_is_passed(monkeypatch, flag, expected, auto_on):
    monkeypatch.setattr(ui, "select", lambda m, c, default=None, **k: "picked")
    monkeypatch.setattr(ui, "detail", lambda m: None)
    try:
        assert wizard._ask_run_mode(argparse.Namespace(auto=flag), is_first=False) == expected
        assert ui.is_auto() is auto_on
    finally:
        ui.set_auto(False)


def test_the_recommended_run_never_signs_the_google_acknowledgement(monkeypatch):
    from ai_resources.setup import state
    monkeypatch.setattr(ui, "info", lambda m: None)
    monkeypatch.setattr(ui, "text", lambda *a, **k: pytest.fail("asked for the Google account"))
    ui.set_auto(True)
    try:
        ok, acks, rc = wizard.agy_terms_check(state.SetupState(), ["reviewer"], interactive=True)
    finally:
        ui.set_auto(False)
    assert (ok, rc) == (False, 0) and "google" not in acks


def test_the_review_names_an_openclaw_key_without_claude_codes_env_var():
    from ai_resources.setup import cockpit_auth, state
    s = state.SetupState()
    s.profile.customizations = {"__targets__": {"selected": ["openclaw"]}}
    cockpit_auth.set_method(s, "openclaw", "cli")
    s.cockpit_auth.setdefault("openclaw", {})["providers"] = {"google": "api_key", "openrouter": "api_key"}
    s.cockpit_auth["openclaw"]["live"] = {"google": "api_key", "openrouter": "api_key"}
    rows = dict(wizard._sign_in_rows(s))
    text = rows["OpenClaw sign-in"]
    assert "google: API key" in text and "ANTHROPIC_API_KEY" not in text

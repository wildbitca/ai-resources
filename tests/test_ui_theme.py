"""NO_COLOR-aware questionary theme (plan S6)."""
from __future__ import annotations

import importlib

import pytest

from ai_resources.setup import ui


def _has_colour(rules):
    return any("fg:" in style or "bg:" in style for _name, style in rules)


def test_rules_without_colour_when_plain():
    rules = ui.qstyle_rules(True)
    assert not _has_colour(rules)
    assert any("bold" in style for _n, style in rules)        # bold stays


def test_rules_with_colour_by_default():
    assert _has_colour(ui.qstyle_rules(False))


def test_no_color_env_is_set_and_non_empty(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert ui.no_color() and not ui.unicode_ok()
    monkeypatch.setenv("NO_COLOR", "")
    assert not ui.no_color()
    monkeypatch.delenv("NO_COLOR")
    assert not ui.no_color()


def test_force_color_does_not_override_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert ui.no_color()


def test_the_loaded_style_has_no_colour_attributes_under_no_color(monkeypatch):
    pytest.importorskip("questionary")
    monkeypatch.setenv("NO_COLOR", "1")
    mod = importlib.reload(ui)
    try:
        assert not _has_colour(mod._QSTYLE.style_rules)
    finally:
        monkeypatch.delenv("NO_COLOR")
        importlib.reload(ui)

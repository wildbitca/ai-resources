"""The step header, progress rail and key hints (plan S6)."""
from __future__ import annotations

import io
import sys

import pytest

from ai_resources.setup import flow, ui


class FakeConsole:
    """Records what section() prints; a width like a terminal's."""

    def __init__(self, width=100):
        self.width = width
        self.out: list[str] = []

    def print(self, *a, **k):
        self.out.append(" ".join(str(x) for x in a))

    def rule(self, title="", **k):
        self.out.append(f"RULE {title} {sorted(k)}")


@pytest.fixture
def term(monkeypatch):
    def make(width=100, *, interactive=True, utf8=True, no_color=False, step=4, back=False):
        con = FakeConsole(width)
        monkeypatch.setattr(ui, "_HAS_RICH", True)
        monkeypatch.setattr(ui, "console", lambda: con)
        monkeypatch.setattr(ui, "_NON_INTERACTIVE", not interactive)
        monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(io.BytesIO(), encoding="utf-8" if utf8 else "ascii"))
        if no_color:
            monkeypatch.setenv("NO_COLOR", "1")
        else:
            monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setattr(ui, "_BACK_ON", back)
        ui.set_progress(step, range(1, step))
        return con
    yield make
    ui.set_progress(0, [])


def test_wide_header_has_the_rail_and_key_hints(term):
    con = term(130)
    ui.section(4, 10, "Provider backends")
    text = "\n".join(con.out)
    assert "[4/10]" in text and "Provider backends" in text
    assert "✔ Tools" in text and "● Providers" in text and "○ Verify" in text
    assert "Enter confirm" in text and "Ctrl-C cancel" in text and "Ctrl-B" not in text


def test_ctrl_b_is_listed_only_while_back_is_enabled(term):
    con = term(130, back=True)
    ui.section(6, 10, "Models and roles - 6a Models")
    assert "Ctrl-B back" in "\n".join(con.out)


def test_the_rail_wraps_at_70_columns(term):
    con = term(70)
    ui.section(4, 10, "Provider backends")
    rail_lines = [l for l in con.out if "Tools" in l and "RULE" not in l][0].split("\n")
    assert len(rail_lines) >= 2 and all(len(l) <= 70 for l in rail_lines)


def test_below_60_columns_one_line_and_no_rail(term):
    con = term(50)
    ui.section(4, 10, "Provider backends")
    assert con.out[1:] == ["Step 4/10  Provider backends"]


def test_ascii_glyphs_without_utf8_or_with_no_color(term):
    for kw in ({"utf8": False}, {"no_color": True}):
        con = term(130, **kw)
        ui.section(4, 10, "Provider backends")
        text = "\n".join(con.out)
        assert "[x] Tools" in text and "[>] Providers" in text and "[ ] Verify" in text
        assert "'characters'" in text           # the rule is drawn with "-"
        assert "✔" not in text and "·" not in text


def test_non_interactive_prints_the_plain_header_only(term, capsys):
    con = term(130, interactive=False)
    ui.section(4, 10, "Provider backends")
    text = "\n".join(con.out)
    assert "[4/10]" in text and "Tools" not in text and "Enter confirm" not in text


def test_no_rail_without_a_wizard_progress(term):
    con = term(130)
    ui.set_progress(0, [])
    ui.section(1, 6, "Configuration state")
    assert "Tools" not in "\n".join(con.out)


def test_step_header_adds_the_sub_step_and_still_goes_through_section(term, monkeypatch):
    seen = []
    monkeypatch.setattr(ui, "section", lambda n, t, title: seen.append((n, t, title)))
    ui.step_header(6, 10, "Models and roles", sub="6c Subscription lanes")
    assert seen == [(6, 10, "Models and roles - 6c Subscription lanes")]


def test_plain_fallback_without_rich(monkeypatch, capsys):
    monkeypatch.setattr(ui, "_HAS_RICH", False)
    ui.section(4, 10, "Provider backends")
    out = capsys.readouterr().out
    assert "[4/10] Provider backends" in out and "Tools" not in out


def test_the_select_hint_follows_the_back_flag(monkeypatch):
    seen = {}

    class Q:
        def unsafe_ask(self):
            return "a"

        def ask(self):
            return "a"

    class FakeQuestionary:
        @staticmethod
        def select(message, **kw):
            seen["instruction"] = kw["instruction"]
            return Q()
    monkeypatch.setattr(ui, "questionary", FakeQuestionary, raising=False)
    monkeypatch.setattr(ui, "_HAS_QUESTIONARY", True)
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", False)
    with ui.prompt_scope(wizard=True, back=True):
        ui.select("m", ["a"])
    assert "Ctrl-B back" in seen["instruction"]
    with ui.prompt_scope(wizard=True, back=False):
        ui.select("m", ["a"])
    assert "Ctrl-B" not in seen["instruction"]
    ui.select("m", ["a"])
    assert seen["instruction"] == " (use ↑/↓, Enter)"


def test_real_rich_renders_the_header_without_ansi_under_no_color(monkeypatch):
    rich_console = pytest.importorskip("rich.console")
    buf = io.StringIO()
    con = rich_console.Console(file=buf, width=100, force_terminal=True, no_color=True, legacy_windows=False)
    monkeypatch.setattr(ui, "console", lambda: con)
    monkeypatch.setattr(ui, "_HAS_RICH", True)
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", False)
    monkeypatch.setenv("NO_COLOR", "1")
    ui.set_progress(4, [1, 2, 3])
    try:
        ui.section(4, 10, "Provider backends")
    finally:
        ui.set_progress(0, [])
    out = buf.getvalue()
    assert "[4/10] Provider backends" in out and "[>] Providers" in out
    assert "\x1b[3" not in out and "\x1b[9" not in out      # no foreground colour codes


def test_real_rich_width_70_wraps(monkeypatch):
    rich_console = pytest.importorskip("rich.console")
    buf = io.StringIO()
    con = rich_console.Console(file=buf, width=70, force_terminal=False, legacy_windows=False)
    monkeypatch.setattr(ui, "console", lambda: con)
    monkeypatch.setattr(ui, "_HAS_RICH", True)
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", False)
    monkeypatch.setenv("NO_COLOR", "1")
    ui.set_progress(4, [1, 2, 3])
    try:
        ui.section(4, 10, "Provider backends")
    finally:
        ui.set_progress(0, [])
    assert all(len(l) <= 70 for l in buf.getvalue().splitlines())
    assert "Verify" in buf.getvalue()


def test_the_pure_rail_text_matches_flow(monkeypatch):
    monkeypatch.setattr(ui, "console", lambda: FakeConsole(100))
    monkeypatch.setattr(ui, "_HAS_RICH", True)
    ui.set_progress(3, [1, 2], skipped=[])
    try:
        assert ui.rail(3) == flow.format_rail(flow.rail_model(3, (1, 2), set()), 100, ui.unicode_ok(),
                                              title=ui._LAST_SECTION)
    finally:
        ui.set_progress(0, [])

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
    assert "✔ Tools" in text and "● Audio" in text and "○ More" in text
    assert "Enter confirm" in text and "Ctrl-C cancel" in text and "Ctrl-B" not in text


def test_ctrl_b_is_listed_only_while_back_is_enabled(term):
    con = term(130, back=True)
    ui.section(6, 10, "Models and roles - 6a Models")
    assert "Ctrl-B back" in "\n".join(con.out)


def test_the_rail_wraps_at_70_columns(term):
    con = term(70)
    ui.section(4, 10, "Provider backends")
    rail_lines = [l for l in con.out if "Tools" in l and "RULE" not in l][0].split("\n")
    assert rail_lines and all(len(l) <= 70 for l in rail_lines)         # five short labels fit; the limit still holds


def test_below_60_columns_one_line_and_no_rail(term):
    con = term(50)
    ui.section(4, 10, "Provider backends")
    assert con.out[1:] == ["Step 4/10  Provider backends"]


def test_ascii_glyphs_without_utf8_or_with_no_color(term):
    for kw in ({"utf8": False}, {"no_color": True}):
        con = term(130, **kw)
        ui.section(4, 10, "Provider backends")
        text = "\n".join(con.out)
        assert "[x] Tools" in text and "[>] Audio" in text and "[ ] More" in text
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
    assert "[4/10] Provider backends" in out and "[>] Audio" in out
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
    assert "More" in buf.getvalue()


def test_the_pure_rail_text_matches_flow(monkeypatch):
    monkeypatch.setattr(ui, "console", lambda: FakeConsole(100))
    monkeypatch.setattr(ui, "_HAS_RICH", True)
    ui.set_progress(3, [1, 2], skipped=[])
    try:
        assert ui.rail(3) == flow.format_rail(flow.rail_model(3, (1, 2), set()), 100, ui.unicode_ok(),
                                              title=ui._LAST_SECTION)
    finally:
        ui.set_progress(0, [])


@pytest.mark.parametrize("width", [59, 60, 79, 80])
@pytest.mark.parametrize("ascii_only", [False, True])
def test_real_rich_header_fits_at_the_boundary_widths(monkeypatch, width, ascii_only):
    rich_console = pytest.importorskip("rich.console")
    buf = io.StringIO()
    con = rich_console.Console(file=buf, width=width, force_terminal=False, legacy_windows=False)
    monkeypatch.setattr(ui, "console", lambda: con)
    monkeypatch.setattr(ui, "_HAS_RICH", True)
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", False)
    monkeypatch.setattr(ui, "unicode_ok", lambda: not ascii_only)
    monkeypatch.setattr(ui, "terminal_width", lambda: width)
    monkeypatch.setenv("NO_COLOR", "1")
    ui.set_progress(4, [1, 2, 3])
    try:
        ui.section(4, 10, "Provider backends")
    finally:
        ui.set_progress(0, [])
    out = buf.getvalue()
    assert all(len(l) <= width for l in out.splitlines()), out
    assert "Provider backends" in out
    if width < 60:
        assert "Step 4/10" in out and "Enter confirm" not in out
    else:
        assert "Ctrl-C cancel" in out
    if ascii_only:
        assert out.isascii()


# --- ADR-0016 D7 (AC-16): the rail names only the phases the wizard has ------------------------------------------------------

REMOVED = ("How", "Gateway", "Providers", "API keys", "Start at login")


@pytest.mark.parametrize("phase", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("ascii_only", [False, True])
def test_no_removed_step_label_appears_in_any_rail(term, phase, ascii_only):
    con = term(130, step=phase, utf8=not ascii_only)
    ui.section(phase, 5, "Anything")
    text = "\n".join(con.out)
    for label in REMOVED:
        assert label not in text, label
    assert [m[1] for m in flow.rail_model(phase)] == list(flow.PHASE_LABELS)


def test_the_stale_ten_step_label_table_is_gone():
    assert not hasattr(flow, "STEP_LABELS")


def test_the_wizard_enters_each_step_on_its_phase(monkeypatch):
    from ai_resources.setup import wizard
    seen = []
    monkeypatch.setattr(ui, "set_progress", lambda cur, done=(), skipped=None: seen.append((cur, list(done), set(skipped or ()))))
    steps = {s.id: s for s in wizard._build_steps(False)}
    runner = flow.Wizard(list(steps.values()), snapshot=lambda: None, restore=lambda snap: None)
    for sid in ("tools", "cockpit", "cockpit_auth", "preset", "cockpits", "audio", "more", "hygiene", "operator_rules",
                "claude_md", "agy_mcp", "review"):
        wizard._enter_step(steps[sid], runner)
    assert [c for c, _d, _s in seen] == [1, 2, 2, 3, 3, 4, 5, 5, 5, 5, 5, 5]
    assert seen[3][1] == [1, 2]

"""Subprocess text echoed by the wizard: no terminal escapes, no rich markup, secrets masked (security audit)."""
from __future__ import annotations

import io
import subprocess

import pytest

from ai_resources.setup import tools, ui

HOSTILE = "ok\x1b]0;pwned\x07\x1b[2J\x1b[31mred\x1b[0m\rspoof [/] [bold red]x[/bold red]\x00end"


def test_clean_text_strips_escapes_and_controls():
    out = ui.clean_text(HOSTILE)
    assert "\x1b" not in out and "\x07" not in out and "\r" not in out and "\x00" not in out
    assert "]0;" not in out and "[2J" not in out
    assert out.startswith("ok") and out.endswith("end")


def test_clean_text_keeps_tabs_newlines_and_unicode():
    assert ui.clean_text("a\tb\nc ✔ é") == "a\tb\nc ✔ é"


@pytest.mark.parametrize("sink", ["info", "ok", "warn", "error", "detail"])
def test_sinks_never_emit_escapes_or_raise_on_markup(monkeypatch, sink):
    rich_console = pytest.importorskip("rich.console")
    buf = io.StringIO()
    con = rich_console.Console(file=buf, width=100, force_terminal=False, highlight=False, legacy_windows=False)
    monkeypatch.setattr(ui, "console", lambda: con)
    monkeypatch.setattr(ui, "_HAS_RICH", True)
    getattr(ui, sink)(HOSTILE)                         # an unbalanced [/] must not raise MarkupError
    text = buf.getvalue()
    assert "\x1b]" not in text and "\x1b[2J" not in text and "\r" not in text
    assert "[bold red]x[/bold red]" in text


def test_log_tail_masks_and_cleans(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(ui, "detail", lambda m: seen.append(m))
    ui.log_tail(["\x1b[31mAPI_KEY=abcdefghijklmnopq\x1b[0m"])
    assert seen[-1] == "API_KEY=****"


def test_safe_text_masks_then_cleans():
    assert ui.safe_text("\x1b[1mtoken: abcdefgh1234\x1b[0m") == "token: ****"


def test_install_failure_message_is_masked(monkeypatch):
    from ai_resources.setup import state
    errors: list[str] = []
    monkeypatch.setattr(ui, "error", errors.append)
    monkeypatch.setattr(tools, "_run_install", lambda cmd, timeout=300: (False, "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123"))
    assert tools.install_one(next(iter(tools.TOOLS)), state.SetupState()) is False
    assert errors and "ghp_abcdefghij" not in errors[0]

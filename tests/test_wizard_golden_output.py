"""Golden output of a whole non-interactive wizard run (real rich, 240 columns, ASCII glyphs, no colour; skipped without rich).

Regenerate deliberately with:  WIZARD_GOLDEN_UPDATE=1 pytest tests/test_wizard_golden_output.py"""
from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

import pytest

from ai_resources import __version__
from ai_resources import selection as sel
from ai_resources.setup import credentials, state, ui, wizard
from wizard_fakes import host_env

GOLDEN = Path(__file__).parent / "fixtures" / "wizard_golden" / "non_interactive_openrouter.txt"


def _args(**kw):
    ns = dict(dry_run=False, yes=False, non_interactive=True, profile="", lanes=False, no_lanes=False,
              lanes_dry_run=False, lanes_orchestrator=[], allow_unverified=False)
    ns.update(kw)
    return argparse.Namespace(**ns)


def test_non_interactive_run_output_matches_the_golden(monkeypatch, tmp_path, capsys):
    rich_console = pytest.importorskip("rich.console")
    real_banner = ui.banner
    home = host_env(monkeypatch, tmp_path)
    monkeypatch.setattr(ui, "banner", real_banner)
    monkeypatch.setattr(ui, "_console", rich_console.Console(width=240, force_terminal=False, no_color=True,
                                                             legacy_windows=False, highlight=False))
    monkeypatch.setattr(ui, "unicode_ok", lambda: False)
    monkeypatch.setattr(ui, "_NON_INTERACTIVE", False)
    monkeypatch.setattr(credentials, "get_key", lambda name, *a, **k: "stored" if name == "OPENROUTER_API_KEY" else "")
    s = state.SetupState()
    s.mode, s.backend = "multi-model", "openrouter"
    s.last_run = "2026-01-01T00:00:00+00:00"
    s.cockpits["claude"] = state.CockpitState(installed=True)
    s.set_selection(sel.Selection(shape="single", providers={"anthropic": sel.ProviderSel()},
                                  slots={"anthropic:sonnet": sel.SlotSel("anthropic/claude-sonnet-5-5")},
                                  primary="anthropic:sonnet", smoke_path="direct"))
    state.config_root().mkdir(parents=True, exist_ok=True)
    state.save(s)
    rc = wizard.run(_args())
    cap = capsys.readouterr()
    text = (cap.out + cap.err).replace(str(home), "<HOME>").replace(__version__, "<VERSION>")
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = re.sub(r"Last run: .*", "Last run: <TS>", text)
    text = re.sub(r" {2,}", " ", text)           # box padding depends on the length of the version
    text = f"rc={rc}\n{text}"
    if os.environ.get("WIZARD_GOLDEN_UPDATE") == "1":
        GOLDEN.write_text(text, encoding="utf-8")
    assert GOLDEN.is_file(), "run once with WIZARD_GOLDEN_UPDATE=1"
    assert text == GOLDEN.read_text(encoding="utf-8")
    assert "stored" not in text
    assert text.isascii(), "NO_COLOR/ASCII mode must not emit non-ASCII glyphs"

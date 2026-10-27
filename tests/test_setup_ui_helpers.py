"""The setup UI helpers: agy is ticked like any installed tool, and table cells are shown as text."""
from __future__ import annotations

import types

from ai_resources.setup import state, ui, wizard


# --- agy is ticked like any installed tool -----------------------------------------------------------------------------

def detected(*ids):
    return {i: types.SimpleNamespace(installed=True) for i in ids}


def test_agy_is_ticked_by_default_when_installed_and_a_saved_tick_is_restored():
    s = state.SetupState()
    assert "agy" in wizard._default_members(s, detected("claude", "agy"))
    s.profile.customizations = {"__targets__": {"selected": ["claude", "agy"]}}
    assert wizard._default_members(s, detected("claude", "agy")) == ["claude", "agy"]


# --- AC-S10g: markup in model text is shown as text ------------------------------------------------------------------------

def test_markup_in_a_role_or_cell_is_escaped(capsys):
    ui.runs_table([("[bold red]role[/]", "[link=http://x]m[/link]", "[red]fb[/red]")], title="[b]t[/b]")
    out = capsys.readouterr().out
    assert "[bold red]role[/]" in out and "[red]fb[/red]" in out and "[link=http://x]m[/link]" in out

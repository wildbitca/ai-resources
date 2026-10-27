"""The Review table `Role | Primary (backend:model, pool) | Fallback`: one table from `routing.table_rows`."""
from __future__ import annotations

import re

import pytest

from ai_resources.scoring import routing
from ai_resources.setup import detection, state, ui, wizard

BANNED = ("(lane)", "(second)", "display-only", "candidates", "second opinion")
MISSING = {"agy": "agy-missing", "codex": "codex-missing"}


def _capture(monkeypatch, width=80):
    rich = pytest.importorskip("rich.console")
    con = rich.Console(width=width, force_terminal=False, color_system=None, highlight=False, legacy_windows=False)
    monkeypatch.setattr(ui, "_console", con)
    return con


def _state(*members):
    s = state.SetupState()
    s.mode = "single-model"
    s.profile.customizations = {"__targets__": {"selected": list(members)}}
    return s


def test_one_row_per_team_role_and_the_chat_plane_only_with_openclaw():
    team = routing.table_rows(chat=False)
    assert [r for r, _p, _f in team] == [n for n, r in sorted(routing.ROLES.items(), key=lambda kv: (kv[1].kind, kv[0]))
                                         if r.kind == "team"]
    full = {r for r, _p, _f in routing.table_rows()}
    assert full - {r for r, _p, _f in team} == {"main", "channel-agents", "utility", "summarization", "explore-bulk",
                                                "heartbeat"}


def test_each_cell_names_its_backend_model_and_pool():
    rows = {r: (p, f) for r, p, f in routing.table_rows()}
    assert rows["implementer"][0] == "claude-code:sonnet [claude-subscription]"
    assert rows["explore"][0] == "agy:gemini-3.8-flash-high [agy-gemini]"
    assert rows["main"][0] == "anthropic-api:claude-sonnet-5-5 [anthropic-api]"
    assert rows["utility"][0] == "gemini-api:gemini-3.8-flash [gemini-api]"
    assert rows["implementer"][1] == "agy:gemini-4-argon-high [agy-gemini]"


def test_heartbeat_shows_its_gate_and_the_other_chat_rows_do_not():
    rows = {r: f for r, _p, f in routing.table_rows()}
    assert "gated" in rows["heartbeat"] and "unproven" in rows["heartbeat"]
    assert all("gated" not in f for r, f in rows.items() if r != "heartbeat")


def test_a_skipped_tool_shows_the_fallback_that_runs_with_its_cause():
    rows = {r: (p, f) for r, p, f in routing.table_rows(unavailable=MISSING)}
    primary, fallback = rows["explore"]
    assert primary == "claude-code:haiku [claude-subscription]"
    assert fallback.startswith("agy-missing: agy:gemini-3.8-flash-high skipped")
    assert rows["implementer"][0] == "claude-code:sonnet [claude-subscription]"      # Claude cells are untouched
    assert "agy-missing" not in rows["implementer"][1]
    assert routing.table_rows(unavailable={}) == routing.table_rows()


def test_codex_missing_shows_its_cause_on_a_codex_override():
    doc = {"routing": {"tester": {"primary": "codex:gpt-5", "fallback": ["claude-code:sonnet"]}}}
    rows = {r: (p, f) for r, p, f in routing.table_rows(doc, unavailable=MISSING)}
    assert rows["tester"][0] == "claude-code:sonnet [claude-subscription]"
    assert rows["tester"][1].startswith("codex-missing: codex:gpt-5 skipped")


def test_the_review_builds_the_chat_plane_once_and_only_when_openclaw_is_selected(monkeypatch):
    monkeypatch.setattr(detection, "inactive_backends", lambda: {})
    without = [r for r, _p, _f in wizard._review_table_rows(_state("claude", "agy"))]
    with_oc = [r for r, _p, _f in wizard._review_table_rows(_state("claude", "openclaw"))]
    assert "main" not in without and with_oc.count("main") == 1
    assert len(with_oc) == len(set(with_oc)), "no role appears twice"
    assert set(with_oc) == set(routing.ROLES)


@pytest.mark.parametrize("openclaw", [False, True])
def test_the_table_prints_once_with_three_columns_inside_80_columns_without_ansi(monkeypatch, capsys, openclaw):
    _capture(monkeypatch)
    rows = routing.table_rows(chat=openclaw, unavailable=MISSING)
    ui.runs_table(rows, title="Who runs what")
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert max(len(line) for line in out.splitlines()) <= 80
    assert out.count("Who runs what") == 1
    header = next(line for line in out.splitlines() if "Role" in line)
    assert "Primary" in out and "pool)" in out and header.index("Role") < header.index("Fallback")      # wraps at 80
    flat = re.sub(r"[|\s]+", "", out)
    for role, _p, _f in rows:
        assert role.replace(" ", "") in flat
    assert ("heartbeat" in out) is openclaw
    low = out.lower()
    assert not [w for w in BANNED if w in low]


def test_the_plain_fallback_without_rich_wraps_at_80_columns(monkeypatch, capsys):
    monkeypatch.setattr(ui, "_HAS_RICH", False)
    ui.runs_table(routing.table_rows(unavailable=MISSING), title="Who runs what")
    out = capsys.readouterr().out
    assert "\x1b" not in out and max(len(line) for line in out.splitlines()) <= 80
    assert "agy-missing: agy:gemini-3.8-flash-high skipped" in out.replace("\n", " ").replace("        ", " ")
    assert not [w for w in BANNED if w in out.lower()]


def test_a_cell_that_looks_like_markup_is_shown_as_text(monkeypatch, capsys):
    _capture(monkeypatch)
    ui.runs_table([("[red]r[/red]", "[b]p[/b]", "f")], title="t")
    assert "[red]r[/red]" in capsys.readouterr().out


def test_the_wizard_source_carries_no_removed_wording():
    import pathlib
    root = pathlib.Path(wizard.__file__).parent
    for name in ("wizard.py", "ui.py", "team_view.py"):
        text = (root / name).read_text(encoding="utf-8").lower()
        for word in ("(lane)", "(second)", "display-only", "role_lane_table", "team_table"):
            assert word not in text, (name, word)

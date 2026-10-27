"""ADR-0010 S10: the team as the operator sees it: real providers, lanes, Gemini held / on / off, one set of lines."""
from __future__ import annotations

import types

import pytest

from ai_resources.setup import detection, profiles, state, team_view, ui, wizard

# Every role whose PRIMARY is a Gemini cell in the routing matrix (ADR-0019).
_GEMINI = [(role, cell.split(":", 1)[1]) for role, cell in team_view.agy_candidates({}) if cell.startswith("agy:gemini-")]
BALANCED_ROLES = sorted({role for role, _slug in _GEMINI})
GEMINI_CELLS = {f"{role} (lane)": f"google {slug} via Antigravity" for role, slug in _GEMINI}
NEXT = team_view.NEXT_AGY


def set_agy(monkeypatch, active=True):
    monkeypatch.setattr(detection, "agy_active", lambda: active)


def balanced():
    return profiles.load_profile("balanced", "single-model")


def base_rows(doc, backend=None):
    return profiles.role_table(profiles.to_executors(doc))


def cells(rows):
    return {r.role: r.lane for r in rows if r.role in GEMINI_CELLS}


def mix_for(**kw):
    doc = balanced()
    rows = base_rows(doc)
    kw.setdefault("row", "cli")
    row_only = ("claude_billed", "effort")
    return doc, rows, team_view.provider_mix(doc, rows, **{k: v for k, v in kw.items() if k not in row_only}), \
        team_view.role_lane_rows(rows, doc, **{k: v for k, v in kw.items() if k in (
            "row", "ticked", "allow_unverified", "cell_verified", *row_only)})


# --- AC-S10a/b/c: agy installed and signed in is the only condition -----------------------------------------------------

def test_cli_balanced_ticked_with_agy_missing_skips_both_gemini_cells_with_the_recorded_cause(monkeypatch):
    set_agy(monkeypatch, active=False)
    _doc, _rows, mix, lane_rows = mix_for(ticked=True)
    assert cells(lane_rows) == {role: f"{text} (held)" for role, text in GEMINI_CELLS.items()}
    assert mix.lines == (f"Gemini skipped: agy not installed or not signed in ({team_view.CAUSE_MISSING}). Next: {NEXT}",)
    assert mix.next_action == NEXT
    assert {role for role, _why in mix.held} == set(BALANCED_ROLES)
    assert all(why == team_view.CAUSE_MISSING for _role, why in mix.held)
    assert "google" in mix.declared and "google" not in mix.active and "anthropic" in mix.active


def test_with_agy_active_both_cells_are_on_and_agy_writes_where_the_matrix_says(monkeypatch):
    set_agy(monkeypatch)
    _doc, rows, mix, lane_rows = mix_for(ticked=True)
    assert cells(lane_rows) == {role: f"{text} (on)" for role, text in GEMINI_CELLS.items()}
    assert mix.lines == () and mix.held == () and mix.next_action == ""
    assert "google" in mix.active
    # agy may write (ADR-0019 D3): tester and doc-writer have agy primaries, the implementer keeps Claude Code
    by = {r.role.split(" ")[0]: r for r in lane_rows}
    assert by["implementer"].lane == "Claude Code" and by["implementer"].provider != "google"
    assert by["tester"].provider == "google" and by["doc-writer"].provider == "google"


def test_with_agy_not_ticked_the_line_says_so_and_no_cell_reads_on(monkeypatch):
    set_agy(monkeypatch)                                                    # even an active agy is not switched on unticked
    _doc, _rows, mix, lane_rows = mix_for(ticked=False)
    assert mix.lines == ("Gemini not active: Antigravity not selected",)
    assert all(text.endswith("(off)") for text in cells(lane_rows).values()) and len(cells(lane_rows)) == len(GEMINI_CELLS)
    assert "google" not in mix.active


# --- AC-S10d: OpenRouter ---------------------------------------------------------------------------------------------

def or_mix(**kw):
    doc = profiles.load_profile("gateway/balanced", "multi-model")
    rows = base_rows(doc)
    return rows, team_view.provider_mix(doc, rows, row="openrouter", **kw), team_view.role_lane_rows(
        rows, doc, row="openrouter", ticked=False, **kw)


def test_openrouter_balanced_with_the_optin_declined_names_the_roles_that_use_the_session_model():
    rows, mix, lane_rows = or_mix()
    assert mix.lines == ("Gemini held on OpenRouter: Claude Code x Google not verified; "
                         "explore, tester, doc-writer use the session model",)
    assert {role for role, _w in mix.held} == {"explore", "tester", "doc-writer"}
    assert "google" in mix.declared and "google" not in mix.active
    by_role = {r.role: r for r in lane_rows}
    assert by_role["explore"].provider == "google" and by_role["explore"].lane.startswith("OpenRouter (held")
    assert by_role["implementer"].lane == "OpenRouter" and by_role["implementer"].provider == "anthropic"


@pytest.mark.parametrize("kw", [{"allow_unverified": True}, {"cell_verified": True}])
def test_openrouter_balanced_with_the_optin_or_a_verified_cell_has_no_held_line(kw):
    _rows, mix, lane_rows = or_mix(**kw)
    assert mix.lines == () and "google" in mix.active
    assert {r.role: r for r in lane_rows}["explore"].lane == "OpenRouter"


# --- AC-S10f: the rows that cannot run Gemini ------------------------------------------------------------------------

def test_the_single_and_legacy_litellm_rows_print_their_degrade_lines():
    doc = balanced()
    rows = base_rows(doc)
    single = team_view.provider_mix(doc, rows, row="single")
    assert single.lines == ("Gemini not active: 'Each on its own' runs Claude Code only "
                            "(choose 'As a team' with Antigravity, or OpenRouter)",)
    assert "google" not in single.active
    legacy = profiles.load_profile("balanced-claude-55", "multi-model")
    lrows = base_rows(legacy)
    assert team_view.provider_mix(legacy, lrows, row="litellm").lines == (
        "Gemini not active: LiteLLM is legacy; choose OpenRouter for Gemini",)
    assert team_view.provider_mix(legacy, lrows, row="litellm").active == ("anthropic",)


# --- the tools table ------------------------------------------------------------------------------------

def test_role_lane_rows_list_the_agy_lanes_and_the_real_provider():
    doc = balanced()
    lane_rows = team_view.role_lane_rows(base_rows(doc), doc, row="cli", ticked=True)
    names = [r.role for r in lane_rows]
    for lane in ("explore (lane)", "code-reviewer (lane)"):
        assert lane in names
    assert not any("(second)" in n for n in names)
    assert next(r for r in lane_rows if r.role == "implementer").provider == "anthropic"
    assert all(r.provider != "-" for r in lane_rows)


def test_the_wrappers_in_the_wizard_still_answer():
    doc = balanced()
    assert wizard._agy_team_roles(doc) == team_view.agy_team_roles(doc)
    assert "code-reviewer: gemini-4-argon-high" in wizard._agy_team_roles(doc)


# --- agy is ticked like any installed tool -----------------------------------------------------------------------------

def detected(*ids):
    return {i: types.SimpleNamespace(installed=True) for i in ids}


def test_agy_is_ticked_by_default_when_installed_and_a_saved_tick_is_restored():
    s = state.SetupState()
    assert "agy" in wizard._default_members(s, detected("claude", "agy"))
    s.profile.customizations = {"__targets__": {"selected": ["claude", "agy"]}}
    assert wizard._default_members(s, detected("claude", "agy")) == ["claude", "agy"]


# --- AC-S10g: markup in model text is shown as text ------------------------------------------------------------------------

def test_markup_in_a_model_or_lane_is_escaped(capsys):
    ui.role_lane_table([("[bold red]role[/]", "goo[/]gle", "[link=http://x]m[/link]", "[red]lane[/red]")], title="[b]t[/b]")
    out = capsys.readouterr().out
    assert "[bold red]role[/]" in out and "[red]lane[/red]" in out and "[link=http://x]m[/link]" in out











def test_member_state_is_held_with_the_cause_when_agy_is_missing_and_on_when_active(monkeypatch):
    set_agy(monkeypatch, active=False)
    assert team_view.member_state("explore", ticked=True) == (team_view.HELD, team_view.CAUSE_MISSING)
    set_agy(monkeypatch)
    assert team_view.member_state("explore", ticked=True) == (team_view.ON, "")


# --- ADR-0016 D5: separate Runs in and Billed to columns ---------------------------------------------------------------

def test_role_lane_rows_carry_billed_to_as_its_own_field(monkeypatch):
    set_agy(monkeypatch)
    _doc, _rows, _mix, lane_rows = mix_for(ticked=True, claude_billed="subscription",
                                           effort={"planner": "high"})
    claude = [r for r in lane_rows if r.lane == "Claude Code"]
    assert claude and {r.billed_to for r in claude} == {"subscription"}
    assert next(r for r in claude if r.role == "planner").effort == "high"
    agy = [r for r in lane_rows if "via Antigravity" in r.lane]
    assert agy and {r.billed_to for r in agy} == {"Google plan"}
    assert all("subscription" not in r.lane and "Google plan" not in r.lane for r in lane_rows)   # not mixed into the lane text


def test_a_gateway_row_bills_its_gateway_and_an_unset_claude_reads_unknown():
    doc = balanced()
    rows = base_rows(doc)
    via = team_view.role_lane_rows(rows, doc, row="openrouter", ticked=False)
    assert {r.billed_to for r in via} == {"API per token (OpenRouter)"}
    alone = team_view.role_lane_rows(rows, doc, row="single", ticked=False)
    assert {r.billed_to for r in alone} == {"unknown"}


def test_review_role_rows_never_guess(monkeypatch):
    set_agy(monkeypatch, active=False)
    doc = balanced()
    rows = base_rows(doc)[:2]
    out = team_view.review_role_rows(rows, doc, selected=["claude", "openclaw", "agy"], claude_billed="unknown",
                                     openclaw_models={rows[0][0]: "openai/gpt-x"}, openclaw_billed={"anthropic": "subscription"})
    first = [r for r in out if r.role == rows[0][0]]
    assert [r.runs_in for r in first] == ["Claude Code subagent", "OpenClaw kit-role agent"]
    assert first[0].billed_to == "unknown" and first[1].billed_to == "unknown" and first[1].model == "openai/gpt-x"
    second = next(r for r in out if r.role == rows[1][0] and r.runs_in == "OpenClaw kit-role agent")
    assert second.model == "unknown" and second.billed_to == "unknown"          # no model known: said so
    agy = [r for r in out if r.runs_in.startswith("agy")]
    assert agy and all(r.runs_in == "agy (held)" for r in agy)                   # agy missing: held, never "agy"

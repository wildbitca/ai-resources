"""ADR-0010 S10: the team as the operator sees it: real providers, lanes, Gemini held / on / off, one set of lines."""
from __future__ import annotations

import types

import pytest

import agy_evidence_fakes as fk
from ai_resources import agy_gate_evidence as ev
from ai_resources.setup import lanes_render, profiles, state, team_view, ui, wizard

BALANCED_ROLES = ["explore", "code-reviewer", "planner", "software-architect"]
GEMINI_CELLS = {
    "explore (lane)": "google gemini-3.8-flash-high via Antigravity",
    "code-reviewer (second)": "google gemini-3.8-flash-high via Antigravity",
    "planner (second)": "google gemini-3.1-pro-low via Antigravity",
    "software-architect (second)": "google gemini-3.1-pro-low via Antigravity",
}
NEXT = "run the probe window, then ai-resources lanes gates record"


@pytest.fixture(autouse=True)
def _fresh():
    ev.clear_cache()
    yield
    ev.clear_cache()


def balanced():
    return profiles.load_profile("balanced", "single-model")


def base_rows(doc, backend=None):
    return profiles.role_table(profiles.to_executors(doc))


def ack(roles):
    return {"google": lanes_render.make_ack("google", list(roles), "2026-10-19T00:00:00+00:00", account="a@example.com")}


def cells(rows):
    return {r.role: r.lane for r in rows if r.role in GEMINI_CELLS}


def mix_for(**kw):
    doc = balanced()
    rows = base_rows(doc)
    kw.setdefault("row", "cli")
    return doc, rows, team_view.provider_mix(doc, rows, **kw), team_view.role_lane_rows(
        rows, doc, **{k: v for k, v in kw.items() if k in ("row", "ticked", "acks", "allow_unverified", "cell_verified")})


# --- AC-S10a ---------------------------------------------------------------------------------------------------------

def test_cli_balanced_ticked_and_acknowledged_without_a_record_holds_four_gemini_cells_and_says_what_next(monkeypatch,
                                                                                                          tmp_path):
    fk.use(monkeypatch, fk.make_root(tmp_path))
    _doc, _rows, mix, lane_rows = mix_for(ticked=True, acks=ack(BALANCED_ROLES + ["verifier"]))
    assert cells(lane_rows) == {role: f"{text} [unconfirmed slug] (held)" for role, text in GEMINI_CELLS.items()}
    assert mix.lines == ("Gemini held: Antigravity gates 0/8 proven (G6, G7 partial). Next: " + NEXT,)
    assert sum("ai-resources lanes gates record" in line for line in mix.lines) == 1
    assert mix.next_action == NEXT
    assert {role for role, _why in mix.held} == set(BALANCED_ROLES)
    assert "google" in mix.declared and "google" not in mix.active and "anthropic" in mix.active


# --- AC-S10b ---------------------------------------------------------------------------------------------------------

def test_with_a_valid_record_and_the_acknowledgement_the_four_cells_are_on_and_writers_stay_on_claude(monkeypatch,
                                                                                                    tmp_path):
    root = fk.make_root(tmp_path)
    fk.use(monkeypatch, root)
    fk.write(root, fk.valid_doc(root))
    _doc, rows, mix, lane_rows = mix_for(ticked=True, acks=ack(BALANCED_ROLES))
    # ADR-0010 C5: only the code-reviewer second has a dispatching workflow step; the planner and architect seconds
    # are display-only and must never read "on".
    display_only = {"planner (second)", "software-architect (second)"}
    assert cells(lane_rows) == {role: f"{text} ({'display-only' if role in display_only else 'on'})"
                                for role, text in GEMINI_CELLS.items()}
    assert not any(cells(lane_rows)[r].endswith("(on)") for r in display_only)
    assert {r: s for r, s in mix.states.items() if r in display_only} == {r: "display-only" for r in display_only}
    assert "unconfirmed" not in " ".join(cells(lane_rows).values())
    assert mix.lines == () and mix.held == () and mix.next_action == ""
    assert "google" in mix.active
    writers = {r.role: r.lane for r in lane_rows if r.role in ("implementer", "tester", "doc-writer")}
    assert writers == {"implementer": "Claude Code", "tester": "Claude Code", "doc-writer": "Claude Code"}
    assert not [r for r in lane_rows if r.role.split(" ")[0] in ("implementer", "tester", "doc-writer")
                and r.provider == "google"]


# --- AC-S10c ---------------------------------------------------------------------------------------------------------

def test_with_agy_not_ticked_the_line_says_so_and_no_cell_reads_on(monkeypatch, tmp_path):
    root = fk.make_root(tmp_path)
    fk.use(monkeypatch, root)
    fk.write(root, fk.valid_doc(root))                                  # even a valid record does not switch it on
    _doc, _rows, mix, lane_rows = mix_for(ticked=False, acks=ack(BALANCED_ROLES))
    assert mix.lines == ("Gemini not active: Antigravity not selected",)
    assert all(text.endswith("(off)") for text in cells(lane_rows).values()) and len(cells(lane_rows)) == 4
    assert "google" not in mix.active


def test_an_acknowledgement_that_misses_planner_holds_only_planner_and_names_the_fix(monkeypatch, tmp_path):
    root = fk.make_root(tmp_path)
    fk.use(monkeypatch, root)
    fk.write(root, fk.valid_doc(root))
    _doc, _rows, mix, lane_rows = mix_for(ticked=True, acks=ack(["explore", "code-reviewer", "software-architect"]))
    assert cells(lane_rows)["planner (second)"].endswith("(held)")
    assert cells(lane_rows)["explore (lane)"].endswith("(on)")
    assert mix.lines == ("Gemini held: the Google acknowledgement does not list planner. Next: "
                         "extend the Google acknowledgement to: planner",)
    assert [role for role, _w in mix.held] == ["planner"]


# --- AC-S10d: OpenRouter ---------------------------------------------------------------------------------------------

def or_mix(**kw):
    doc = profiles.load_profile("gateway/balanced", "multi-model")
    rows = base_rows(doc)
    return rows, team_view.provider_mix(doc, rows, row="openrouter", **kw), team_view.role_lane_rows(
        rows, doc, row="openrouter", ticked=False, acks=None, **kw)


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


# --- the tools table and the seconds ------------------------------------------------------------------------------------

def test_role_lane_rows_list_the_seconds_of_every_report_role_and_the_real_provider():
    doc = balanced()
    lane_rows = team_view.role_lane_rows(base_rows(doc), doc, row="cli", ticked=True, acks=None)
    names = [r.role for r in lane_rows]
    for second in ("code-reviewer (second)", "planner (second)", "software-architect (second)"):
        assert second in names
    assert next(r for r in lane_rows if r.role == "implementer").provider == "anthropic"
    assert all(r.provider != "-" for r in lane_rows)


def test_the_wrappers_in_the_wizard_still_answer(monkeypatch):
    doc = balanced()
    assert wizard._agy_team_roles(doc) == team_view.agy_team_roles(doc)
    assert any("planner (second opinion): gemini-3.1-pro-low [unconfirmed slug]" == e for e in wizard._agy_team_roles(doc))
    assert wizard._agy_lanes_on(types.SimpleNamespace(profile=types.SimpleNamespace(lanes=None))) is False
    s = state.SetupState()
    s.profile.lanes = {"agents": {"a": {"backend": "agy-cli-lane"}}}
    assert wizard._agy_lanes_on(s) is True


def test_a_confirmed_slug_loses_its_tag_per_slug(monkeypatch, tmp_path):
    root = fk.make_root(tmp_path)
    fk.use(monkeypatch, root)
    doc = fk.valid_doc(root)
    only = [e for e in doc["gates"] if e["id"] == "G1"][0]
    only["confirmed_slugs"] = fk.preset_slugs(root)
    fk.write(root, fk.resealed(doc))
    assert team_view.cell_text("agy:gemini-3.8-flash-high", "on") == "google gemini-3.8-flash-high via Antigravity (on)"
    assert "[unconfirmed slug]" in team_view.cell_text("agy:gpt-oss-120b-medium", "held")


# --- AC-S10e: the Antigravity tick is not remembered without its acknowledgement ----------------------------------------

def detected(*ids):
    return {i: types.SimpleNamespace(installed=True) for i in ids}


def test_a_new_user_does_not_start_with_antigravity_ticked():
    s = state.SetupState()
    assert "agy" not in wizard._default_members(s, detected("claude", "agy"))


def test_a_saved_tick_without_a_valid_acknowledgement_is_not_restored_and_with_one_it_is():
    s = state.SetupState()
    s.profile.customizations = {"__targets__": {"selected": ["claude", "agy"]}}
    assert wizard._default_members(s, detected("claude", "agy")) == ["claude"]
    s.profile.lane_acks = {"google": {"roles": ["explore"], "text_sha256": "stale", "account": "a@example.com"}}
    assert wizard._default_members(s, detected("claude", "agy")) == ["claude"]
    s.profile.lane_acks = ack(["explore"])
    assert wizard._default_members(s, detected("claude", "agy")) == ["claude", "agy"]


# --- AC-S10g: markup in model text is shown as text ------------------------------------------------------------------------

def test_markup_in_a_model_or_lane_is_escaped(capsys):
    ui.role_lane_table([("[bold red]role[/]", "goo[/]gle", "[link=http://x]m[/link]", "[red]lane[/red]")], title="[b]t[/b]")
    out = capsys.readouterr().out
    assert "[bold red]role[/]" in out and "[red]lane[/red]" in out and "[link=http://x]m[/link]" in out


def test_display_only_seconds_keep_held_and_off_and_do_not_activate_google_alone(monkeypatch, tmp_path):
    root = fk.make_root(tmp_path)
    fk.use(monkeypatch, root)
    fk.write(root, fk.valid_doc(root))
    ack_ = ack(BALANCED_ROLES)
    assert team_view.member_state("planner", ticked=True, acks=ack_, kind="second")[0] == "display-only"
    assert team_view.member_state("software-architect", ticked=False, acks=ack_, kind="second")[0] == "off"
    assert team_view.member_state("planner", ticked=True, acks=None, kind="second")[0] == "held"
    assert team_view.member_state("code-reviewer", ticked=True, acks=ack_, kind="second")[0] != "display-only"

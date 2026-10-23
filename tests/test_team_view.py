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
    "code-reviewer (second)": "google gemini-4-argon-high via Antigravity",
    "planner (second)": "google gemini-4-argon-high via Antigravity",
    "software-architect (second)": "google gemini-4-argon-high via Antigravity",
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
    row_only = ("claude_billed", "effort")
    return doc, rows, team_view.provider_mix(doc, rows, **{k: v for k, v in kw.items() if k not in row_only}), \
        team_view.role_lane_rows(rows, doc, **{k: v for k, v in kw.items() if k in (
            "row", "ticked", "acks", "allow_unverified", "cell_verified", *row_only)})


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
    assert any("planner (second opinion): gemini-4-argon-high [unconfirmed slug]" == e for e in wizard._agy_team_roles(doc))
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


# --- ADR-0010 advisories: the display-only set follows the workflow, and display-only members never activate Google ---

def _dispatched_second_roles():
    import pathlib
    import yaml
    path = pathlib.Path(__file__).resolve().parents[1] / "workflows" / "feature-lanes.workflow.yaml"
    steps = yaml.safe_load(path.read_text())["steps"]
    return {s["subagent_type"] for s in steps if (s.get("execution_hints") or {}).get("lane_member") == "second"}


def test_display_only_seconds_are_derived_from_the_feature_lanes_workflow():
    dispatched = _dispatched_second_roles()
    assert dispatched, "the workflow must keep at least one step with lane_member: second"
    declared = {role for kind, role, _c in team_view.agy_members(balanced()) if kind == "second"}
    assert team_view.DISPLAY_ONLY_SECONDS == frozenset(declared - dispatched)
    assert not team_view.DISPLAY_ONLY_SECONDS & dispatched


def test_only_display_only_gemini_members_ticked_does_not_report_google_active(monkeypatch, tmp_path):
    root = fk.make_root(tmp_path)
    fk.use(monkeypatch, root)
    fk.write(root, fk.valid_doc(root))                         # a valid record: nothing is held
    display_only = sorted(team_view.DISPLAY_ONLY_SECONDS)
    doc = balanced()
    doc = {**doc, "lanes": {**doc["lanes"], "roles": {}, "second": {r: doc["lanes"]["second"][r] for r in display_only}}}
    rows = base_rows(doc)
    mix = team_view.provider_mix(doc, rows, row="cli", ticked=True, acks=ack(BALANCED_ROLES))
    assert set(mix.states.values()) == {"display-only"} and len(mix.states) == len(display_only)
    assert "google" in mix.declared and "google" not in mix.active
    assert mix.held == () and mix.lines == ()
    # and the same team with a dispatching member added does activate it
    full = balanced()
    assert "google" in team_view.provider_mix(full, base_rows(full), row="cli", ticked=True,
                                              acks=ack(BALANCED_ROLES)).active


# --- ADR-0016 D3 (AC-4): no host overlay, absent evidence is held ------------------------------------------------------

def test_the_kit_default_root_without_a_record_holds_every_agy_member_and_never_reads_on(monkeypatch):
    """Not a tmp root: the real kit root has no committed record, and `PYTEST_CURRENT_TEST` is removed so no bypass
    can matter. Absent evidence means the gates are not proven."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    ev.clear_cache()
    assert ev.load().status == "absent"
    _doc, _rows, mix, lane_rows = mix_for(ticked=True, acks=ack(BALANCED_ROLES + ["verifier"]))
    texts = list(cells(lane_rows).values())
    assert len(texts) == 4 and all(t.endswith("(held)") for t in texts)
    assert not any("(on)" in t for t in texts)
    assert {role for role, _why in mix.held} == set(BALANCED_ROLES)
    assert all("gates not proven" in why for _role, why in mix.held)


def test_member_state_is_held_with_the_refusal_reason_when_no_record_exists(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    ev.clear_cache()
    st, why = team_view.member_state("explore", ticked=True, acks=ack(BALANCED_ROLES))
    assert st == team_view.HELD and why.startswith("held: Antigravity lane gates not proven")


def test_a_valid_record_fixture_reads_on_for_the_lane_member(monkeypatch, tmp_path):
    root = fk.make_root(tmp_path)
    fk.use(monkeypatch, root)
    fk.write(root, fk.valid_doc(root))
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    st, why = team_view.member_state("explore", ticked=True, acks=ack(BALANCED_ROLES))
    assert (st, why) == (team_view.ON, "")


def test_the_review_suffix_is_computed_from_the_gate_record_not_a_literal(monkeypatch, tmp_path):
    s = state.SetupState()
    s.profile.lane_acks = ack(BALANCED_ROLES)
    ev.clear_cache()
    assert "held" in wizard._agy_team_suffix(s) and "(on" not in wizard._agy_team_suffix(s)
    root = fk.make_root(tmp_path)
    fk.use(monkeypatch, root)
    fk.write(root, fk.valid_doc(root))
    assert "held" not in wizard._agy_team_suffix(s)


# --- ADR-0016 D5: separate Runs in and Billed to columns ---------------------------------------------------------------

def test_role_lane_rows_carry_billed_to_as_its_own_field(monkeypatch, tmp_path):
    fk.use(monkeypatch, fk.make_root(tmp_path))
    _doc, _rows, _mix, lane_rows = mix_for(ticked=True, acks=ack(BALANCED_ROLES), claude_billed="subscription",
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
    via = team_view.role_lane_rows(rows, doc, row="openrouter", ticked=False, acks={})
    assert {r.billed_to for r in via} == {"API per token (OpenRouter)"}
    alone = team_view.role_lane_rows(rows, doc, row="single", ticked=False, acks={})
    assert {r.billed_to for r in alone} == {"unknown"}


def test_review_role_rows_never_guess(monkeypatch, tmp_path):
    fk.use(monkeypatch, fk.make_root(tmp_path))
    doc = balanced()
    rows = base_rows(doc)[:2]
    out = team_view.review_role_rows(rows, doc, selected=["claude", "openclaw", "agy"], claude_billed="unknown",
                                     openclaw_models={rows[0][0]: "openai/gpt-x"}, openclaw_billed={"anthropic": "subscription"},
                                     acks=ack(BALANCED_ROLES))
    first = [r for r in out if r.role == rows[0][0]]
    assert [r.runs_in for r in first] == ["Claude Code subagent", "OpenClaw kit-role agent"]
    assert first[0].billed_to == "unknown" and first[1].billed_to == "unknown" and first[1].model == "openai/gpt-x"
    second = next(r for r in out if r.role == rows[1][0] and r.runs_in == "OpenClaw kit-role agent")
    assert second.model == "unknown" and second.billed_to == "unknown"          # no model known: said so
    agy = [r for r in out if r.runs_in.startswith("agy")]
    assert agy and all(r.runs_in == "agy (held)" for r in agy)                   # no gate evidence: held, never "agy"

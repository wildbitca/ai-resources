"""Antigravity is a tool like any other (ADR-0009): ticking it on "Your tools" is the opt-in, the Google terms check
follows on the team screen, and everything ADR-0007 section 11 guards still holds: never ticked on its own, accept
stores exactly what `--lanes-antigravity` stores, a decline writes nothing, the writer stays on Claude Code, the
Codex row is never a choice. Offline; the profile is a fixture copy and no gate is proven unless a test says so."""
from __future__ import annotations

import argparse
import copy
import dataclasses
import types

import pytest

from ai_resources import agy_lane_gates as gates_mod
from ai_resources import roles_policy
from ai_resources.setup import detection, lanes_render, profiles, state, ui, wizard
from ai_resources.setup.cockpits import openclaw_lanes

PROFILE = "subscription-claude-55"
ACCT = "operator@example.test"


def _args(**kw):
    ns = dict(lanes=False, no_lanes=False, lanes_dry_run=False, lanes_orchestrator=["main"],
              allow_unverified=False, lanes_antigravity=False, no_lanes_antigravity=False)
    ns.update(kw)
    return argparse.Namespace(**ns)


@pytest.fixture(autouse=True)
def _profile(monkeypatch):
    """A profile with Antigravity candidates on the roles ADR-0007 section 11 accepts (as in test_wizard_lanes_agy)."""
    monkeypatch.setattr(lanes_render, "AGY_LANE_ADR_ACCEPTED", True)
    real = profiles.load_profile

    def load(name):
        doc = copy.deepcopy(real(name))
        lanes = doc.get("lanes")
        if not lanes:
            return doc
        for role in ("tester", "doc-writer"):
            lanes["roles"][role] = ["claude-cli:sonnet", "agy:claude-sonnet-5-5", "claude-code"]
        lanes["roles"]["implementer"] = ["claude-cli:sonnet", "agy:claude-sonnet-5-5", "claude-code"]
        lanes["second"]["code-reviewer"] = ["agy:claude-sonnet-5-5", "claude-cli:sonnet"]
        return doc
    monkeypatch.setattr(profiles, "load_profile", load)


def _prove_gates(monkeypatch):
    monkeypatch.setattr(gates_mod, "EVIDENCE", {g.id: dataclasses.replace(g, status=gates_mod.PROVEN,
                                                                         evidence="test-fixture")
                                                 for g in gates_mod.GATES})


def _s(*, ticked=("claude", "agy"), listed=True):
    s = state.SetupState()
    s.mode, s.cli_team = "single-model", True
    s.profile.name = PROFILE
    s.profile.customizations = {"__targets__": {"selected": list(ticked)}}
    if listed:
        s._tool_members_this_run = list(ticked)
    return s


class Ui:
    def __init__(self, monkeypatch, *, google=True, anthropic=True, account=ACCT):
        self.warned, self.asked, self.boxes, self.infos = [], [], [], []
        monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
        monkeypatch.setattr(ui, "section", lambda *a: None)
        monkeypatch.setattr(ui, "warn", lambda m: self.warned.append(m))
        monkeypatch.setattr(ui, "info", lambda m: self.infos.append(m))
        monkeypatch.setattr(ui, "detail", lambda m: None)
        monkeypatch.setattr(ui, "error", lambda m: self.infos.append(m))

        def confirm(msg, default=True):
            if "Enable subscription lanes" in msg:      # the Claude parallel runs question (moved by S9): not under test
                return False
            self.asked.append(msg)
            return google if "Google" in msg else anthropic
        monkeypatch.setattr(ui, "confirm", confirm)
        monkeypatch.setattr(ui, "text", lambda msg, default="", validate=None: (
            self.asked.append(msg) or (account if "Google account" in msg else "main")))

        def checkbox(msg, choices, default=None, instruction=""):
            self.boxes.append((msg, [getattr(c, "value", c) for c in choices], list(default or [])))
            return list(default or [])
        monkeypatch.setattr(ui, "checkbox", checkbox)


def _agy_part(s):
    lanes = s.profile.lanes or {}
    return {"agents": {a: v for a, v in (lanes.get("agents") or {}).items() if v["backend"] != "claude-cli"},
            "gates": lanes.get("gates"),
            "held": [h for h in (lanes.get("held") or []) if h["candidate"].startswith("agy:")]}


# --- the tick is the opt-in ---------------------------------------------------------------------------------
def test_ticked_and_accepted_stores_the_google_ack_and_opens_the_gate(monkeypatch):
    u = Ui(monkeypatch)
    s = _s()
    assert wizard._step_lanes(s, _args()) == 0
    assert u.warned.count(lanes_render.ACK_TEXT["google"]) == 1
    google = s.profile.lane_acks["google"]
    assert google["account"] == ACCT and "implementer" not in google["roles"]
    assert lanes_render.valid_acks(s.profile.lane_acks).keys() == {"google"}
    assert s.profile.lanes["gates"] == {"antigravity": True}
    assert "agy" in s.profile.customizations["__targets__"]["selected"]


def test_ticking_it_does_not_switch_the_claude_parallel_runs_on(monkeypatch):
    u = Ui(monkeypatch)
    s = _s()
    wizard._step_lanes(s, _args())
    assert not any(a["backend"] == "claude-cli" for a in s.profile.lanes["agents"].values())
    assert "anthropic" not in s.profile.lane_acks
    assert not any("Anthropic" in m or "Acknowledge for these roles" in m for m in u.warned + u.asked)


def test_team_row_accept_matches_flag_path(monkeypatch):
    """Same Google acknowledgement, same Antigravity agents, gate and held candidates as `--lanes-antigravity` for the
    same account and roles; the Claude parallel runs are the only difference (the flag also enables them)."""
    for prove in (False, True):
        if prove:
            _prove_gates(monkeypatch)
        Ui(monkeypatch)
        team = _s()
        assert wizard._step_lanes(team, _args()) == 0
        Ui(monkeypatch)
        flag = _s(listed=False)
        assert wizard._step_lanes(flag, _args(lanes=True, lanes_antigravity=True)) == 0
        a, b = team.profile.lane_acks["google"], flag.profile.lane_acks["google"]
        assert {k: v for k, v in a.items() if k != "date"} == {k: v for k, v in b.items() if k != "date"}
        assert _agy_part(team) == _agy_part(flag)


def test_team_row_decline_fails_closed(monkeypatch):
    u = Ui(monkeypatch, google=False)
    s = _s()
    before = copy.deepcopy(s.profile.lane_acks)
    assert wizard._step_lanes(s, _args()) == 0
    assert s.profile.lane_acks == before and s.profile.lanes is None
    assert "agy" not in s.profile.customizations["__targets__"]["selected"]
    assert "claude" in s.profile.customizations["__targets__"]["selected"]


def test_an_empty_account_unticks_and_stores_nothing(monkeypatch):
    Ui(monkeypatch, account=" ")
    s = _s()
    assert wizard._step_lanes(s, _args()) == 0
    assert s.profile.lane_acks is None and s.profile.lanes is None
    assert "agy" not in s.profile.customizations["__targets__"]["selected"]


def test_ctrl_c_and_back_at_the_terms_check_store_nothing(monkeypatch):
    for exc in (ui.Cancelled("terms"), ui.GoBack()):
        Ui(monkeypatch)

        def boom(msg, default=True, _e=exc):
            raise _e
        monkeypatch.setattr(ui, "confirm", boom)
        s = _s()
        with pytest.raises(type(exc)):
            wizard._step_lanes(s, _args())
        assert s.profile.lane_acks is None and s.profile.lanes is None


def test_an_unticked_antigravity_is_never_asked(monkeypatch):
    u = Ui(monkeypatch)
    s = _s(ticked=("claude",))
    assert wizard._step_lanes(s, _args()) == 0
    assert u.warned == [] and u.asked == [] and s.profile.lanes is None and s.profile.lane_acks is None


def test_without_the_interactive_list_only_the_flags_decide(monkeypatch):
    u = Ui(monkeypatch)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    s = _s(listed=False)
    assert wizard._step_lanes(s, _args()) == 0
    assert s.profile.lanes is None and s.profile.lane_acks is None
    assert wizard._step_lanes(s, _args(lanes=True, lanes_antigravity=True)) == 2      # no stored ack: exit 2 as before


def test_a_dry_run_shows_no_terms_check_and_stores_nothing(monkeypatch):
    u = Ui(monkeypatch)
    s = _s()
    assert wizard._step_lanes(s, _args(), dry_run=True) == 0
    assert u.warned == [] and s.profile.lanes is None and s.profile.lane_acks is None
    assert any("DRY RUN" in m for m in u.infos)


def test_a_stored_valid_ack_is_not_asked_again_on_a_later_run(monkeypatch):
    Ui(monkeypatch)
    s = _s()
    wizard._step_lanes(s, _args())
    roles = s.profile.lane_acks["google"]["roles"]
    u = Ui(monkeypatch)
    s2 = _s()
    s2.profile.lane_acks, s2.profile.lanes = s.profile.lane_acks, s.profile.lanes
    assert wizard._step_lanes(s2, _args()) == 0
    assert u.warned == [] and s2.profile.lane_acks["google"]["roles"] == roles


def test_unticking_a_tool_that_was_on_closes_the_gate_and_keeps_the_record(monkeypatch):
    Ui(monkeypatch)
    _prove_gates(monkeypatch)
    s = _s()
    wizard._step_lanes(s, _args())
    assert s.profile.lanes["gates"] == {"antigravity": True} and _agy_part(s)["agents"]
    Ui(monkeypatch)
    s.profile.customizations = {"__targets__": {"selected": ["claude"]}}
    s._tool_members_this_run = ["claude"]
    assert wizard._step_lanes(s, _args()) == 0
    assert "gates" not in s.profile.lanes and not _agy_part(s)["agents"]
    assert "google" in s.profile.lane_acks


def test_a_team_that_gives_antigravity_no_role_unticks_it_without_a_question(monkeypatch):
    u = Ui(monkeypatch)
    s = _s()
    s.profile.name = "claude-native"                          # no lanes block: no Antigravity candidate
    assert wizard._step_lanes(s, _args()) == 0
    assert u.warned == [] and u.asked == [] and s.profile.lanes is None and s.profile.lane_acks is None
    assert "agy" not in s.profile.customizations["__targets__"]["selected"]
    assert any("takes no role" in m for m in u.infos)


def test_each_on_its_own_never_opens_the_terms_check(monkeypatch):
    u = Ui(monkeypatch)
    s = _s()
    s.cli_team = False
    assert wizard._step_lanes(s, _args()) == 0
    assert u.warned == [] and s.profile.lanes is None and s.profile.lane_acks is None
    assert "agy" in s.profile.customizations["__targets__"]["selected"]      # still a tool; no lane, no store


# --- what ticking can never do ---------------------------------------------------------------------------------
def test_writer_never_agy(monkeypatch):
    _prove_gates(monkeypatch)
    Ui(monkeypatch)
    s = _s()
    wizard._step_lanes(s, _args())
    implementer = s.profile.lanes["roles"]["implementer"]["candidates"]
    agy_ids = {a for a, v in s.profile.lanes["agents"].items() if v["backend"] != "claude-cli"}
    assert agy_ids and not any(c.split(":", 1)[-1] in agy_ids for c in implementer if c.startswith("openclaw-agent"))
    assert roles_policy.DEFAULT_INTEGRATING_WRITER == "implementer"
    assert openclaw_lanes.AGY_BACKEND not in openclaw_lanes.WRITABLE_BACKENDS


def test_the_gates_stay_not_proven_and_the_candidates_stay_held(monkeypatch):
    Ui(monkeypatch)
    s = _s()
    wizard._step_lanes(s, _args())
    assert gates_mod.refusal()                       # NOT PROVEN: the real evidence table is untouched
    assert openclaw_lanes.AGY_BACKEND not in openclaw_lanes.writable_backends()
    assert not _agy_part(s)["agents"] and _agy_part(s)["held"]


def test_codex_row_never_selectable(monkeypatch):
    assert lanes_render.CODEX_LANE_ADR_ACCEPTED is False
    monkeypatch.setattr(detection, "agy_installed", lambda: True)
    s = _s()
    s.cockpits["codex"] = state.CockpitState(installed=True)
    s.profile.customizations = {"__targets__": {"selected": ["claude", "codex", "agy"]}}
    rows = {r[0]: r for r in wizard._team_rows(s, profiles.load_profile(PROFILE), [("planner", "x", "y")])}
    assert rows["Codex CLI"][2] == "-" and "no roles yet" in rows["Codex CLI"][1]
    Ui(monkeypatch)
    wizard._step_lanes(s, _args())
    assert not any(a["backend"] == "codex" for a in (s.profile.lanes or {}).get("agents", {}).values())


# --- the team table ----------------------------------------------------------------------------------------------
def test_the_team_table_shows_antigravity_roles_only_when_ticked(monkeypatch):
    monkeypatch.setattr(detection, "agy_installed", lambda: True)
    base = profiles.load_profile(PROFILE)
    rows = [("planner", "anthropic", "opus")]
    on = {r[0]: r for r in wizard._team_rows(_s(), base, rows)}
    off = {r[0]: r for r in wizard._team_rows(_s(ticked=("claude",)), base, rows)}
    assert on["Antigravity"][2].startswith("in team") and "sends repo content to Google" in on["Antigravity"][1]
    assert "implementer" not in on["Antigravity"][1] and "tester" in on["Antigravity"][1]
    assert off["Antigravity"][2] == "off" and "tester" not in off["Antigravity"][1]
    assert on["Claude Code"][1] == "planner"


def test_the_team_table_has_no_antigravity_row_when_it_is_not_installed(monkeypatch):
    monkeypatch.setattr(detection, "agy_installed", lambda: False)
    rows = {r[0] for r in wizard._team_rows(_s(), profiles.load_profile(PROFILE), [("planner", "a", "b")])}
    assert "Antigravity" not in rows

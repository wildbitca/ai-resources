"""The wizard's Antigravity lane step (ADR-0007 section 11, P3-S3): per-account role sets, the Google
acknowledgement, the non-interactive rule and the revocation flag. Offline; the profile is a fixture copy."""
from __future__ import annotations

import argparse
import copy
import dataclasses

import pytest

from ai_resources import agy_lane_gates as gates_mod
from ai_resources.setup import lanes_render, profiles, state, ui, wizard

PROFILE = "subscription-claude-55"
ACCT = "operator@example.test"


def _args(**kw):
    ns = dict(lanes=False, no_lanes=False, lanes_dry_run=False, lanes_orchestrator=["main"],
              allow_unverified=False, lanes_antigravity=False, no_lanes_antigravity=False)
    ns.update(kw)
    return argparse.Namespace(**ns)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    monkeypatch.setattr(lanes_render, "AGY_LANE_ADR_ACCEPTED", True)
    monkeypatch.setattr(gates_mod, "EVIDENCE",
                        {g.id: dataclasses.replace(g, status=gates_mod.PROVEN, evidence="test-fixture")
                         for g in gates_mod.GATES})
    real = profiles.load_profile

    def load(name, mode=None):
        doc = copy.deepcopy(real(name, mode))
        lanes = doc.get("lanes")
        if not lanes:
            return doc
        for role in ("implementer", "tester", "doc-writer"):
            lanes["roles"][role] = ["claude-cli:sonnet", "agy:claude-sonnet-5-5", "claude-code"]
        for role in ("verifier", "security-auditor"):
            lanes["roles"][role] = ["claude-code", "claude-cli:opus", "agy:claude-opus-5-5"]
        lanes["roles"]["planner"] = ["claude-code"]
        lanes["second"]["code-reviewer"] = ["agy:claude-sonnet-5-5", "claude-cli:sonnet"]
        # ADR-0010 S6: Balanced pins its Gemini candidates; this fixture swaps them for Claude ones, so the pins go.
        lanes.pop("pins", None)
        for role in ("planner", "software-architect"):
            lanes["second"].pop(role, None)
        lanes["roles"]["explore"] = ["claude-cli:haiku", "claude-code"]
        return doc
    monkeypatch.setattr(profiles, "load_profile", load)


def _s():
    s = state.SetupState()
    s.mode = "single-model"
    s.profile.name = PROFILE
    return s


def _anthropic(s):
    doc = profiles.load_profile(PROFILE)
    roles = sorted(set(doc["lanes"]["roles"]) | set(doc["lanes"]["second"]))
    return {"anthropic": lanes_render.make_ack("anthropic", roles, "2026-10-16")}


def _google(roles, account=ACCT):
    return {"google": lanes_render.make_ack("google", roles, "2026-10-16", account=account)}


def test_lanes_alone_never_shows_the_google_text_and_holds_every_agy_candidate(monkeypatch):
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    shown = []
    monkeypatch.setattr(ui, "warn", lambda m: shown.append(m))
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(ui, "text", lambda *a, **k: "main")
    s = _s()
    assert wizard._step_lanes(s, _args(lanes=True)) == 0
    assert lanes_render.ACK_TEXT["google"] not in shown
    assert not any(a["backend"] != "claude-cli" for a in s.profile.lanes["agents"].values())
    assert s.profile.lanes["held"] and "gates" not in s.profile.lanes


def test_non_interactive_antigravity_without_a_valid_google_ack_exits_2_and_writes_nothing():
    s = _s()
    s.profile.lane_acks = _anthropic(s)
    before = state._to_dict(s)
    assert wizard._step_lanes(s, _args(lanes=True, lanes_antigravity=True)) == 2
    assert state._to_dict(s) == before and s.profile.lanes is None


@pytest.mark.parametrize("bad", [
    {"google": {**_google(["tester"])["google"], "text_sha256": "0" * 64}},       # stale text
    _google(["tester"], account=""),                                               # no account
    _google([]),                                                                   # lists no role
])
def test_non_interactive_antigravity_needs_a_valid_ack_listing_at_least_one_role(bad):
    s = _s()
    s.profile.lane_acks = {**_anthropic(s), **bad}
    assert wizard._step_lanes(s, _args(lanes=True, lanes_antigravity=True)) == 2
    assert s.profile.lanes is None


def test_non_interactive_with_a_partial_ack_holds_the_unlisted_roles_and_is_idempotent():
    s = _s()
    s.profile.lane_acks = {**_anthropic(s), **_google(["tester", "doc-writer"])}
    assert wizard._step_lanes(s, _args(lanes=True, lanes_antigravity=True)) == 0
    lanes = s.profile.lanes
    assert lanes["gates"] == {"antigravity": True}
    assert "kit-lane-agy-sonnet55" in lanes["agents"]
    held = {(h["role"], h["candidate"]): h["reason"] for h in lanes["held"]}
    assert "does not list implementer" in held[("implementer", "agy:claude-sonnet-5-5")]
    assert ("tester", "agy:claude-sonnet-5-5") not in held
    first = copy.deepcopy(state._to_dict(s))
    assert wizard._step_lanes(s, _args(lanes=True, lanes_antigravity=True)) == 0
    assert state._to_dict(s) == first


def test_allow_unverified_opens_neither_account():
    s = _s()
    s.profile.lane_acks = _anthropic(s)
    assert wizard._step_lanes(s, _args(lanes=True, allow_unverified=True)) == 0
    assert not any(a["backend"] != "claude-cli" for a in s.profile.lanes["agents"].values())
    assert "gates" not in s.profile.lanes


def test_a_changed_google_text_drops_the_ack_and_holds_agy_until_re_signed(monkeypatch):
    s = _s()
    s.profile.lane_acks = {**_anthropic(s), **_google(["tester"])}
    monkeypatch.setitem(lanes_render.ACK_TEXT, "google", lanes_render.ACK_TEXT["google"] + " Changed.")
    assert wizard._step_lanes(s, _args(lanes=True, lanes_antigravity=True)) == 2


def _interactive(monkeypatch, *, google=True, anthropic=True, account=ACCT):
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    log = {"warn": [], "checkbox": [], "text": [], "confirm": []}
    monkeypatch.setattr(ui, "warn", lambda m: log["warn"].append(m))

    def confirm(msg, default=True):
        log["confirm"].append(msg)
        return google if "Google" in msg else anthropic
    monkeypatch.setattr(ui, "confirm", confirm)

    def text(msg, default="", validate=None):
        log["text"].append(msg)
        return account if "Google account" in msg else "main"
    monkeypatch.setattr(ui, "text", text)

    def checkbox(msg, choices, default=None, instruction=""):
        log["checkbox"].append((msg, [getattr(c, "value", c) for c in choices], list(default or [])))
        return list(default or [])
    monkeypatch.setattr(ui, "checkbox", checkbox)
    return log


def test_interactive_shows_the_google_text_once_with_the_account_prompt_and_unticks_the_implementer(monkeypatch):
    log = _interactive(monkeypatch)
    s = _s()
    assert wizard._step_lanes(s, _args(lanes=True, lanes_antigravity=True)) == 0
    assert log["warn"].count(lanes_render.ACK_TEXT["google"]) == 1
    assert any("Google account" in m for m in log["text"])
    (msg, choices, default), = log["checkbox"]
    assert "implementer" in choices and "implementer" not in default
    assert {"tester", "doc-writer", "verifier", "security-auditor", "code-reviewer"} <= set(default)
    google = s.profile.lane_acks["google"]
    assert google["account"] == ACCT and "implementer" not in google["roles"]
    assert lanes_render.valid_acks(s.profile.lane_acks).keys() == {"anthropic", "google"}
    assert s.profile.lanes["gates"] == {"antigravity": True}


def test_per_account_role_sets_cover_only_the_roles_each_account_runs(monkeypatch):
    _interactive(monkeypatch)
    s = _s()
    assert wizard._step_lanes(s, _args(lanes=True, lanes_antigravity=True)) == 0
    anth, goog = set(s.profile.lane_acks["anthropic"]["roles"]), set(s.profile.lane_acks["google"]["roles"])
    assert "explore" in anth and "explore" not in goog          # claude-cli only
    assert "planner" in anth                                    # claude-code only role keeps its Anthropic row
    assert goog <= {"implementer", "tester", "doc-writer", "verifier", "security-auditor", "code-reviewer"}


def test_declining_google_keeps_agy_closed_while_claude_cli_proceeds(monkeypatch):
    _interactive(monkeypatch, google=False)
    s = _s()
    assert wizard._step_lanes(s, _args(lanes=True, lanes_antigravity=True)) == 0
    assert "google" not in (s.profile.lane_acks or {})
    assert state.lanes_enabled(s) and "gates" not in s.profile.lanes
    assert not any(a["backend"] != "claude-cli" for a in s.profile.lanes["agents"].values())


def test_no_lanes_antigravity_clears_the_gate_and_the_agy_agents_but_keeps_claude_cli_lanes():
    s = _s()
    s.profile.lane_acks = {**_anthropic(s), **_google(["tester"])}
    assert wizard._step_lanes(s, _args(lanes=True, lanes_antigravity=True)) == 0
    assert "kit-lane-agy-sonnet55" in s.profile.lanes["agents"]
    assert wizard._step_lanes(s, _args(no_lanes_antigravity=True)) == 0
    lanes = s.profile.lanes
    assert "gates" not in lanes
    assert all(a["backend"] == "claude-cli" for a in lanes["agents"].values()) and lanes["agents"]
    assert "kit-lane-agy-sonnet55" not in repr(lanes["roles"]) + repr(lanes["second"])
    assert lanes["orchestrators"] == ["main"]
    assert "google" in s.profile.lane_acks                      # the ack stays as a record


def test_the_flag_pair_contradicts_and_no_lanes_wins_over_nothing():
    s = _s()
    assert wizard._step_lanes(s, _args(lanes_antigravity=True, no_lanes_antigravity=True)) == 2
    assert wizard._step_lanes(s, _args(no_lanes=True, lanes_antigravity=True)) == 2


def test_a_stale_google_ack_proposes_its_account_so_enter_re_signs(monkeypatch):
    log = _interactive(monkeypatch)
    proposed = []

    def text(msg, default="", validate=None):
        log["text"].append(msg)
        proposed.append(default)
        return default if "Google account" in msg else "main"
    monkeypatch.setattr(ui, "text", text)
    s = _s()
    s.profile.lane_acks = {**_anthropic(s), **_google(["tester"])}
    s.profile.lane_acks["google"]["text_sha256"] = "0" * 64
    assert wizard._step_lanes(s, _args(lanes=True, lanes_antigravity=True)) == 0
    assert ACCT in proposed
    assert lanes_render.valid_acks(s.profile.lane_acks)["google"]["account"] == ACCT

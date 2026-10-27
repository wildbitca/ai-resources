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


def _s():
    s = state.SetupState()
    s.mode = "single-model"
    s.profile.name = PROFILE
    return s


def _anthropic(s):
    return {"anthropic": lanes_render.make_ack("anthropic", lanes_render.lane_roles(), "2026-10-16")}


def _google(roles, account=ACCT):
    return {"google": lanes_render.make_ack("google", roles, "2026-10-16", account=account)}


AGY = lanes_render.agy_roles()


def test_lanes_alone_never_shows_the_google_text_and_opens_no_gate(monkeypatch):
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    shown = []
    monkeypatch.setattr(ui, "warn", lambda m: shown.append(m))
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(ui, "text", lambda *a, **k: "main")
    s = _s()
    assert wizard._step_lanes(s, _args(lanes=True)) == 0
    assert lanes_render.ACK_TEXT["google"] not in shown
    assert "gates" not in s.profile.lanes


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


def test_non_interactive_with_a_valid_ack_opens_the_gate_and_is_idempotent():
    s = _s()
    s.profile.lane_acks = {**_anthropic(s), **_google(["tester", "doc-writer"])}
    assert wizard._step_lanes(s, _args(lanes=True, lanes_antigravity=True)) == 0
    assert s.profile.lanes["gates"] == {"antigravity": True}
    assert set(s.profile.lanes) == {"orchestrators", "gates"}
    first = copy.deepcopy(state._to_dict(s))
    assert wizard._step_lanes(s, _args(lanes=True, lanes_antigravity=True)) == 0
    assert state._to_dict(s) == first


def test_allow_unverified_opens_neither_account():
    s = _s()
    s.profile.lane_acks = _anthropic(s)
    assert wizard._step_lanes(s, _args(lanes=True, allow_unverified=True)) == 0
    assert "gates" not in s.profile.lanes


def test_a_changed_google_text_drops_the_ack_until_re_signed(monkeypatch):
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


def test_interactive_shows_the_google_text_once_with_the_account_prompt_and_ticks_every_agy_role(monkeypatch):
    log = _interactive(monkeypatch)
    s = _s()
    assert wizard._step_lanes(s, _args(lanes=True, lanes_antigravity=True)) == 0
    assert log["warn"].count(lanes_render.ACK_TEXT["google"]) == 1
    assert any("Google account" in m for m in log["text"])
    (msg, choices, default), = log["checkbox"]
    assert set(choices) == set(AGY) == set(default)
    assert "implementer" in default                              # agy may write: the implementer is ticked
    google = s.profile.lane_acks["google"]
    assert google["account"] == ACCT and set(google["roles"]) == set(AGY)
    assert lanes_render.valid_acks(s.profile.lane_acks).keys() == {"anthropic", "google"}
    assert s.profile.lanes["gates"] == {"antigravity": True}


def test_per_account_role_sets_cover_only_the_roles_each_account_runs(monkeypatch):
    _interactive(monkeypatch)
    s = _s()
    assert wizard._step_lanes(s, _args(lanes=True, lanes_antigravity=True)) == 0
    anth, goog = set(s.profile.lane_acks["anthropic"]["roles"]), set(s.profile.lane_acks["google"]["roles"])
    assert "implementer" in anth and "verifier" in anth
    assert goog == set(AGY) and "verifier" in goog              # the checker ladder reaches agy:claude-opus-5-5


def test_declining_google_keeps_agy_closed_while_the_rest_proceeds(monkeypatch):
    _interactive(monkeypatch, google=False)
    s = _s()
    assert wizard._step_lanes(s, _args(lanes=True, lanes_antigravity=True)) == 0
    assert "google" not in (s.profile.lane_acks or {})
    assert state.lanes_enabled(s) and "gates" not in s.profile.lanes


def test_no_lanes_antigravity_clears_the_gate_but_keeps_the_lanes_and_the_ack_record():
    s = _s()
    s.profile.lane_acks = {**_anthropic(s), **_google(["tester"])}
    assert wizard._step_lanes(s, _args(lanes=True, lanes_antigravity=True)) == 0
    assert s.profile.lanes["gates"] == {"antigravity": True}
    assert wizard._step_lanes(s, _args(no_lanes_antigravity=True)) == 0
    assert "gates" not in s.profile.lanes and s.profile.lanes["orchestrators"] == ["main"]
    assert "google" in s.profile.lane_acks                      # the ack stays as a record

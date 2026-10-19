"""The Antigravity (Google) terms check as a standalone function (ADR-0007 section 11, ADR-0009).

`wizard.agy_terms_check(s, roles, interactive)` is the one place the Google acknowledgement is shown and stored. The
`--lanes-antigravity` flag path calls it, and so does the tool list of the setup screens. It fails closed: a
decline, an empty account, Ctrl-B or Ctrl-C stores nothing, and a non-interactive run without a valid stored
acknowledgement is refused. Offline; the state object is built by hand."""
from __future__ import annotations

import hashlib

import pytest

from ai_resources import roles_policy
from ai_resources.setup import lanes_render, state, ui, wizard
from ai_resources.setup.cockpits import openclaw_lanes

ACCT = "operator@example.test"
ROLES = ["code-reviewer", "doc-writer", "explore", "implementer", "tester"]


def _s(acks=None):
    s = state.SetupState()
    s.mode = "single-model"
    s.cli_team = True
    s.profile.lane_acks = acks
    return s


def _google(roles, account=ACCT):
    return {"google": lanes_render.make_ack("google", roles, "2026-10-16", account=account)}


class Ui:
    """Scripted prompts; records what the operator was shown."""

    def __init__(self, monkeypatch, *, confirm=True, account=ACCT, picked=None):
        self.warned, self.details, self.errors, self.boxes, self.asks = [], [], [], [], []
        monkeypatch.setattr(ui, "warn", lambda m: self.warned.append(m))
        monkeypatch.setattr(ui, "detail", lambda m: self.details.append(m))
        monkeypatch.setattr(ui, "error", lambda m: self.errors.append(m))
        monkeypatch.setattr(ui, "info", lambda m: None)

        def text(msg, default="", validate=None):
            self.asks.append(msg)
            return account
        monkeypatch.setattr(ui, "text", text)

        def checkbox(msg, choices, default=None, instruction=""):
            self.boxes.append((msg, [getattr(c, "value", c) for c in choices], list(default or [])))
            return list(default or []) if picked is None else picked
        monkeypatch.setattr(ui, "checkbox", checkbox)

        def conf(msg, default=True):
            self.asks.append(msg)
            return confirm
        monkeypatch.setattr(ui, "confirm", conf)


def test_decline_stores_nothing(monkeypatch):
    s = _s()
    u = Ui(monkeypatch, confirm=False)
    accepted, acks, rc = wizard.agy_terms_check(s, ROLES, interactive=True)
    assert (accepted, rc) == (False, 0)
    assert "google" not in acks
    assert s.profile.lane_acks is None and s.profile.lanes is None
    assert u.warned == [lanes_render.ACK_TEXT["google"]]


def test_accept_stores_the_hash_account_roles_and_date_through_make_ack(monkeypatch):
    s = _s()
    u = Ui(monkeypatch, confirm=True)
    accepted, acks, rc = wizard.agy_terms_check(s, ROLES, interactive=True)
    assert (accepted, rc) == (True, 0)
    ack = acks["google"]
    text = lanes_render.ACK_TEXT["google"]
    assert ack["text_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert ack["text"] == text and ack["account"] == ACCT
    assert ack["roles"] == sorted(r for r in ROLES if r != roles_policy.DEFAULT_INTEGRATING_WRITER)
    assert ack["date"].startswith("20")
    assert lanes_render.valid_acks(acks)["google"]["account"] == ACCT


def test_the_google_text_is_shown_verbatim_and_once(monkeypatch):
    u = Ui(monkeypatch)
    wizard.agy_terms_check(_s(), ROLES, interactive=True)
    assert u.warned.count(lanes_render.ACK_TEXT["google"]) == 1


def test_the_implementer_is_not_ticked_by_default(monkeypatch):
    u = Ui(monkeypatch)
    wizard.agy_terms_check(_s(), ROLES, interactive=True)
    _, offered, default = u.boxes[0]
    assert roles_policy.DEFAULT_INTEGRATING_WRITER not in default
    assert "tester" in default


def test_ticking_the_implementer_is_the_operators_explicit_act(monkeypatch):
    Ui(monkeypatch, picked=["implementer", "tester"])
    accepted, acks, _ = wizard.agy_terms_check(_s(), ROLES, interactive=True)
    assert accepted and acks["google"]["roles"] == ["implementer", "tester"]


def test_an_empty_account_keeps_the_lane_closed_and_stores_nothing(monkeypatch):
    u = Ui(monkeypatch, account="  ")
    s = _s()
    accepted, acks, rc = wizard.agy_terms_check(s, ROLES, interactive=True)
    assert (accepted, rc) == (False, 0) and "google" not in acks
    assert any("account must be named" in e for e in u.errors)
    assert s.profile.lane_acks is None


def test_no_role_picked_is_a_decline(monkeypatch):
    Ui(monkeypatch, picked=[])
    accepted, acks, rc = wizard.agy_terms_check(_s(), ROLES, interactive=True)
    assert (accepted, rc) == (False, 0) and "google" not in acks


def test_ctrl_c_stores_nothing(monkeypatch):
    Ui(monkeypatch)

    def boom(msg, default=True):
        raise ui.Cancelled("terms")
    monkeypatch.setattr(ui, "confirm", boom)
    s = _s()
    with pytest.raises(ui.Cancelled):
        wizard.agy_terms_check(s, ROLES, interactive=True)
    assert s.profile.lane_acks is None


def test_back_stores_nothing(monkeypatch):
    Ui(monkeypatch)

    def back(msg, default=True):
        raise ui.GoBack()
    monkeypatch.setattr(ui, "confirm", back)
    s = _s()
    with pytest.raises(ui.GoBack):
        wizard.agy_terms_check(s, ROLES, interactive=True)
    assert s.profile.lane_acks is None


def test_non_interactive_without_a_stored_ack_is_refused_with_todays_message(monkeypatch):
    u = Ui(monkeypatch)
    accepted, acks, rc = wizard.agy_terms_check(_s(), ROLES, interactive=False)
    assert (accepted, rc) == (False, 2)
    assert any("--lanes-antigravity: no stored valid Google acknowledgement lists a role" in e for e in u.errors)
    assert u.asks == []


def test_non_interactive_with_a_stale_hash_is_refused(monkeypatch):
    Ui(monkeypatch)
    stale = {"google": {**_google(["tester"])["google"], "text_sha256": "0" * 64}}
    assert wizard.agy_terms_check(_s(stale), ROLES, interactive=False)[2] == 2


def test_non_interactive_with_a_valid_ack_keeps_it_and_asks_nothing(monkeypatch):
    u = Ui(monkeypatch)
    have = _google(["tester"])
    accepted, acks, rc = wizard.agy_terms_check(_s(have), ROLES, interactive=False)
    assert (accepted, rc) == (True, 0) and acks["google"] == have["google"] and u.asks == []


def test_a_stored_valid_ack_that_covers_every_role_is_not_asked_again(monkeypatch):
    u = Ui(monkeypatch)
    have = _google(ROLES)
    accepted, acks, rc = wizard.agy_terms_check(_s(have), ROLES, interactive=True)
    assert (accepted, rc) == (True, 0) and u.asks == [] and u.warned == []


def test_a_changed_text_makes_the_stored_ack_stale_in_an_interactive_run_too(monkeypatch):
    have = _google(ROLES)
    monkeypatch.setitem(lanes_render.ACK_TEXT, "google", lanes_render.ACK_TEXT["google"] + " Changed.")
    u = Ui(monkeypatch)
    accepted, acks, rc = wizard.agy_terms_check(_s(have), ROLES, interactive=True)
    assert accepted and u.warned == [lanes_render.ACK_TEXT["google"]]
    assert acks["google"]["text_sha256"] == hashlib.sha256(lanes_render.ACK_TEXT["google"].encode()).hexdigest()


def test_the_other_accounts_acknowledgement_is_kept(monkeypatch):
    anth = {"anthropic": lanes_render.make_ack("anthropic", ["tester"], "2026-10-16")}
    Ui(monkeypatch)
    accepted, acks, _ = wizard.agy_terms_check(_s(anth), ROLES, interactive=True)
    assert accepted and acks["anthropic"] == anth["anthropic"] and "google" in acks


def test_the_writer_never_moves_to_agy():
    assert roles_policy.DEFAULT_INTEGRATING_WRITER == "implementer"
    assert openclaw_lanes.WRITABLE_BACKENDS == frozenset({"claude-cli"})
    assert openclaw_lanes.AGY_BACKEND not in openclaw_lanes.writable_backends()


def test_the_function_does_not_touch_the_state_it_is_given(monkeypatch):
    Ui(monkeypatch)
    s = _s()
    before = state._to_dict(s)
    wizard.agy_terms_check(s, ROLES, interactive=True)
    assert state._to_dict(s) == before


# --- ADR-0010 (c): an acknowledgement stored before planner / software-architect were lane roles does not cover them

NEW_ROLES = ["code-reviewer", "explore", "implementer", "planner", "software-architect", "tester"]


def test_an_older_ack_is_asked_again_for_planner_and_architect_with_default_no(monkeypatch):
    have = _google(["code-reviewer", "explore"])
    u = Ui(monkeypatch)
    calls = []

    def conf(msg, default=True):
        calls.append((msg, default))
        return False
    monkeypatch.setattr(ui, "confirm", conf)
    accepted, acks, rc = wizard.agy_terms_check(_s(have), NEW_ROLES, interactive=True)
    extend = "Extend the Google acknowledgement to: implementer, planner, software-architect, tester?"
    assert calls == [(extend, False)]
    assert (accepted, rc) == (True, 0) and acks["google"] == have["google"]
    assert u.warned == [] and u.boxes == []


def test_non_interactive_never_accepts_the_extension_and_keeps_the_stored_ack(monkeypatch):
    have = _google(["code-reviewer", "explore"])
    u = Ui(monkeypatch)
    accepted, acks, rc = wizard.agy_terms_check(_s(have), NEW_ROLES, interactive=False)
    assert (accepted, rc) == (True, 0) and acks["google"] == have["google"]
    assert u.asks == [] and u.warned == [] and u.boxes == []


def test_the_checklist_pre_ticks_planner_architect_and_explore_but_not_the_implementer(monkeypatch):
    for have in (None, _google(["code-reviewer", "explore"])):
        u = Ui(monkeypatch)
        wizard.agy_terms_check(_s(have), NEW_ROLES, interactive=True)
        _, offered, default = u.boxes[0]
        assert {"planner", "software-architect", "explore"} <= set(default)
        assert roles_policy.DEFAULT_INTEGRATING_WRITER not in default and "implementer" in offered

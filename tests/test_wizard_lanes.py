"""The wizard's lane step (ADR-0007, plan P2-S6): disabled by default, explicit enable, acknowledgement."""
from __future__ import annotations

import argparse

import pytest

from ai_resources.setup import lanes_render, profiles, state, ui, wizard

PROFILE = "subscription-claude-55"


def _args(**kw):
    ns = dict(lanes=False, no_lanes=False, lanes_dry_run=False, lanes_orchestrator=[], allow_unverified=False)
    ns.update(kw)
    return argparse.Namespace(**ns)


@pytest.fixture(autouse=True)
def _non_interactive(monkeypatch):
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)


def _s():
    s = state.SetupState()
    s.mode = "single-model"
    s.profile.name = PROFILE
    return s


def _acks(s):
    roles = lanes_render.lane_roles(profiles.load_profile(PROFILE))
    return {"anthropic": lanes_render.make_ack("anthropic", roles, "2026-10-15")}


def test_fresh_install_no_flags_leaves_lanes_disabled():
    s = _s()
    before = state._to_dict(s)
    assert wizard._step_lanes(s, _args()) == 0
    assert s.profile.lanes is None and state._to_dict(s) == before
    assert not state.lanes_enabled(s)


def test_allow_unverified_does_not_imply_lanes():
    s = _s()
    assert wizard._step_lanes(s, _args(allow_unverified=True)) == 0
    assert s.profile.lanes is None


def test_non_interactive_lanes_without_a_stored_ack_refuses():
    s = _s()
    assert wizard._step_lanes(s, _args(lanes=True, lanes_orchestrator=["main"])) == 2
    assert s.profile.lanes is None


def test_non_interactive_lanes_with_a_stored_ack_enables():
    s = _s()
    s.profile.lane_acks = _acks(s)
    assert wizard._step_lanes(s, _args(lanes=True, lanes_orchestrator=["main"])) == 0
    assert state.lanes_enabled(s)
    assert s.profile.lanes["orchestrators"] == ["main"]
    assert set(s.profile.lanes) == {"orchestrators"}            # the role table is the matrix, never stored


def test_ack_that_misses_a_role_is_not_enough():
    s = _s()
    acks = _acks(s)
    acks["anthropic"]["roles"] = ["implementer"]
    s.profile.lane_acks = acks
    assert wizard._step_lanes(s, _args(lanes=True, lanes_orchestrator=["main"])) == 2


def test_interactive_enable_shows_the_ack_once_and_stores_it(monkeypatch):
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    shown = []
    monkeypatch.setattr(ui, "warn", lambda m: shown.append(m))
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(ui, "text", lambda *a, **k: "main")
    s = _s()
    assert wizard._step_lanes(s, _args(lanes=True)) == 0
    ack = s.profile.lane_acks["anthropic"]
    assert shown == [lanes_render.ACK_TEXT["anthropic"]] and ack["text"] == shown[0]
    assert set(ack["roles"]) == set(lanes_render.lane_roles(profiles.load_profile(PROFILE)))
    assert s.profile.lanes["orchestrators"] == ["main"]
    for key in ("date", "text_sha256", "text", "roles"):
        assert ack[key]


def test_declining_the_ack_leaves_lanes_disabled(monkeypatch):
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "warn", lambda m: None)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: False)
    monkeypatch.setattr(ui, "text", lambda *a, **k: "main")
    s = _s()
    assert wizard._step_lanes(s, _args(lanes=True)) == 0
    assert s.profile.lanes is None and not s.profile.lane_acks


def test_no_lanes_clears_the_table_and_the_file_follows_on_apply():
    s = _s()
    s.profile.lane_acks = _acks(s)
    wizard._step_lanes(s, _args(lanes=True, lanes_orchestrator=["main"]))
    lanes_render.sync_file(s)
    assert state.lanes_path().is_file()
    assert wizard._step_lanes(s, _args(no_lanes=True)) == 0
    assert s.profile.lanes is None
    wizard._apply_lanes_file(s, {"by_role": {}})
    assert not state.lanes_path().exists()
    assert str(state.lanes_path()) not in s.tracking.config_files_written


def test_lanes_dry_run_enables_nothing():
    s = _s()
    s.profile.lane_acks = _acks(s)
    assert wizard._step_lanes(s, _args(lanes_dry_run=True, lanes_orchestrator=["main"])) == 0
    assert s.profile.lanes is None


def test_lanes_and_no_lanes_contradict():
    assert wizard._step_lanes(_s(), _args(lanes=True, no_lanes=True)) == 2


def test_malformed_profile_routing_override_is_refused(monkeypatch):
    doc = {"name": "x", "mode": "multi-model", "by_role": {"implementer": {"model": "sonnet"}},
           "routing": {"implementer": {"primary": "nonsense:model"}}}
    monkeypatch.setattr(profiles, "load_profile", lambda name, mode=None: doc)
    s = state.SetupState()
    s.mode, s.profile.name = "multi-model", "x"
    errors = []
    monkeypatch.setattr(ui, "error", lambda m: errors.append(m))
    assert wizard._step_lanes(s, _args(lanes=True, lanes_orchestrator=["main"])) == 2
    assert any("routing.implementer" in e for e in errors)
    assert s.profile.lanes is None


def test_cli_exposes_the_lane_flags():
    from ai_resources import cli
    ns = cli.build_parser().parse_args(["setup", "--lanes", "--lanes-orchestrator", "a", "--lanes-orchestrator", "b",
                                        "--lanes-dry-run"])
    assert ns.lanes and ns.lanes_dry_run and ns.lanes_orchestrator == ["a", "b"] and not ns.no_lanes
    assert cli.build_parser().parse_args(["setup", "--no-lanes"]).no_lanes

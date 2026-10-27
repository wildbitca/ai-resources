"""The wizard's lane step (ADR-0007, plan P2-S6): disabled by default, explicit enable; no acknowledgement (ADR-0019)."""
from __future__ import annotations

import argparse

import pytest

from ai_resources.setup import lanes_render, profiles, state, ui, wizard

PROFILE = "subscription-claude-55"


def _args(**kw):
    ns = dict(lanes=False, no_lanes=False, allow_unverified=False)
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


def test_non_interactive_lanes_enables_and_asks_nothing():
    s = _s()
    assert wizard._step_lanes(s, _args(lanes=True)) == 0
    assert state.lanes_enabled(s)
    assert set(s.profile.lanes) == {"orchestrators"} and s.profile.lanes["orchestrators"] == []   # the role table is the matrix; the orchestrators are found in the OpenClaw config


def test_interactive_enable_asks_nothing_and_shows_no_acknowledgement(monkeypatch):
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    warned, confirmed = [], []
    monkeypatch.setattr(ui, "warn", warned.append)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: confirmed.append(a) or True)
    monkeypatch.setattr(ui, "text", lambda *a, **k: pytest.fail("the orchestrators are no longer asked"))
    s = _s()
    assert wizard._step_lanes(s, _args(lanes=True)) == 0
    assert state.lanes_enabled(s) and warned == [] and confirmed == []


def test_no_lanes_clears_the_table_and_the_file_follows_on_apply():
    s = _s()
    wizard._step_lanes(s, _args(lanes=True))
    lanes_render.sync_file(s)
    assert state.lanes_path().is_file()
    assert wizard._step_lanes(s, _args(no_lanes=True)) == 0
    assert s.profile.lanes is None
    wizard._apply_lanes_file(s, {"by_role": {}})
    assert not state.lanes_path().exists()
    assert str(state.lanes_path()) not in s.tracking.config_files_written


def test_a_dry_run_enables_nothing():
    s = _s()
    assert wizard._step_lanes(s, _args(lanes=True), dry_run=True) == 0
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
    assert wizard._step_lanes(s, _args(lanes=True)) == 2
    assert any("routing.implementer" in e for e in errors)
    assert s.profile.lanes is None


def test_cli_exposes_the_lane_flags():
    from ai_resources import cli
    ns = cli.build_parser().parse_args(["setup", "--lanes"])
    assert ns.lanes and not ns.no_lanes
    assert cli.build_parser().parse_args(["setup", "--no-lanes"]).no_lanes
    for gone in ("--lanes-orchestrator", "--lanes-dry-run"):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["setup", gone, "x"] if "orch" in gone else ["setup", gone])

"""Under the recommended run (`--auto`) the Antigravity lane is on by default and asks nothing (ADR-0018 waiver).
Offline; the profile is a fixture copy, `agy` on PATH is simulated."""
from __future__ import annotations

import argparse
import copy
import dataclasses

import pytest

from ai_resources import agy_lane_gates as gates_mod
from ai_resources.setup import lanes_render, profiles, state, ui, wizard

PROFILE = "subscription-claude-55"

pytestmark = pytest.mark.agy_waiver      # the shipped waiver applies (conftest neutralises it for every other test)


def _args(**kw):
    ns = dict(lanes=False, no_lanes=False, lanes_dry_run=False, lanes_orchestrator=["main"],
              allow_unverified=False, lanes_antigravity=False, no_lanes_antigravity=False)
    ns.update(kw)
    return argparse.Namespace(**ns)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    monkeypatch.setattr(ui, "is_auto", lambda: True)
    monkeypatch.setattr(wizard.shutil, "which", lambda name, *a, **k: "/usr/bin/agy" if name == "agy" else None)
    asked = []
    for fn in ("confirm", "text", "checkbox", "select"):
        monkeypatch.setattr(ui, fn, lambda *a, _fn=fn, **k: asked.append(_fn))
    monkeypatch.setattr(wizard, "_ASKED", asked, raising=False)
    real = profiles.load_profile

    def load(name, mode=None):
        doc = copy.deepcopy(real(name, mode))
        lanes = doc.get("lanes")
        if lanes:
            lanes["roles"]["tester"] = ["claude-cli:sonnet", "agy:claude-sonnet-5-5", "claude-code"]
            lanes["roles"]["explore"] = ["claude-cli:haiku", "claude-code"]
            lanes.pop("pins", None)
            for role in ("planner", "software-architect"):
                lanes["second"].pop(role, None)
            lanes["second"]["code-reviewer"] = ["agy:claude-sonnet-5-5", "claude-cli:sonnet"]
        return doc
    monkeypatch.setattr(profiles, "load_profile", load)
    return asked


def _s():
    s = state.SetupState()
    s.mode = "single-model"
    s.cli_team = True
    s.profile.name = PROFILE
    return s


def _agy_agents(s):
    return [a for a in (s.profile.lanes or {}).get("agents", {}).values() if a["backend"] != "claude-cli"]


def test_auto_turns_the_agy_lane_on_signs_the_waiver_ack_and_asks_nothing(_env):
    s = _s()
    assert wizard._step_lanes(s, _args()) == 0
    assert _agy_agents(s)
    assert s.profile.lanes["gates"] == {"antigravity": True}
    google = s.profile.lane_acks["google"]
    assert google["account"] == lanes_render.OPERATOR_WAIVER_ACCOUNT and google["roles"]
    assert lanes_render.valid_acks(s.profile.lane_acks).get("google")
    assert _env == []
    # no Anthropic acknowledgement on file: the Claude parallel runs are not switched on by this
    assert not any(a["backend"] == "claude-cli" for a in s.profile.lanes["agents"].values())


def test_auto_keeps_the_account_already_on_file_and_is_idempotent():
    s = _s()
    s.profile.lane_acks = {"google": lanes_render.make_ack("google", ["tester"], "2026-10-16", account="me@example.test")}
    assert wizard._step_lanes(s, _args()) == 0
    assert s.profile.lane_acks["google"]["account"] == "me@example.test"
    first = copy.deepcopy(state._to_dict(s))
    assert wizard._step_lanes(s, _args()) == 0
    assert state._to_dict(s)["profile"]["lanes"] == first["profile"]["lanes"]


def test_auto_keeps_claude_parallel_runs_that_have_an_anthropic_ack():
    s = _s()
    doc = profiles.load_profile(PROFILE)
    roles = sorted(set(doc["lanes"]["roles"]) | set(doc["lanes"]["second"]))
    s.profile.lane_acks = {"anthropic": lanes_render.make_ack("anthropic", roles, "2026-10-16")}
    s.profile.lanes = {"agents": {"kit-lane-cli-sonnet55": {"backend": "claude-cli",
                                                            "model": "anthropic/claude-sonnet-5-5",
                                                            "pool": "claude-subscription"}}}
    assert wizard._step_lanes(s, _args()) == 0
    backends = {a["backend"] for a in s.profile.lanes["agents"].values()}
    assert "claude-cli" in backends and len(backends) > 1


def test_opt_out_flag_and_missing_binary_and_no_auto_keep_it_off(monkeypatch):
    s = _s()
    assert wizard._step_lanes(s, _args(no_lanes_antigravity=True)) == 0
    assert not _agy_agents(s)
    monkeypatch.setattr(wizard.shutil, "which", lambda *a, **k: None)
    s = _s()
    assert wizard._step_lanes(s, _args()) == 0
    assert not _agy_agents(s) and s.profile.lanes is None
    monkeypatch.setattr(wizard.shutil, "which", lambda name, *a, **k: "/usr/bin/agy")
    monkeypatch.setattr(ui, "is_auto", lambda: False)
    s = _s()
    assert wizard._step_lanes(s, _args()) == 0
    assert s.profile.lanes is None


def test_the_waiver_is_recorded_in_the_adr():
    from pathlib import Path
    adr = Path(__file__).resolve().parent.parent / "docs" / "decisions" / "0018-routing-matrix-and-pool-balancing.md"
    text = adr.read_text(encoding="utf-8")
    assert "## Operator waiver" in text and "G1" in text and "G7" in text
    assert lanes_render.WAIVER_REF.endswith("#operator-waiver")
    assert gates_mod.WAIVED_BY_OPERATOR == frozenset(g.id for g in gates_mod.GATES)
    assert dataclasses.is_dataclass(gates_mod.GATES[0])

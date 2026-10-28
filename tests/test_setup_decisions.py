"""3.2.8 shared plumbing: persisted `decisions`, skipped-change collector, gate and verbosity switches."""
from __future__ import annotations

import pytest

from ai_resources.setup import state, ui


@pytest.fixture(autouse=True)
def _reset():
    ui.clear_skipped()
    ui.bind_decisions(None)
    ui.set_accept_gates(False)
    ui.set_verbose(False)
    yield
    ui.clear_skipped()
    ui.bind_decisions(None)
    ui.set_accept_gates(False)
    ui.set_verbose(False)


def test_remember_writes_through_to_bound_state():
    o = state.OpenClawState()
    ui.bind_decisions(o.decisions)
    assert ui.remembered("route:slack:main") is None
    ui.remember("route:slack:main", "routed")
    assert ui.remembered("route:slack:main") == "routed"
    assert o.decisions == {"route:slack:main": "routed"}


def test_decisions_round_trip_and_stay_absent_while_empty():
    s = state.SetupState()
    assert "decisions" not in state._to_dict(s)["openclaw"]
    assert "config_patch_pending" not in state._to_dict(s)["openclaw"]
    s.openclaw.decisions["k"] = "v"
    s.openclaw.config_patch_pending.append({"patch": {"a": 1}, "source": "t", "at": "now"})
    doc = state._to_dict(s)
    back = state._from_dict(state.SetupState, doc)
    assert back.openclaw.decisions == {"k": "v"}
    assert back.openclaw.config_patch_pending[0]["patch"] == {"a": 1}


def test_old_state_without_the_fields_loads():
    back = state._from_dict(state.SetupState, {"openclaw": {"engine": "keep"}})
    assert back.openclaw.decisions == {} and back.openclaw.config_patch_pending == []


def test_skipped_summary_lists_what_why_and_how(capsys):
    assert ui.print_skipped_summary() is False
    ui.record_skipped("install units", "operator-owned", "ai-resources setup --accept-gates")
    ui.record_skipped("install units", "operator-owned", "ai-resources setup --accept-gates")   # deduplicated
    assert len(ui.skipped()) == 1
    assert ui.print_skipped_summary() is True
    out = capsys.readouterr().out
    assert "install units" in out and "operator-owned" in out and "--accept-gates" in out


def test_switches():
    assert not ui.accept_gates() and not ui.is_verbose()
    ui.set_accept_gates(); ui.set_verbose()
    assert ui.accept_gates() and ui.is_verbose()

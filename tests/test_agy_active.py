"""agy has the same standing as Claude Code (ADR-0019 O1): it runs whenever the matrix assigns it and it is installed and
signed in. No flag, acknowledgement, gate or evidence record exists; an inactive cell is skipped with a recorded cause."""
from __future__ import annotations

import argparse
import importlib

import pytest

from ai_resources import route_cmd
from ai_resources.scoring import routing
from ai_resources.setup import detection, live_auth, state, ui, wizard


def _args(**kw):
    ns = dict(lanes=False, no_lanes=False, lanes_dry_run=False, lanes_orchestrator=[], allow_unverified=False)
    ns.update(kw)
    return argparse.Namespace(**ns)


def test_the_removed_gate_machinery_is_gone():
    for mod in ("agy_lane_gates", "agy_gate_evidence"):
        with pytest.raises(ImportError):
            importlib.import_module(f"ai_resources.{mod}")
    from ai_resources.setup import lanes_render
    for name in ("ACK_TEXT", "make_ack", "valid_acks", "agy_role_reason", "agy_admission", "agy_admitted", "acked"):
        assert not hasattr(lanes_render, name), name
    assert not hasattr(wizard, "agy_terms_check")


def test_the_antigravity_flags_are_gone_from_the_cli():
    from ai_resources import cli
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["setup", "--lanes-antigravity"])
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["setup", "--no-lanes-antigravity"])


def test_agy_is_active_only_when_installed_and_signed_in(monkeypatch, tmp_path):
    home = tmp_path / ".gemini"
    home.mkdir()
    monkeypatch.setattr(live_auth, "agy_home", lambda: home)
    monkeypatch.setattr(detection, "agy_installed", lambda: True)
    assert detection.agy_active() is False                       # installed, no account state
    (home / "google_accounts.json").write_text("{}", encoding="utf-8")
    assert detection.agy_active() is True
    monkeypatch.setattr(detection, "agy_installed", lambda: False)
    assert detection.agy_active() is False


def test_codex_is_an_optional_pool_active_only_when_detected_and_authed(monkeypatch, tmp_path):
    monkeypatch.setattr(detection.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(detection, "_which_extra", lambda b: "/x/codex" if b == "codex" else "")
    assert detection.codex_active() is False
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    assert detection.codex_active() is True
    monkeypatch.setattr(detection, "_which_extra", lambda b: "")
    assert detection.codex_active() is False


def test_codex_is_valid_in_the_cell_grammar_has_a_pool_and_sits_in_no_default_cell():
    assert routing.validate_cell("codex:gpt-5") is None and routing.pool_of("codex:gpt-5") == routing.POOL_CODEX
    assert routing.POOL_CODEX in routing.POOLS
    assert not [r.name for r in routing.ROLES.values() if any(c.startswith("codex") for c in r.chain)]


def test_inactive_backends_name_the_cause(monkeypatch):
    monkeypatch.setattr(detection, "agy_active", lambda: False)
    monkeypatch.setattr(detection, "codex_active", lambda: False)
    assert detection.inactive_backends() == {"agy": "agy-missing", "codex": "codex-missing"}
    monkeypatch.setattr(detection, "agy_active", lambda: True)
    assert detection.inactive_backends() == {"codex": "codex-missing"}


@pytest.mark.parametrize("active,cell,cause", [(True, "agy:gemini-3.8-flash-high", None),
                                               (False, "claude-code:haiku", "agy-missing")])
def test_route_skips_an_agy_primary_with_a_recorded_cause_when_agy_is_inactive(monkeypatch, capsys, active, cell, cause):
    monkeypatch.setattr(detection, "agy_active", lambda: active)
    monkeypatch.setattr(detection, "codex_active", lambda: False)
    ns = argparse.Namespace(role="explore", usage_file=None, writers="", unavailable=[], json=True)
    monkeypatch.setattr(route_cmd.pool_usage, "usage_map", lambda snap: {})
    monkeypatch.setattr(route_cmd.pool_usage, "snapshot", lambda: None)
    assert route_cmd.cmd_route(ns) == 0
    import json
    out = json.loads(capsys.readouterr().out)
    assert out["cell"] == cell and out["cause"] == cause


def test_ticking_agy_enables_the_routing_record_without_any_acknowledgement(monkeypatch):
    warned = []
    monkeypatch.setattr(ui, "warn", warned.append)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", lambda *a, **k: pytest.fail("no confirmation expected"))
    s = state.SetupState()
    s.mode, s.cli_team, s.profile.name = "single-model", True, "subscription-claude-55"
    s.profile.customizations = {"__targets__": {"selected": ["claude", "agy"]}}
    s._tool_members_this_run = ["claude", "agy"]
    assert wizard._step_lanes(s, _args()) == 0
    assert state.lanes_enabled(s) and s.profile.lanes == {"orchestrators": []} and warned == []


def test_doctor_reports_one_agy_line_with_the_cause_when_inactive(monkeypatch):
    from ai_resources import doctor
    seen = []
    monkeypatch.setattr(ui, "info", seen.append)
    monkeypatch.setattr(detection, "agy_active", lambda: False)
    assert doctor._check_antigravity_lane(state.SetupState()) == 0
    assert len(seen) == 1 and "agy-missing" in seen[0]
    monkeypatch.setattr(detection, "agy_active", lambda: True)
    monkeypatch.setattr(doctor, "_check_antigravity_quota", lambda s: 7)
    seen.clear()
    assert doctor._check_antigravity_lane(state.SetupState()) == 7 and "installed and signed in" in seen[0]

"""ADR-0014 D5: the utilityModel probe is opt-in, billed (so priced first), local-transport only, never run by tests,
and a replacement is proposed only when a probe VERIFIED it."""
from __future__ import annotations

import json

import pytest

from ai_resources import openclaw_cost_migrations as cm
from ai_resources import openclaw_utility_probe as up
from test_openclaw_host_wizard import _run_wizard, _state, script, sim  # noqa: F401  (fixtures)


def test_the_command_uses_the_local_transport_not_the_gateway():
    argv = up.command("anthropic/claude-haiku-5-5")
    assert argv[:3] == ["infer", "model", "run"] and "--local" in argv and "--gateway" not in argv
    assert argv[argv.index("--model") + 1] == "anthropic/claude-haiku-5-5" and up.PROMPT in argv


@pytest.mark.parametrize("rc,out,status", [
    (0, '{"text": "OK"}', "ok"),
    (1, "HTTP 400 invalid_request_error: model: claude-haiku-4-5", "invalid"),
    (1, "401 Unauthorized", "invalid"),
    (1, "model_not_found", "invalid"),
    (0, '{"text": "Sure! Here you go"}', "error"),
    (1, "connection reset", "error"),
    (124, "", "error"),
])
def test_classification(rc, out, status):
    assert up.classify("m", rc, out).status == status


def test_an_invalid_answer_carries_the_workspace_message():
    assert up.classify("m", 1, "400").detail == "key not valid for this model, create the key inside the workspace"


def test_a_runner_that_raises_is_an_error_not_a_crash():
    def boom(argv):
        raise RuntimeError("no")
    assert up.probe("m", boom).status == "error"


def test_first_verified_skips_the_current_model_and_stops_at_the_first_ok():
    seen = []

    def run(argv):
        ref = argv[argv.index("--model") + 1]
        seen.append(ref)
        return (0, "OK") if ref == "b" else (1, "400")
    got = up.first_verified(["cur", "a", "b", "c"], run, skip="cur")
    assert got.ref == "b" and seen == ["a", "b"]
    assert up.first_verified(["a"], lambda argv: (1, "400")) is None


def test_the_candidates_are_the_pinned_models():
    from ai_resources import model_pins
    pins = model_pins.effective()
    assert up.candidates() == [f"anthropic/{pins['haiku']}", f"anthropic/{pins['sonnet']}"]


def test_the_proposal_needs_a_verified_ref_and_a_configured_different_model():
    doc = {"agents": {"defaults": {"utilityModel": "anthropic/claude-haiku-4-5"}}}
    assert "cost-utility-model" not in {p.id for p in cm.proposals(doc)}                    # never a guessed id
    (p,) = [p for p in cm.proposals(doc, utility_model="anthropic/claude-haiku-5-5") if p.id == "cost-utility-model"]
    assert (p.path, p.old, p.new) == (("agents", "defaults", "utilityModel"), "anthropic/claude-haiku-4-5",
                                      "anthropic/claude-haiku-5-5")
    assert "cost-utility-model" not in {q.id for q in cm.proposals({}, utility_model="x")}  # unset: the profile fills it
    same = {"agents": {"defaults": {"utilityModel": "x"}}}
    assert "cost-utility-model" not in {q.id for q in cm.proposals(same, utility_model="x")}


# --- through the wizard (a fake `openclaw infer`) -----------------------------------------------------------------------

def _set_utility(sim, ref):
    doc = json.loads(sim.cfg.read_text(encoding="utf-8"))
    doc["agents"]["defaults"]["utilityModel"] = ref
    sim.cfg.write_text(json.dumps(doc), encoding="utf-8")
    sim.overrides_file.unlink(missing_ok=True)


def _infer(sim, ok_refs):
    real = sim.oc.__call__
    seen = []

    def oc(args, stdin=None, timeout=120):
        if args[:3] == ["infer", "model", "run"]:
            ref = args[args.index("--model") + 1]
            seen.append(ref)
            return (0, '{"text": "OK"}') if ref in ok_refs else (1, "400 invalid_request_error")
        return real(args, stdin, timeout)
    return oc, seen


def test_the_probe_is_never_asked_when_the_utility_model_is_unset(sim, script):
    _run_wizard(_state())
    assert not [q for q in script.asked if q.startswith("Check that the utility model")]


def test_default_no_runs_no_probe_and_proposes_nothing(sim, script, monkeypatch):
    from ai_resources.setup.cockpits import openclaw
    _set_utility(sim, "anthropic/claude-haiku-4-5")
    oc, seen = _infer(sim, set())
    monkeypatch.setattr(openclaw, "_openclaw", oc)
    script.answers["Replace "] = False
    _run_wizard(_state())
    assert any(q.startswith("Check that the utility model") for q in script.asked) and seen == []
    assert not [q for q in script.asked if q.startswith("Replace agents.defaults.utilityModel")]


def test_a_failing_model_and_a_verified_pin_propose_a_replacement_and_apply_only_on_confirm(sim, script, monkeypatch):
    from ai_resources.setup.cockpits import openclaw
    _set_utility(sim, "anthropic/claude-haiku-4-5")
    pins = up.candidates()
    oc, seen = _infer(sim, {pins[0]})
    monkeypatch.setattr(openclaw, "_openclaw", oc)
    script.answers["Check that the utility model"] = True
    script.answers["Replace "] = False
    s = _state()
    _run_wizard(s)
    assert seen == ["anthropic/claude-haiku-4-5", pins[0]]
    assert any("key not valid for this model, create the key inside the workspace" in m for m in script.messages("warn"))
    assert json.loads(sim.cfg.read_text())["agents"]["defaults"]["utilityModel"] == "anthropic/claude-haiku-4-5"
    assert any(q.startswith("Replace agents.defaults.utilityModel") for q in script.asked)

    script.answers["Replace agents.defaults.utilityModel"] = True
    script.asked.clear()
    _run_wizard(s)
    assert json.loads(sim.cfg.read_text())["agents"]["defaults"]["utilityModel"] == pins[0]
    assert "cost-utility-model" in s.openclaw.cost_migrations_applied


def test_no_pin_that_answers_means_no_proposal(sim, script, monkeypatch):
    from ai_resources.setup.cockpits import openclaw
    _set_utility(sim, "anthropic/claude-haiku-4-5")
    oc, seen = _infer(sim, set())
    monkeypatch.setattr(openclaw, "_openclaw", oc)
    script.answers["Check that the utility model"] = True
    script.answers["Replace "] = False
    _run_wizard(_state())
    assert len(seen) == 3                                   # the current model and both pins
    assert not [q for q in script.asked if q.startswith("Replace agents.defaults.utilityModel")]


def test_a_working_model_proposes_nothing(sim, script, monkeypatch):
    from ai_resources.setup.cockpits import openclaw
    _set_utility(sim, "anthropic/claude-haiku-5-5")
    oc, seen = _infer(sim, {"anthropic/claude-haiku-5-5"})
    monkeypatch.setattr(openclaw, "_openclaw", oc)
    script.answers["Check that the utility model"] = True
    script.answers["Replace "] = False
    _run_wizard(_state())
    assert seen == ["anthropic/claude-haiku-5-5"]
    assert not [q for q in script.asked if q.startswith("Replace agents.defaults.utilityModel")]


def test_non_interactive_and_dry_runs_never_probe(sim, script, monkeypatch):
    from ai_resources.setup.cockpits import openclaw
    _set_utility(sim, "anthropic/claude-haiku-4-5")
    oc, seen = _infer(sim, set())
    monkeypatch.setattr(openclaw, "_openclaw", oc)
    script.non_interactive = True
    _run_wizard(_state())
    script.non_interactive = False
    _run_wizard(_state(), dry_run=True)
    assert seen == [] and not [q for q in script.asked if q.startswith("Check that the utility model")]


# --- doctor and snapshot probes --------------------------------------------------------------------------------------

def test_doctor_prints_the_announcement_note_for_an_openclaw_host_and_nothing_probes_by_default(monkeypatch):
    import argparse
    from ai_resources import doctor
    from ai_resources.setup import state, ui
    info = []
    monkeypatch.setattr(ui, "info", info.append)
    s = state.SetupState()
    s.cockpits["openclaw"] = state.CockpitState(installed=True)
    assert doctor._check_openclaw_cost(s, argparse.Namespace(probe_utility_model=False)) == 0
    assert info == [doctor.ANNOUNCE_INFO] and "announceTimeoutMs" in doctor.ANNOUNCE_INFO and "2026-10-22" in doctor.ANNOUNCE_INFO
    info.clear()
    assert doctor._check_openclaw_cost(state.SetupState(), argparse.Namespace(probe_utility_model=True)) == 0
    assert info == []                                           # no OpenClaw, no note, no probe


def test_doctor_probe_flag_prints_the_cost_first_and_reports_a_400(monkeypatch, tmp_path):
    import argparse
    from ai_resources import doctor, openclaw_host
    from ai_resources.setup import state, ui
    from ai_resources.setup.cockpits import openclaw as cockpit
    cfg = tmp_path / "openclaw.json"
    cfg.write_text(json.dumps({"agents": {"defaults": {"utilityModel": "anthropic/claude-haiku-4-5"}}}))
    monkeypatch.setattr(cockpit, "config_path", lambda: cfg)
    order, warns = [], []
    monkeypatch.setattr(ui, "info", lambda m: order.append(("info", m)))
    monkeypatch.setattr(ui, "warn", warns.append)
    monkeypatch.setattr(openclaw_host, "default_runner", lambda argv, **k: order.append(("run", argv)) or (1, "HTTP 400"))
    s = state.SetupState()
    s.openclaw.host = True
    doctor._check_openclaw_cost(s, argparse.Namespace(probe_utility_model=True))
    kinds = [k for k, _m in order]
    assert kinds.index("run") > max(i for i, (k, m) in enumerate(order) if k == "info" and "billed" in str(m))
    assert order[kinds.index("run")][1][:4] == ["openclaw", "infer", "model", "run"] and "--local" in order[kinds.index("run")][1]
    assert any("key not valid for this model" in w for w in warns)


def test_the_snapshot_probe_includes_the_utility_model_only_when_configured(tmp_path):
    from ai_resources import audit_snapshot
    said, calls = [], []

    def run(cmd):
        calls.append(cmd[:4])
        return json.dumps({"usage": {"input_tokens": 1}, "text": "OK"})
    home = tmp_path / "h"
    (home / ".openclaw").mkdir(parents=True)
    out = audit_snapshot.run_probes("x", run=run, say=said.append, home=home)
    assert out["utility_model"] == {"status": "unset"} and len(calls) == 2
    (home / ".openclaw" / "openclaw.json").write_text(json.dumps({"agents": {"defaults": {"utilityModel": "anthropic/m"}}}))
    calls.clear()
    out = audit_snapshot.run_probes("x", run=run, say=said.append, home=home)
    assert out["utility_model"]["ref"] == "anthropic/m" and out["utility_model"]["status"] == "ok"
    assert calls[-1] == ["openclaw", "infer", "model", "run"] and "REAL model calls" in said[0]

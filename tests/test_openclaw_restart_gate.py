"""Setup never makes a live gateway restart (ADR-0003): the restart-required gate.

Driven through the wizard's own entry points on the simulated host of test_openclaw_host_wizard
(a fake `openclaw`, a recording systemctl, HOME and every path under tmp_path). The simulated
operator has `force: ["*"]` in the overrides file, so the profile wants `gateway.bind: tailnet`
over the fixture's `lan`, which is the key that restarted the live gateway on 2026-10-09.
"""
from __future__ import annotations

import json
import sys

import pytest

from test_openclaw_host_wizard import (  # noqa: F401  (fixtures are used by name)
    ANSWERS, GATES, _configure, _run_wizard, _state, script, sim,
)
from ai_resources import openclaw_host as host
from ai_resources import openclaw_reload_rules as rr
from ai_resources.setup import state
from ai_resources.setup.cockpits import _openclaw_host as section, openclaw

BIND = {"gateway": {"bind": "tailnet"}}


@pytest.fixture
def events(sim, monkeypatch):
    """One ordered log over the fake openclaw and the fake systemctl, plus the watchdog marker state."""
    log: list[tuple] = []
    real_oc, real_sd = sim.oc, sim.systemd

    def oc(args, stdin=None, timeout=120):
        patch = json.loads(stdin) if stdin else None
        if args[:2] == ["config", "patch"]:
            kind = "dry" if "--dry-run" in args else "patch"
            log.append((kind, tuple(rr.leaf_paths(patch))))
        return real_oc(args, stdin=stdin, timeout=timeout)

    def sd(argv, **kw):
        if argv[:2] == ["systemctl", "--user"] and argv[2] in ("stop", "start") and host.GATEWAY_UNIT in argv:
            log.append((argv[2], host.WATCHDOG_OFF.exists()))
        elif argv[:2] == ["systemctl", "--user"] and argv[2] == "show" and "TasksCurrent" in argv:
            log.append(("drain",))
        elif len(argv) == 2 and argv[1] == "health":
            log.append(("health",))
        return real_sd(argv, **kw)

    monkeypatch.setattr(openclaw, "_openclaw", oc)
    monkeypatch.setattr(section, "_runner", lambda: sd)
    return log


def bind_written(sim) -> bool:
    return any("gateway" in (p or {}) and "bind" in p["gateway"] for _a, p in sim.oc.real_patches())


def stops(log) -> list:
    return [e for e in log if e[0] == "stop"]


def setup_s():
    s = _state()
    return s


def pending_paths(s) -> list[str]:
    return sorted(e["path"] for e in s.openclaw.host_restart_pending)


def idle(monkeypatch, n=0):
    """Runs in flight (None: the probe could not tell). Stubs the section's single probe seam."""
    monkeypatch.setattr(section, "_in_flight",
                        lambda: (n, "unknown (treated as busy)" if n is None else str(n)))


# --- (a) AC-1: unattended ---------------------------------------------------------------------------------

def test_unattended_never_writes_a_restart_required_key_and_records_it_pending(sim, script, events):
    s = _state()
    openclaw.prompt(s)
    script.non_interactive = True
    _configure(s)
    assert not bind_written(sim)
    assert sim.oc.real_patches() == []                                 # unattended applies nothing at all
    assert any("not applied" in m and "unattended" in m for m in script.messages("warn"))
    assert "gateway.bind" in pending_paths(s)
    assert stops(events) == []


# --- (b) (c) AC-1b: busy or unknown ---------------------------------------------------------------------------

def test_interactive_with_runs_in_flight_shows_the_count_and_defers(sim, script, events, monkeypatch):
    idle(monkeypatch, 3)
    s = _state()
    _run_wizard(s)
    assert not bind_written(sim) and stops(events) == []
    shown = " | ".join(m for _l, m in script.log)
    assert "agent runs in flight: 3" in shown and "3 agent run(s) in flight" in shown
    assert "gateway.bind" in pending_paths(s)
    assert not any("Apply in a drained window" in q for q in script.asked), "no prompt while busy"
    assert sim.cfg.exists() and json.loads(sim.cfg.read_text())["gateway"]["bind"] == "lan"


def test_interactive_with_a_probe_error_counts_as_busy(sim, script, events, monkeypatch):
    idle(monkeypatch, None)
    s = _state()
    _run_wizard(s)
    assert not bind_written(sim) and stops(events) == []
    assert any("agent runs in flight: unknown (treated as busy)" in m for _l, m in script.log)
    assert "gateway.bind" in pending_paths(s)


# --- (d) declined ---------------------------------------------------------------------------------------------

def test_idle_but_declined_writes_nothing_and_records_pending(sim, script, events, monkeypatch):
    script.answers["Apply in a drained window"] = False
    s = _state()
    _run_wizard(s)
    assert not bind_written(sim) and stops(events) == []
    assert any("Apply in a drained window" in q for q in script.asked)
    assert "gateway.bind" in pending_paths(s)
    # The hot half was still applied: only the restart key waits.
    assert any(p and "tools" in p for _a, p in sim.oc.real_patches())


# --- (e) AC-1c: the order ---------------------------------------------------------------------------------------

def test_confirmed_apply_runs_dry_run_marker_stop_drain_patch_marker_start_health(sim, script, events):
    s = _state()
    _run_wizard(s)
    seq = [e for e in events if e[0] in ("dry", "patch", "stop", "start", "drain", "health")]
    bind_dry = ("dry", ("gateway.bind",))
    bind_patch = ("patch", ("gateway.bind",))
    assert bind_dry in seq and bind_patch in seq
    i = seq.index(bind_dry)
    window = seq[i:]
    kinds = [w[0] for w in window]
    assert kinds[:4] == ["dry", "stop", "drain", "patch"], kinds
    assert window[1] == ("stop", True), "watchdog.off is held when the gateway is stopped"
    assert window[3] == bind_patch
    assert [k for k in kinds[4:] if k != "drain"] == ["start", "health"]
    start = window[kinds.index("start")]
    assert start == ("start", False), "the marker is removed before the gateway starts again"
    assert not host.WATCHDOG_OFF.exists()
    assert json.loads(sim.cfg.read_text())["gateway"]["bind"] == "tailnet"
    assert s.openclaw.host_restart_pending == []
    assert any(c["path"] == ["gateway", "bind"] for c in s.openclaw.host_config_changes)


# --- (f) a rejected dry run stops nothing ------------------------------------------------------------------------

def test_a_rejected_restart_dry_run_stops_nothing(sim, script, events, monkeypatch):
    real = openclaw._openclaw

    def reject_bind_only(args, stdin=None, timeout=120):
        if "--dry-run" in args and stdin and json.loads(stdin) == BIND:
            return 1, "invalid: schema"
        return real(args, stdin=stdin, timeout=timeout)

    monkeypatch.setattr(openclaw, "_openclaw", reject_bind_only)
    s = _state()
    _run_wizard(s)
    assert stops(events) == [] and not bind_written(sim)
    assert any("rejected the restart-required patch" in m for m in script.messages("error"))
    assert "gateway.bind" in pending_paths(s)


# --- (g) the writer's own gate -------------------------------------------------------------------------------------

def test_apply_patch_raises_before_any_cli_call_for_a_restart_key(monkeypatch):
    calls = []
    monkeypatch.setattr(openclaw, "_openclaw", lambda *a, **k: calls.append(a) or (0, "ok"))
    with pytest.raises(rr.RestartRequired) as e:
        openclaw.apply_patch(BIND)
    assert e.value.paths == ["gateway.bind"] and calls == []
    ok, _ = openclaw.apply_patch(BIND, dry_run=True)          # a dry run is never gated
    assert ok and len(calls) == 1
    ok, _ = openclaw.apply_patch(BIND, allow_restart=True)    # the drained window's own call
    assert ok and calls[-1][0][:2] == ["config", "patch"]


def test_reload_mode_off_makes_every_key_restart_required(monkeypatch):
    monkeypatch.setattr(openclaw, "_openclaw", lambda *a, **k: (0, "ok"))
    with pytest.raises(rr.RestartRequired):
        openclaw.apply_patch({"tools": {"profile": "coding"}}, reload_mode="off")


# --- (h) AC-5 end to end ---------------------------------------------------------------------------------------------

def test_a_version_other_than_the_pin_gates_even_a_hot_only_patch(monkeypatch):
    monkeypatch.setattr(rr, "installed_openclaw_version", lambda runner, **kw: "2026.12.1")
    calls = []
    monkeypatch.setattr(openclaw, "_openclaw", lambda *a, **k: calls.append(a) or (0, "ok"))
    with pytest.raises(rr.RestartRequired):
        openclaw.apply_patch({"tools": {"profile": "coding"}})
    assert calls == []


def test_a_version_mismatch_defers_the_whole_host_patch_in_the_wizard(sim, script, events, monkeypatch):
    monkeypatch.setattr(rr, "installed_openclaw_version", lambda runner, **kw: None)
    script.answers["Apply in a drained window"] = False
    s = _state()
    _run_wizard(s)
    assert sim.oc.real_patches() == []
    assert "tools.profile" in pending_paths(s) and "gateway.bind" in pending_paths(s)


def test_the_engine_writer_reports_a_refusal_instead_of_crashing(monkeypatch, script):
    monkeypatch.setattr(rr, "installed_openclaw_version", lambda runner, **kw: "0.0.1")
    monkeypatch.setattr(openclaw, "_openclaw", lambda *a, **k: (0, "ok"))
    ok, msg = openclaw._apply_hot({"tools": {"media": {}}})
    assert ok is False and "not applied" in msg
    assert any("not applied" in m for m in script.messages("warn"))


# --- (i) a hot-only patch is exactly as before ------------------------------------------------------------------------

def test_a_host_that_already_has_the_bind_gets_one_hot_write_and_no_window(sim, script, events):
    doc = json.loads(sim.cfg.read_text())
    doc["gateway"]["bind"] = "tailnet"
    sim.cfg.write_text(json.dumps(doc))
    s = _state()
    _run_wizard(s)
    patches = [p for _a, p in sim.oc.real_patches()]
    assert len(patches) == 1 and "gateway" in patches[0] and "bind" not in patches[0]["gateway"]
    assert stops(events) == [] and s.openclaw.host_restart_pending == []
    assert not any("Apply in a drained window" in q for q in script.asked)


# --- (j) old state loads ------------------------------------------------------------------------------------------------

def test_an_old_state_without_the_new_fields_loads():
    s = state._from_dict(state.SetupState, {"openclaw": {"engine": "keep", "host": True}})
    assert s.openclaw.host_restart_pending == []


# --- (k) teardown --------------------------------------------------------------------------------------------------------

def test_teardown_restores_hot_keys_and_leaves_bind_pending_when_unattended(sim, script, events):
    s = _state()
    _run_wizard(s)
    assert json.loads(sim.cfg.read_text())["gateway"]["bind"] == "tailnet"
    script.non_interactive = True
    sim.oc.calls.clear()
    events.clear()
    openclaw.teardown(s)
    doc = json.loads(sim.cfg.read_text())
    assert doc["tools"]["profile"] == "minimal", "the hot half is restored"
    assert doc["gateway"]["bind"] == "tailnet", "the restart-required half is not written unattended"
    assert not bind_written(sim) and stops(events) == []
    [entry] = [e for e in s.openclaw.host_restart_pending if e["path"] == "gateway.bind"]
    assert entry["op"] == "restore" and entry["change"]["previous"] == "lan" and entry["source"] == "teardown"
    assert s.openclaw.host_config_changes == []


def test_teardown_interactive_and_idle_restores_bind_in_a_drained_window(sim, script, events):
    s = _state()
    _run_wizard(s)
    events.clear()
    openclaw.teardown(s)
    assert json.loads(sim.cfg.read_text())["gateway"]["bind"] == "lan"
    assert len(stops(events)) == 1 and s.openclaw.host_restart_pending == []


# --- (l) every other writer carries hot keys only ------------------------------------------------------------------------

V = rr.PINNED_OPENCLAW_VERSION


def _all_hot(patch):
    return rr.restart_paths(patch, openclaw_version=V) == []


@pytest.mark.parametrize("engine", ["claude-code", "codex", "gemini-cli"])
def test_the_engine_patch_is_all_hot(engine):
    patch = openclaw.build_patch(openclaw.ENGINES[engine], "anthropic/claude-sonnet-5", {}, "/kit/skills", "/bin/engram")
    assert _all_hot(patch), rr.restart_paths(patch, openclaw_version=V)


def test_the_antigravity_patch_and_its_restore_fragment_are_all_hot():
    patch = openclaw.build_patch(openclaw.ENGINES["antigravity"], "gemini-3.8-flash-low", {}, "/kit/skills",
                                 "/bin/engram", worker_model="claude-sonnet-5", ak_path="/kit",
                                 agy_bin="/bin/agy", claude_bin="/bin/claude", python3_bin="/bin/python3")
    assert _all_hot(patch)
    frag, _rp = openclaw._antigravity_restore_fragment({"default_allow_agents": ["a"], "agent_entries": {}})
    assert _all_hot(frag)


def test_the_media_and_bindings_patches_are_all_hot():
    media = openclaw.voice.build_media("agy", "python3", "/kit/voice.py", "auto", None, "llm")
    assert _all_hot({"tools": {"media": media}})
    new, _notes = host.build_bindings([], {"app": "-5200000001"})
    assert _all_hot({"bindings": new})


def test_the_host_profile_is_all_hot_except_the_bind():
    built = host.build_host_patch(host.load_host_profile(), {}, {"DOMAIN": "ai.example.org", "POD_CIDR": "10.9.0.0/24",
                                                                  "HOME": "/home/u", "TAILNET": "1"})
    assert rr.restart_paths(built["patch"], openclaw_version=V) == ["gateway.bind"]


# --- (m) apply-pending --------------------------------------------------------------------------------------------------------

def _pend_unattended(sim, script):
    s = _state()
    openclaw.prompt(s)
    script.non_interactive = True
    _configure(s)
    script.non_interactive = False
    return s


def test_apply_pending_refuses_while_busy_and_keeps_the_entry(sim, script, events, monkeypatch):
    s = _pend_unattended(sim, script)
    idle(monkeypatch, 2)
    assert section.apply_pending(s, assume_yes=True) == 1
    assert stops(events) == [] and "gateway.bind" in pending_paths(s)


def test_apply_pending_applies_in_a_window_and_clears_when_idle(sim, script, events):
    s = _pend_unattended(sim, script)
    events.clear()
    assert section.apply_pending(s, assume_yes=True) == 0
    assert json.loads(sim.cfg.read_text())["gateway"]["bind"] == "tailnet"
    assert s.openclaw.host_restart_pending == []
    assert [e[0] for e in events if e[0] in ("stop", "start")] == ["stop", "start"]
    assert any(c["path"] == ["gateway", "bind"] for c in s.openclaw.host_config_changes)


def test_apply_pending_without_yes_asks_and_declining_changes_nothing(sim, script, events):
    s = _pend_unattended(sim, script)
    script.answers["Apply in a drained window"] = False
    events.clear()
    assert section.apply_pending(s) == 1
    assert stops(events) == [] and "gateway.bind" in pending_paths(s)


def test_apply_pending_drops_an_entry_the_operator_already_satisfied(sim, script, events):
    s = _pend_unattended(sim, script)
    doc = json.loads(sim.cfg.read_text())
    doc["gateway"]["bind"] = "tailnet"
    sim.cfg.write_text(json.dumps(doc))
    assert section.apply_pending(s, assume_yes=True) == 0
    assert s.openclaw.host_restart_pending == [] and stops(events) == []


def test_nothing_pending_is_a_clean_no_op(sim, script):
    assert section.apply_pending(_state()) == 0


# --- Q5: the bind an older setup overwrote ------------------------------------------------------------------------------------

def _legacy_state(sim):
    """A host where setup 2.0.0 replaced `loopback` with `tailnet` (a record without `action`)."""
    doc = json.loads(sim.cfg.read_text())
    doc["gateway"]["bind"] = "tailnet"
    sim.cfg.write_text(json.dumps(doc))
    s = _state()
    s.openclaw.host_config_changes = [{"path": ["gateway", "bind"], "previous": "loopback", "had": True}]
    return s


def test_an_overwritten_bind_is_offered_back_and_restored_in_a_window_when_confirmed(sim, script, events):
    script.answers["Restore gateway.bind to"] = True
    s = _legacy_state(sim)
    _run_wizard(s)
    assert json.loads(sim.cfg.read_text())["gateway"]["bind"] == "loopback"
    assert len(stops(events)) == 1
    assert not any(c["path"] == ["gateway", "bind"] for c in s.openclaw.host_config_changes)


def test_the_offer_defaults_to_no_and_declining_changes_nothing(sim, script, events, monkeypatch):
    from ai_resources.setup import ui
    seen = {}
    real = ui.confirm

    def confirm(message, default=True):
        if message.startswith("Restore gateway.bind"):
            seen["default"] = default
        return real(message, default)

    script.answers["Restore gateway.bind to"] = False
    s = _legacy_state(sim)
    monkeypatch.setattr(ui, "confirm", confirm)
    _run_wizard(s)
    assert seen["default"] is False
    assert json.loads(sim.cfg.read_text())["gateway"]["bind"] == "tailnet" and stops(events) == []


def test_the_offer_is_not_made_unattended_or_while_busy(sim, script, events, monkeypatch):
    s = _legacy_state(sim)
    openclaw.prompt(s)
    script.non_interactive = True
    _configure(s)
    assert stops(events) == [] and not any("Restore gateway.bind" in q for q in script.asked)
    script.non_interactive = False
    idle(monkeypatch, 4)
    _configure(s)
    assert stops(events) == [] and not any("Restore gateway.bind" in q for q in script.asked)


def test_a_bind_the_operator_changed_since_is_not_offered(sim, script, events):
    s = _legacy_state(sim)
    doc = json.loads(sim.cfg.read_text())
    doc["gateway"]["bind"] = "lan"          # neither the recorded previous nor the profile value
    sim.cfg.write_text(json.dumps(doc))
    _run_wizard(s)
    assert not any("Restore gateway.bind" in q for q in script.asked)


# --- the kept key must not restart anything on a customised host ---------------------------------------------------------------

def test_without_overrides_a_customised_bind_is_kept_and_nothing_is_pending_or_restarted(sim, script, events):
    sim.overrides_file.unlink()
    s = _state()
    _run_wizard(s)
    assert stops(events) == [] and s.openclaw.host_restart_pending == []
    assert json.loads(sim.cfg.read_text())["gateway"]["bind"] == "lan"


# --- (n) apply-pending with a malformed overrides file ----------------------------------------------------------------------

@pytest.mark.parametrize("content, problem", [
    ("{ keep: [", "not valid JSON5"),
    ('["gateway.bind"]', "expected an object"),
    ('{"keep": "gateway.bind"}', "must be a list"),
    ('{"force": ["gateway bind!"]}', "invalid path"),
    ('{"force": ["channels.telegram.botToken"]}', "credential path"),
])
def test_apply_pending_with_a_bad_overrides_file_errors_cleanly_and_applies_nothing(
        sim, script, events, content, problem):
    s = _pend_unattended(sim, script)
    sim.overrides_file.write_text(content, encoding="utf-8")
    events.clear()
    assert section.apply_pending(s, assume_yes=True) == 1          # no ValueError, no traceback
    errors = script.messages("error")
    assert len(errors) == 1 and str(sim.overrides_file) in errors[0] and problem in errors[0]
    assert stops(events) == [] and not [e for e in events if e[0] == "patch"]
    assert "gateway.bind" in pending_paths(s)                       # the key stays pending


# --- (o) the engine section's changes are listed and recorded (Q8) -----------------------------------------------------------

def test_engine_section_lists_its_changes_and_records_them_for_the_watch_without_leaking_a_secret(
        sim, script, monkeypatch):
    s = _state()
    s.openclaw.engine = "claude-code"
    secret = "sk-live-0123456789SECRETVALUE"
    doc = {"agents": {"defaults": {"model": {"primary": "anthropic/old-model"}}}}

    def fake_patch(*_a, **_k):
        return {"agents": {"defaults": {"model": {"primary": "anthropic/new-model"}}},
                "models": {"providers": {"x": {"apiKey": secret}}}}

    monkeypatch.setattr(openclaw, "build_patch", fake_patch)
    monkeypatch.setattr(openclaw, "_apply_hot", lambda patch, **_k: (True, ""))
    run_changes: list[dict] = []
    written: list = []
    ctx = {"state": s, "dry_run": False, "run_changes": run_changes}
    assert openclaw._configure_engine(ctx, doc, sim.cfg, "/kit", written) is True

    listed = [m for m in script.messages("info") if m.startswith("changed by the engine section: ")]
    assert len(listed) == 1 and "agents.defaults.model.primary" in listed[0]
    rec = [c for c in run_changes if c["path"] == ["agents", "defaults", "model", "primary"]]
    assert len(rec) == 1 and rec[0]["action"] == "engine" and rec[0]["previous"] == "anthropic/old-model"
    # the watch's revert path can invert what was recorded
    inverse, _rp = host.restore_patch(rec)
    assert inverse["agents"]["defaults"]["model"]["primary"] == "anthropic/old-model"
    assert secret not in " ".join(m for _l, m in script.log) and secret not in json.dumps(run_changes)

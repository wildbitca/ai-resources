"""Item 2 (3.2.8): a config patch refused because OpenClaw's state is in offline maintenance is retried with a short
backoff inside a bounded budget, recorded as pending when the budget runs out, and never lost silently."""
from __future__ import annotations

import pytest

from ai_resources.setup import patch_retry as pr
from ai_resources.setup import state, ui

# The text `openclaw config patch` prints while the gateway does not hold the state database (real 2026.10.7 shape).
MAINT = ("Error: OpenClaw state at /home/op/.openclaw/state/openclaw.sqlite is undergoing offline maintenance; "
         "retry when it finishes.")
PATCH = {"agents": {"defaults": {"subagents": {"model": None}}}}


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(pr, "_sleep", c.sleep)
    monkeypatch.setattr(pr, "_clock", c.time)
    monkeypatch.setattr(pr, "_marker_exists", lambda: False)
    monkeypatch.delenv("AI_RESOURCES_PATCH_WAIT_S", raising=False)
    pr.set_wait_budget(None)
    pr.clear_exhausted()
    yield c
    pr.set_wait_budget(None)
    pr.clear_exhausted()


def _sender(results):
    calls = []

    def send():
        calls.append(1)
        return results[min(len(calls), len(results)) - 1]
    return send, calls


def test_is_maintenance_matches_the_real_text():
    assert pr.is_maintenance(MAINT)
    assert not pr.is_maintenance("config validation failed: unknown key")


def test_rejected_then_retried_then_applied(clock):
    send, calls = _sender([(1, MAINT), (1, MAINT), (0, "ok")])
    rc, out = pr.send(send, patch=PATCH)
    assert (rc, out) == (0, "ok")
    assert len(calls) == 3 and clock.sleeps == [2, 4]
    assert pr.exhausted() == []


def test_a_non_maintenance_failure_is_not_retried(clock):
    send, calls = _sender([(1, "config validation failed")])
    assert pr.send(send, patch=PATCH)[0] == 1
    assert len(calls) == 1 and clock.sleeps == []


def test_budget_exhausted_records_the_patch_and_returns_the_failure(clock):
    send, calls = _sender([(1, MAINT)])
    rc, out = pr.send(send, patch=PATCH, replace_paths=["a.b"])
    assert rc == 1 and pr.is_maintenance(out)
    assert sum(clock.sleeps) <= pr.DEFAULT_WAIT_S and max(clock.sleeps) <= 30   # no long sleep in one call
    assert sum(clock.sleeps) >= pr.DEFAULT_WAIT_S - 30
    [e] = pr.exhausted()
    assert e["patch"] == PATCH and e["replace_paths"] == ["a.b"]


def test_budget_from_env_and_override(clock, monkeypatch):
    monkeypatch.setenv("AI_RESOURCES_PATCH_WAIT_S", "10")
    send, _ = _sender([(1, MAINT)])
    pr.send(send, patch=PATCH)
    assert sum(clock.sleeps) <= 10
    clock.sleeps.clear()
    pr.set_wait_budget(0)
    pr.send(send, patch=PATCH)
    assert clock.sleeps == []


def test_a_dry_run_refused_for_the_whole_budget_is_recorded_and_the_drained_window_is_not(clock):
    send, calls = _sender([(1, MAINT)])
    pr.send(send, patch=PATCH, dry_run=True, flags={"slack_cost": True, "retired": False})
    [e] = pr.exhausted()                              # B1: a refused dry run is never lost silently
    assert e["patch"] == PATCH and e["flags"] == {"slack_cost": True, "retired": False}
    clock.sleeps.clear()
    pr.clear_exhausted()
    pr.send(send, patch=PATCH, retry=False)          # inside the kit's own drained window the gateway is ours
    assert clock.sleeps == [] and pr.exhausted() == []


def test_a_dry_run_then_the_real_write_of_the_same_patch_is_recorded_once(clock):
    send, _ = _sender([(1, MAINT)])
    pr.send(send, patch=PATCH, dry_run=True)
    pr.send(send, patch=PATCH)
    assert len(pr.exhausted()) == 1


def test_a_real_write_that_lands_clears_the_entry_a_dry_run_left(clock):
    pr.send(_sender([(1, MAINT)])[0], patch=PATCH, dry_run=True)
    assert len(pr.exhausted()) == 1
    pr.send(_sender([(0, "ok")])[0], patch=PATCH)
    assert pr.exhausted() == []


def test_five_patches_share_one_run_wide_budget(clock):
    pr.set_wait_budget(60)
    send, calls = _sender([(1, MAINT)])
    for i in range(5):
        pr.send(send, patch={"k": i})
    assert sum(clock.sleeps) <= 60                    # B2: not 5 x 60
    assert len(pr.exhausted()) == 5
    slept = sum(clock.sleeps)
    pr.send(send, patch={"k": 99})                    # budget spent: one attempt, no wait, recorded
    assert sum(clock.sleeps) == slept and len(pr.exhausted()) == 6


def test_the_budget_is_capped_and_negatives_are_rejected(clock, monkeypatch):
    pr.set_wait_budget(100000)
    assert pr.wait_budget() == pr.MAX_WAIT_S == 900
    with pytest.raises(ValueError):
        pr.set_wait_budget(-1)
    pr.set_wait_budget(None)
    monkeypatch.setenv("AI_RESOURCES_PATCH_WAIT_S", "-5")
    assert pr.wait_budget() == pr.DEFAULT_WAIT_S
    monkeypatch.setenv("AI_RESOURCES_PATCH_WAIT_S", "99999")
    assert pr.wait_budget() == 900


def test_the_cli_rejects_a_negative_patch_wait():
    from ai_resources import cli
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["setup", "--patch-wait", "-3"])
    assert cli.build_parser().parse_args(["setup", "--patch-wait", "30"]).patch_wait == 30


def _hostile(flags, patch=None):
    return {"patch": patch or {"a": 1}, "replace_paths": [], "flags": flags, "source": "setup",
            "at": "2026-10-28T00:00:00+00:00"}


def test_replay_passes_only_the_allowlisted_boolean_flags(clock):
    o = state.OpenClawState()
    o.config_patch_pending = [_hostile({"slack_cost": True, "allow_restart": True, "reload_mode": "off", "x": 1}),
                              _hostile({"retired": "yes"}, {"b": 2})]
    seen = []

    def fake(patch, **kw):
        kw.pop("replace_paths", None)
        seen.append(kw)
        return True, "ok"
    assert pr.apply_pending_patches(o, fake) is True
    assert seen[0] == {"slack_cost": True} and "allow_restart" not in seen[0]
    assert seen[1] == {}                              # a non-bool value is dropped


def test_replay_refuses_an_entry_with_both_flags_and_keeps_it_pending(clock):
    o = state.OpenClawState()
    o.config_patch_pending = [_hostile({"slack_cost": True, "retired": True})]
    called = []
    assert pr.apply_pending_patches(o, lambda *a, **k: called.append(1) or (True, "")) is False
    assert not called and len(o.config_patch_pending) == 1


def test_replay_keeps_an_entry_pending_when_it_would_restart(clock):
    from ai_resources.openclaw_reload_rules import RestartRequired
    o = state.OpenClawState()
    o.config_patch_pending = [_hostile({})]

    def fake(patch, **kw):
        raise RestartRequired(["gateway.port"])
    assert pr.apply_pending_patches(o, fake) is False
    assert len(o.config_patch_pending) == 1


def test_replay_warns_about_an_entry_older_than_a_week(clock, monkeypatch):
    warns = []
    monkeypatch.setattr(ui, "warn", warns.append)
    o = state.OpenClawState()
    e = _hostile({})
    e["at"] = "2026-01-01T00:00:00+00:00"
    o.config_patch_pending = [e]
    pr.apply_pending_patches(o, lambda *a, **k: (True, "ok"))
    assert any("older than 7 days" in w for w in warns)


def test_an_open_maintenance_window_is_waited_out_before_the_first_attempt(clock, monkeypatch):
    open_for = iter([True, True, False])
    monkeypatch.setattr(pr, "_marker_exists", lambda: next(open_for))
    send, calls = _sender([(0, "ok")])
    assert pr.send(send, patch=PATCH)[0] == 0
    assert clock.sleeps == [2, 4] and len(calls) == 1


def test_record_pending_is_idempotent_and_lists_the_command(clock):
    send, _ = _sender([(1, MAINT)])
    pr.send(send, patch=PATCH)
    o = state.OpenClawState()
    pr.record_pending(o)
    pr.record_pending(o)
    assert len(o.config_patch_pending) == 1
    assert pr.PENDING_COMMAND in pr.finish_command()


def test_incomplete_run_prints_the_exact_command_and_flags_the_exit(clock, capsys):
    ui.clear_skipped()
    send, _ = _sender([(1, MAINT)])
    pr.send(send, patch=PATCH)
    o = state.OpenClawState()
    assert pr.settle(o) == pr.EXIT_PATCH_PENDING
    assert pr.EXIT_PATCH_PENDING not in (0, 1)
    assert len(o.config_patch_pending) == 1
    assert any(pr.PENDING_COMMAND in how for _w, _y, how in ui.skipped())
    ui.clear_skipped()


def test_settle_with_nothing_exhausted_is_zero(clock):
    assert pr.settle(state.OpenClawState()) == 0


def test_apply_pending_patches_applies_clears_and_keeps_the_failed(clock):
    o = state.OpenClawState()
    o.config_patch_pending = [{"patch": {"a": 1}, "replace_paths": [], "flags": {}, "source": "setup", "at": "t"},
                              {"patch": {"b": 2}, "replace_paths": [], "flags": {}, "source": "setup", "at": "t"}]
    seen = []

    def fake_apply(patch, **kw):
        seen.append(patch)
        return (patch == {"a": 1}), ("ok" if patch == {"a": 1} else "boom")
    assert pr.apply_pending_patches(o, fake_apply) is False
    assert seen == [{"a": 1}, {"b": 2}]
    assert [e["patch"] for e in o.config_patch_pending] == [{"b": 2}]


# --- through the real writer (cockpit apply_patch) on the simulated host of the wizard tests -----------------------

from test_openclaw_host_wizard import _run_wizard, _state, script, sim   # noqa: E402,F401  (fixtures)
from ai_resources.setup.cockpits import openclaw as _cockpit            # noqa: E402


def _flaky(sim, monkeypatch, refusals):
    """The first `refusals` real `config patch` calls are refused with the maintenance text (real shape)."""
    inner, left = sim.oc, [refusals]

    def oc(args, stdin=None, timeout=120):
        if args[:2] == ["config", "patch"] and "--dry-run" not in args and left[0] != 0:
            left[0] -= 1 if left[0] > 0 else 0
            inner.calls.append((list(args), None))
            return 1, MAINT
        return inner(args, stdin, timeout)
    monkeypatch.setattr(_cockpit, "_openclaw", oc)


def test_a_refused_patch_is_retried_and_lands_when_the_window_ends(sim, script, clock, monkeypatch):
    _flaky(sim, monkeypatch, 2)
    s = _state()
    _run_wizard(s)
    assert sim.oc.real_patches(), "the patch must have been applied after the retries"
    assert pr.exhausted() == [] and clock.sleeps[:2] == [2, 4]
    assert s.openclaw.host_config_changes and pr.settle(s.openclaw) == 0


def test_a_patch_refused_for_the_whole_budget_is_recorded_and_the_run_is_incomplete(sim, script, clock, monkeypatch):
    _flaky(sim, monkeypatch, -1)                       # never applied
    pr.set_wait_budget(6)
    s = _state()
    _run_wizard(s)
    import json
    live, was = (json.loads(b.decode()) for b in (sim.cfg.read_bytes(), sim.original_config))
    assert live["agents"] == was["agents"] and live["gateway"] == was["gateway"]      # every config patch was refused
    assert pr.exhausted(), "nothing may be lost silently"
    assert pr.settle(s.openclaw) == pr.EXIT_PATCH_PENDING
    assert s.openclaw.config_patch_pending and all(e["patch"] for e in s.openclaw.config_patch_pending)
    # the next run (or apply-pending) applies it through the same writer once the window is over
    _flaky(sim, monkeypatch, 0)
    assert pr.apply_pending_patches(s.openclaw, _cockpit.apply_patch) is True
    assert s.openclaw.config_patch_pending == [] and json.loads(sim.cfg.read_text())["agents"] != was["agents"]


def _flaky_dry(sim, monkeypatch):
    """Every DRY-RUN `config patch` is refused with the maintenance text; real writes are never reached."""
    inner = sim.oc

    def oc(args, stdin=None, timeout=120):
        if args[:2] == ["config", "patch"] and "--dry-run" in args:
            inner.calls.append((list(args), None))
            return 1, MAINT
        return inner(args, stdin, timeout)
    monkeypatch.setattr(_cockpit, "_openclaw", oc)


def test_a_dry_run_refused_for_the_whole_budget_ends_incomplete_exit_75(sim, script, clock, monkeypatch):
    _flaky_dry(sim, monkeypatch)
    pr.set_wait_budget(6)
    s = _state()
    _run_wizard(s)
    assert not sim.oc.real_patches()
    assert pr.exhausted(), "the refused dry run must be recorded"
    assert pr.settle(s.openclaw) == pr.EXIT_PATCH_PENDING
    assert s.openclaw.config_patch_pending


# --- FIX2 (verifier findings) ----------------------------------------------------------------------------------------

def test_settle_prints_the_exact_finishing_command(clock, monkeypatch):
    printed: list[str] = []
    monkeypatch.setattr(ui, "error", printed.append)
    ui.clear_skipped()
    pr.send(_sender([(1, MAINT)])[0], patch=PATCH)
    assert pr.settle(state.OpenClawState()) == pr.EXIT_PATCH_PENDING
    ui.clear_skipped()
    [line] = printed
    assert pr.finish_command() in line and f"{pr.PENDING_COMMAND} --yes" in line


def test_the_wizard_exits_75_and_prints_no_completion_banner_when_every_patch_is_refused(
        sim, script, clock, monkeypatch, tmp_path):
    import argparse
    from ai_resources.setup import state as st, wizard
    from test_openclaw_host_wizard import _step10_env
    _step10_env(monkeypatch, [])
    _flaky(sim, monkeypatch, -1)                       # every real patch refused for the whole budget
    pr.set_wait_budget(6)
    monkeypatch.setattr(ui, "require_deps", lambda: None)
    monkeypatch.setattr(ui, "banner", lambda *a, **k: None)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(st, "load", lambda: st.SetupState())
    monkeypatch.setattr(st, "save", lambda s: None)
    monkeypatch.setattr(st, "is_first_run", lambda: True)
    for step in ("_step_cockpit_auth", "_step2_cockpits", "_step6_single_model_profile", "_step7_cockpit_config",
                 "_step_models", "_step_preset", "_step_summary"):
        monkeypatch.setattr(wizard, step, lambda *a, **k: 0)

    def apply(s, dry_run=False):
        s.cockpits.update(_state().cockpits)
        s.openclaw.engine = "keep"
        _run_wizard(s)                                 # the real OpenClaw writer on the simulated host
        wizard._APPLIED_TARGETS = []
        return 0
    monkeypatch.setattr(wizard, "_step9_apply", apply)
    banner: list[int] = []
    monkeypatch.setattr(wizard, "_print_completion", lambda s: banner.append(1))
    args = argparse.Namespace(dry_run=False, non_interactive=False, profile="", accept_gates=True)
    from ai_resources import cli
    rc = cli.cmd_setup(args)
    assert rc == pr.EXIT_PATCH_PENDING == 75, script.messages("error")
    assert banner == [], "no 'Setup complete' banner for an incomplete run"

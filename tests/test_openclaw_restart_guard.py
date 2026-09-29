"""The pre-restart guard: `gateway_busy()` and the decision flow in `restart_gateway()`.

`brew upgrade` moves the kit plugin, so setup's normal path restarts the gateway, and a restart
kills every agent turn in flight. The probe reads the unit's cgroup and counts only agent CLI
workers; a fake proc tree under tmp_path and a fake runner stand in for systemd and /proc, so
nothing here can reach a live gateway. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import openclaw_host as host  # noqa: E402
from ai_resources.setup import state, ui  # noqa: E402
from ai_resources.setup.cockpits import openclaw  # noqa: E402

CGROUP = "/user.slice/user-1000.slice/user@1000.service/app.slice/openclaw-gateway.service"
CLK = os.sysconf("SC_CLK_TCK")
UPTIME = 5000.0


class Tree:
    """A fake /sys/fs/cgroup + /proc holding the gateway unit's processes."""

    def __init__(self, tmp_path: pathlib.Path):
        self.cgroup_root = tmp_path / "cgroup"
        self.proc_root = tmp_path / "proc"
        self.unit_dir = self.cgroup_root / CGROUP.lstrip("/")
        self.unit_dir.mkdir(parents=True)
        self.proc_root.mkdir()
        (self.proc_root / "uptime").write_text(f"{UPTIME} 0.00\n")
        self.pids: dict[str, list[int]] = {}

    def add(self, pid: int, *argv: str, age: float = 60.0, in_cgroup: bool = True, listed: bool = True,
            sub: str = ""):
        """`sub` puts the process in a descendant cgroup of the unit (e.g. a scope per turn)."""
        if in_cgroup:
            here = self.unit_dir / sub
            here.mkdir(parents=True, exist_ok=True)
            self.pids.setdefault(sub, []).append(pid)
            (here / "cgroup.procs").write_text("".join(f"{p}\n" for p in self.pids[sub]))
        if not listed:
            return
        d = self.proc_root / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        # Field 2 (comm) may hold spaces and parentheses: the parser must split after the LAST ")".
        start = int((UPTIME - age) * CLK)
        fields = ["S"] + ["0"] * 18 + [str(start)]
        (d / "stat").write_text(f"{pid} (node (worker)) " + " ".join(fields) + " 0 0\n")


def fake_systemctl(control_group: str = CGROUP, rc: int = 0):
    calls: list[list[str]] = []

    def run(argv, **_kw):
        calls.append(argv)
        if argv[:3] == ["systemctl", "--user", "show"] and argv[-3:] == ["-p", "ControlGroup", "--value"]:
            assert argv[3] == host.GATEWAY_UNIT
            return rc, control_group
        raise AssertionError(f"unexpected command {argv}")

    run.calls = calls
    return run


@pytest.fixture
def tree(tmp_path):
    return Tree(tmp_path)


def probe(tree, runner=None):
    return host.gateway_busy(runner or fake_systemctl(), cgroup_root=tree.cgroup_root, proc_root=tree.proc_root)


# --- the probe ---------------------------------------------------------------------------------------

def test_an_idle_gateway_is_not_busy(tree):
    tree.add(100, "node", "/opt/openclaw/dist/index.js", "gateway")
    assert probe(tree) == (0, [])


def test_agent_cli_workers_are_counted_with_pid_and_elapsed(tree):
    tree.add(100, "node", "/opt/openclaw/dist/index.js", "gateway")
    tree.add(201, "claude", "--print", age=3725.0)
    tree.add(202, "/home/u/.local/bin/codex", "exec", age=12.0)
    count, detail = probe(tree)
    assert count == 2
    assert [(d.pid, d.agent) for d in detail] == [(201, "claude"), (202, "codex")]
    assert [round(d.elapsed) for d in detail] == [3725, 12]


def test_the_gateways_own_node_workers_are_never_counted(tree):
    """AC-5: spawn-broker and sqlite-readonly-location are the gateway's plumbing, not a turn."""
    tree.add(100, "node", "/opt/openclaw/dist/index.js", "gateway", "--port", "18789")
    tree.add(101, "spawn-broker")
    tree.add(102, "node", "/opt/openclaw/dist/spawn-broker.js")
    tree.add(103, "sqlite-readonly-location", "/home/u/.openclaw/state.db")
    # An agent's name inside another program's arguments is not an agent worker either.
    tree.add(104, "node", "/opt/node_modules/@anthropic-ai/claude-code/cli.js")
    assert probe(tree) == (0, [])


def test_workers_in_descendant_cgroups_are_counted(tree):
    """cgroup v2 lists only direct members in cgroup.procs; the drain (TasksCurrent) is recursive,
    so the probe must be too or a Delegate=yes / scope-per-turn gateway would always look idle."""
    tree.add(100, "node", "/opt/openclaw/dist/index.js", "gateway")
    tree.add(201, "claude", "--print", sub="turn-1.scope")
    tree.add(202, "codex", "exec", sub="turn-2.scope/inner")
    count, detail = probe(tree)
    assert count == 2 and sorted(d.pid for d in detail) == [201, 202]


def test_a_pid_listed_in_two_cgroups_is_counted_once(tree):
    tree.add(201, "claude", "--print")
    (tree.unit_dir / "sub").mkdir()
    (tree.unit_dir / "sub" / "cgroup.procs").write_text("201\n")
    assert probe(tree)[0] == 1


def test_a_process_that_exits_between_the_two_reads_is_skipped(tree):
    tree.add(201, "claude", "--print")
    tree.add(202, "claude", "--print", listed=False)   # in cgroup.procs, gone from /proc
    count, detail = probe(tree)
    assert count == 1 and detail[0].pid == 201


def test_an_unreadable_start_time_still_counts_the_worker(tree):
    tree.add(201, "claude", "--print")
    (tree.proc_root / "201" / "stat").unlink()
    count, detail = probe(tree)
    assert count == 1 and detail[0].elapsed is None


def test_the_probe_reuses_the_gateway_unit_and_the_systemd_env(tree, monkeypatch):
    """AC-6: same unit constant and env handling as the drained doctor, so the two cannot drift."""
    tree.add(201, "claude")
    seen = {}

    def runner(argv, **kw):
        seen["argv"], seen["env"] = argv, kw.get("env")
        return 0, CGROUP

    monkeypatch.setattr(host, "systemd_env", lambda: {"XDG_RUNTIME_DIR": "/run/user/1000"})
    host.gateway_busy(runner, cgroup_root=tree.cgroup_root, proc_root=tree.proc_root)
    assert seen["argv"] == ["systemctl", "--user", "show", host.GATEWAY_UNIT, "-p", "ControlGroup", "--value"]
    assert seen["env"] == {"XDG_RUNTIME_DIR": "/run/user/1000"}


# --- a broken probe never blocks (AC-4) -----------------------------------------------------------------

@pytest.mark.parametrize("runner", [
    fake_systemctl(rc=1),                       # systemctl failed
    fake_systemctl(control_group=""),           # the unit has no cgroup (stopped)
    fake_systemctl(rc=127, control_group="systemctl not found"),
])
def test_a_systemctl_failure_is_not_busy(tree, runner):
    tree.add(201, "claude")
    assert probe(tree, runner) == (0, [])


def test_a_missing_cgroup_directory_is_not_busy(tree):
    assert probe(tree, fake_systemctl("/no/such/slice.service")) == (0, [])


def test_an_unreadable_cgroup_procs_is_not_busy(tree):
    tree.add(201, "claude")
    (tree.unit_dir / "cgroup.procs").chmod(0)
    try:
        if os.access(tree.unit_dir / "cgroup.procs", os.R_OK):
            pytest.skip("running as a user that ignores file modes")
        assert probe(tree) == (0, [])
    finally:
        (tree.unit_dir / "cgroup.procs").chmod(0o644)


def test_a_runner_that_raises_is_not_busy(tree):
    def boom(argv, **_kw):
        raise RuntimeError("dbus went away")

    assert probe(tree, boom) == (0, [])


def test_garbage_in_cgroup_procs_is_ignored(tree):
    tree.add(201, "claude")
    (tree.unit_dir / "cgroup.procs").write_text("201\nnot-a-pid\n\n")
    assert probe(tree)[0] == 1


def test_a_control_group_that_climbs_out_of_the_cgroup_root_is_not_followed(tree, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "cgroup.procs").write_text("201\n")
    tree.add(201, "claude", in_cgroup=False)
    assert probe(tree, fake_systemctl("/../outside")) == (0, [])


def test_a_broken_probe_says_so_quietly_but_still_reports_not_busy(tree):
    """Fail-open makes a dead guard look like an idle host, so the failure leaves a trace."""
    said: list[str] = []
    assert host.gateway_busy(fake_systemctl(rc=1), cgroup_root=tree.cgroup_root, proc_root=tree.proc_root,
                             out=said.append) == (0, [])
    assert len(said) == 1 and "restart guard" in said[0] and "idle" in said[0]

    said.clear()

    def boom(argv, **_kw):
        raise RuntimeError("dbus went away")

    assert host.gateway_busy(boom, cgroup_root=tree.cgroup_root, proc_root=tree.proc_root, out=said.append) == (0, [])
    assert len(said) == 1 and "RuntimeError" in said[0]


def test_a_stopped_unit_and_a_healthy_probe_stay_silent(tree):
    """Not noise on a normal run: a stopped unit (no cgroup) and an idle host say nothing."""
    said: list[str] = []
    tree.add(100, "node", "/opt/openclaw/dist/index.js", "gateway")
    host.gateway_busy(fake_systemctl(control_group=""), cgroup_root=tree.cgroup_root, proc_root=tree.proc_root,
                      out=said.append)
    host.gateway_busy(fake_systemctl(), cgroup_root=tree.cgroup_root, proc_root=tree.proc_root, out=said.append)
    assert said == []


# --- the decision flow -----------------------------------------------------------------------------------

BUSY = (2, [host.AgentProc(201, "claude", 3725.0), host.AgentProc(202, "codex", 12.0)])
IDLE = (0, [])


class Flow:
    """Everything restart_gateway() touches, faked and recorded."""

    def __init__(self, monkeypatch, probes, answer="defer", restart_rc=0):
        self.probes = list(probes)
        self.answer = answer
        self.asked: list[tuple[str, list[str]]] = []
        self.restarts = 0
        self.slept: list[float] = []
        self.clock = 0.0
        self.messages: list[str] = []

        def fake_openclaw(args, stdin=None, timeout=120):
            if args == ["gateway", "restart"]:
                self.restarts += 1
                return restart_rc, "restarted" if restart_rc == 0 else "boom"
            return 0, "{}"

        monkeypatch.setattr(openclaw, "_openclaw", fake_openclaw)
        monkeypatch.setattr(openclaw, "backend_registered", lambda _b: True)
        monkeypatch.setattr(ui, "warn", self.messages.append)
        monkeypatch.setattr(ui, "info", self.messages.append)

    def probe(self):
        # The last answer repeats, so a test can say "busy forever" with one entry.
        return self.probes.pop(0) if len(self.probes) > 1 else self.probes[0]

    def ask(self, message, choices, **_kw):
        self.asked.append((message, list(choices)))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.clock += seconds
        # A wait loop with no deadline check would spin forever (no pytest-timeout here): fail instead.
        assert len(self.slept) < 1000, "the wait loop never reached its cap"
        if self.sleep_raises:
            raise self.sleep_raises

    sleep_raises: BaseException | None = None

    def monotonic(self):
        return self.clock

    def restart(self, *, interactive=True, **kw):
        return openclaw.restart_gateway(probe=self.probe, ask=self.ask, sleep=self.sleep,
                                        monotonic=self.monotonic, interactive=interactive, **kw)


def test_an_idle_gateway_restarts_without_a_prompt(monkeypatch):
    """AC-1: today's behaviour, byte for byte: one `gateway restart`, no question."""
    f = Flow(monkeypatch, [IDLE], answer=AssertionError("must not ask"))
    assert f.restart() is True
    assert f.restarts == 1 and f.asked == [] and f.slept == []


def test_an_idle_non_interactive_run_restarts_too(monkeypatch):
    f = Flow(monkeypatch, [IDLE])
    assert f.restart(interactive=False) is True
    assert f.restarts == 1


def test_busy_and_interactive_asks_with_three_answers_and_the_detail(monkeypatch):
    f = Flow(monkeypatch, [BUSY], answer="defer")
    with pytest.raises(openclaw.RestartDeferred):
        f.restart()
    message, choices = f.asked[0]
    assert choices == [openclaw.RESTART_NOW, openclaw.RESTART_DEFER, openclaw.RESTART_WAIT]
    assert "2 agent" in message and "201" in message and "claude" in message and "1h 02m" in message


def test_restart_now_restarts_over_live_work(monkeypatch):
    f = Flow(monkeypatch, [BUSY], answer=openclaw.RESTART_NOW)
    assert f.restart() is True
    assert f.restarts == 1


def test_defer_leaves_the_gateway_running(monkeypatch):
    """AC-2: declining restarts nothing."""
    f = Flow(monkeypatch, [BUSY], answer=openclaw.RESTART_DEFER)
    with pytest.raises(openclaw.RestartDeferred) as exc:
        f.restart()
    assert f.restarts == 0
    assert exc.value.busy == BUSY[1] and "agent" in exc.value.reason


def test_ctrl_c_or_a_cancelled_prompt_defers(monkeypatch):
    for answer in (KeyboardInterrupt(), None):
        f = Flow(monkeypatch, [BUSY], answer=answer)
        with pytest.raises(openclaw.RestartDeferred):
            f.restart()
        assert f.restarts == 0


def test_wait_polls_until_idle_then_restarts(monkeypatch):
    f = Flow(monkeypatch, [BUSY, BUSY, (1, BUSY[1][:1]), IDLE], answer=openclaw.RESTART_WAIT)
    assert f.restart(wait_interval=15) is True
    assert f.slept == [15, 15, 15] and f.restarts == 1


def test_wait_falls_back_to_defer_at_the_cap(monkeypatch):
    f = Flow(monkeypatch, [BUSY], answer=openclaw.RESTART_WAIT)
    with pytest.raises(openclaw.RestartDeferred) as exc:
        f.restart(wait_cap=60, wait_interval=15)
    assert f.restarts == 0 and f.clock >= 60
    assert "60" in exc.value.reason or "1 min" in exc.value.reason


def test_ctrl_c_during_the_wait_defers_and_never_restarts(monkeypatch):
    f = Flow(monkeypatch, [BUSY], answer=openclaw.RESTART_WAIT)
    f.sleep_raises = KeyboardInterrupt()
    with pytest.raises(openclaw.RestartDeferred):
        f.restart()
    assert f.restarts == 0


def test_the_default_wait_cap_is_thirty_minutes():
    assert openclaw.RESTART_WAIT_CAP == 30 * 60


def test_busy_and_non_interactive_defers_without_asking(monkeypatch):
    """AC-3: nobody to ask, so the only safe answer is not to restart."""
    f = Flow(monkeypatch, [BUSY], answer=AssertionError("must not ask"))
    with pytest.raises(openclaw.RestartDeferred):
        f.restart(interactive=False)
    assert f.restarts == 0 and f.asked == []


def test_a_probe_that_raises_never_blocks_the_restart(monkeypatch):
    f = Flow(monkeypatch, [IDLE], answer=AssertionError("must not ask"))

    def broken():
        raise OSError("/proc is unreadable")

    monkeypatch.setattr(f, "probe", broken)
    assert f.restart(interactive=False) is True
    assert f.restarts == 1


def test_interactivity_defaults_to_the_wizards_own_flags(monkeypatch):
    monkeypatch.setattr(ui, "is_non_interactive", lambda: True)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    assert openclaw._can_ask() is False
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    assert openclaw._can_ask() is True
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: False)
    assert openclaw._can_ask() is False


# --- a deferral is loud and recorded ----------------------------------------------------------------------

def _antigravity_state():
    s = state.SetupState()
    for cid in ("agy", "claude", "openclaw"):
        s.cockpits[cid] = state.CockpitState(installed=True)
    s.openclaw.engine = "antigravity"
    s.openclaw.risk_acknowledged = True
    return s


@pytest.fixture
def stale_plugin(monkeypatch):
    """A gateway running the plugin from an older kit directory, with the bridge already registered."""
    monkeypatch.setattr(openclaw, "backend_registered", lambda _b: True)
    monkeypatch.setattr(openclaw, "plugin_is_stale", lambda _d: True)
    monkeypatch.setattr(openclaw, "register_agy_mcp_bridge", lambda *_a: (True, False, ""))
    monkeypatch.setattr(openclaw, "_openclaw", lambda *_a, **_k: (0, "ok"))
    monkeypatch.setattr(openclaw, "_gateway_main_pid", lambda: "4242")
    errors: list[str] = []
    monkeypatch.setattr(ui, "error", errors.append)
    monkeypatch.setattr(ui, "info", lambda _m: None)
    monkeypatch.setattr(ui, "ok", lambda _m: None)
    return errors


def _defer(monkeypatch):
    def restart(_backend="agy-cli"):
        raise openclaw.RestartDeferred("2 agent turn(s) in flight", BUSY[1])

    monkeypatch.setattr(openclaw, "restart_gateway", restart)


def test_a_deferred_restart_is_recorded_and_reported_not_complete(stale_plugin, monkeypatch):
    _defer(monkeypatch)
    s = _antigravity_state()
    assert openclaw._register_plugin_and_backends(s, "/kit") is False
    pending = s.openclaw.gateway_restart_pending
    assert pending["command"] == "openclaw gateway restart"
    assert "2 agent turn(s) in flight" in pending["reason"]
    assert pending["main_pid"] == "4242" and pending["since"]
    text = "\n".join(stale_plugin)
    assert "openclaw gateway restart" in text and "in flight" in text and "not complete" in text.lower()


def test_a_deferred_restart_survives_a_state_round_trip(stale_plugin, monkeypatch, tmp_path):
    _defer(monkeypatch)
    s = _antigravity_state()
    openclaw._register_plugin_and_backends(s, "/kit")
    monkeypatch.setattr(state, "state_path", lambda: tmp_path / "setup-state.yaml")
    state.save(s)
    assert state.load().openclaw.gateway_restart_pending == s.openclaw.gateway_restart_pending


def test_a_restart_that_happens_clears_the_pending_action(stale_plugin, monkeypatch):
    s = _antigravity_state()
    s.openclaw.gateway_restart_pending = {"reason": "old", "command": "openclaw gateway restart",
                                          "main_pid": "1", "since": "then"}
    monkeypatch.setattr(openclaw, "restart_gateway", lambda _b="agy-cli": True)
    assert openclaw._register_plugin_and_backends(s, "/kit") is True
    assert s.openclaw.gateway_restart_pending == {}


def test_a_gateway_that_is_current_again_clears_the_pending_action(stale_plugin, monkeypatch):
    """The operator ran the command by hand between two setup runs."""
    monkeypatch.setattr(openclaw, "plugin_is_stale", lambda _d: False)
    s = _antigravity_state()
    s.openclaw.gateway_restart_pending = {"reason": "old", "command": "openclaw gateway restart",
                                          "main_pid": "1", "since": "then"}
    assert openclaw._register_plugin_and_backends(s, "/kit") is True
    assert s.openclaw.gateway_restart_pending == {}


def test_a_failed_restart_is_not_recorded_as_a_deferral(stale_plugin, monkeypatch):
    monkeypatch.setattr(openclaw, "restart_gateway", lambda _b="agy-cli": False)
    s = _antigravity_state()
    assert openclaw._register_plugin_and_backends(s, "/kit") is False
    assert s.openclaw.gateway_restart_pending == {}


def test_a_deferral_stops_the_engine_patch_from_being_applied(stale_plugin, monkeypatch):
    """`agy-cli/<model>` is unknown until the gateway reloads the plugin: patching now would be rejected."""
    _defer(monkeypatch)
    s = _antigravity_state()
    applied = []
    monkeypatch.setattr(openclaw, "apply_patch", lambda *a, **k: applied.append(1) or (True, ""))
    monkeypatch.setattr(openclaw, "read_config", lambda _p: {})
    written: list = []
    assert openclaw._configure_engine({"state": s}, {}, pathlib.Path("/x/openclaw.json"), "/kit", written) is False
    assert applied == [] and s.openclaw.gateway_restart_pending


# --- verify and `openclaw status` surface it until it is done -----------------------------------------------

def _pending(pid="4242"):
    return {"reason": "2 agent turn(s) in flight", "command": "openclaw gateway restart",
            "main_pid": pid, "since": "2026-09-29T10:00:00+00:00"}


def test_pending_findings_is_an_error_naming_the_command_while_the_gateway_has_not_restarted():
    o = state.OpenClawState(gateway_restart_pending=_pending("4242"))
    found = openclaw.pending_restart_findings(o, main_pid=lambda: "4242")
    assert len(found) == 1 and found[0].level == "error"
    assert "openclaw gateway restart" in found[0].remedy and "in flight" in found[0].message


def test_pending_findings_goes_quiet_once_the_gateway_has_a_new_main_pid():
    o = state.OpenClawState(gateway_restart_pending=_pending("4242"))
    assert openclaw.pending_restart_findings(o, main_pid=lambda: "9999") == []


def test_pending_findings_stays_an_error_when_the_pid_cannot_be_read():
    """Unknown is not done: only a changed pid proves the gateway restarted."""
    o = state.OpenClawState(gateway_restart_pending=_pending("4242"))
    assert len(openclaw.pending_restart_findings(o, main_pid=lambda: "")) == 1


def test_no_pending_action_means_no_finding():
    assert openclaw.pending_restart_findings(state.OpenClawState(), main_pid=lambda: "1") == []


def test_render_pending_names_the_command_and_the_reason():
    text = host.render_pending(_pending())
    assert "openclaw gateway restart" in text and "in flight" in text and "PENDING" in text


def test_status_shows_the_pending_restart_until_the_pid_changes():
    runner = lambda argv, **_kw: (0, "4242")   # noqa: E731 - MainPID unchanged
    assert "PENDING" in host.pending_restart_report(_pending("4242"), runner)
    runner = lambda argv, **_kw: (0, "9999")   # noqa: E731 - the gateway restarted
    assert host.pending_restart_report(_pending("4242"), runner) == ""
    assert host.pending_restart_report({}, runner) == ""


def test_main_pid_uses_the_same_unit_and_env(monkeypatch):
    seen = {}

    def runner(argv, **kw):
        seen["argv"], seen["env"] = argv, kw.get("env")
        return 0, "4242\n"

    monkeypatch.setattr(host, "systemd_env", lambda: {"K": "v"})
    assert host.gateway_main_pid(runner) == "4242"
    assert seen["argv"] == ["systemctl", "--user", "show", host.GATEWAY_UNIT, "-p", "MainPID", "--value"]
    assert seen["env"] == {"K": "v"}
    assert host.gateway_main_pid(lambda *_a, **_k: (1, "boom")) == ""


def test_the_status_command_appends_the_pending_restart(monkeypatch, capsys):
    monkeypatch.setattr(host, "collect_status", lambda **_kw: {})
    monkeypatch.setattr(host, "render_status", lambda _r: "OpenClaw host status")
    s = state.SetupState()
    s.openclaw.gateway_restart_pending = _pending()
    monkeypatch.setattr(state, "load", lambda: s)
    monkeypatch.setattr(host, "gateway_main_pid", lambda *_a, **_k: "4242")
    import argparse
    assert host.cmd_status(argparse.Namespace(no_doctor=True)) == 0
    out = capsys.readouterr().out
    assert out.startswith("OpenClaw host status") and "PENDING" in out and "openclaw gateway restart" in out


def test_the_status_command_survives_an_unreadable_setup_state(monkeypatch, capsys):
    monkeypatch.setattr(host, "collect_status", lambda **_kw: {})
    monkeypatch.setattr(host, "render_status", lambda _r: "OpenClaw host status")

    def boom():
        raise OSError("state dir unreadable")

    monkeypatch.setattr(state, "load", boom)
    import argparse
    assert host.cmd_status(argparse.Namespace(no_doctor=True)) == 0
    assert capsys.readouterr().out.strip() == "OpenClaw host status"


# --- an unprovable pending record stays pending (review finding 1) ---------------------------------

@pytest.mark.parametrize("recorded", ["", None, 0])
def test_a_deferral_recorded_without_a_pid_is_never_declared_done(recorded):
    """The pid could not be read when the deferral was written, so no later pid proves a restart."""
    pending = dict(_pending(), main_pid=recorded)
    o = state.OpenClawState(gateway_restart_pending=pending)
    # `main_pid` is passed explicitly: the conftest stub returns "" and would hide this path.
    assert len(openclaw.pending_restart_findings(o, main_pid=lambda: "9999")) == 1
    assert "PENDING" in host.pending_restart_report(pending, lambda *_a, **_k: (0, "9999"))


def test_status_treats_an_unreadable_current_pid_as_not_done():
    runner = lambda *_a, **_k: (1, "dbus went away")   # noqa: E731
    assert "PENDING" in host.pending_restart_report(_pending("4242"), runner)


# --- a malformed record is pending, not absent (security L3) --------------------------------------------

@pytest.mark.parametrize("bad", ["restart later", 7, ["openclaw gateway restart"], True])
def test_a_malformed_pending_record_is_reported_not_raised(bad):
    o = state.OpenClawState(gateway_restart_pending=bad)
    found = openclaw.pending_restart_findings(o, main_pid=lambda: "9999")
    assert len(found) == 1 and found[0].level == "error" and "openclaw gateway restart" in found[0].remedy
    assert "PENDING" in host.pending_restart_report(bad, lambda *_a, **_k: (0, "9999"))


# --- verify never reaches the live unit (review finding 2) ---------------------------------------------------

from test_openclaw_host_wizard import _configure, _state, script, sim  # noqa: E402,F401  (fixtures by name)
from test_openclaw_cockpit import jarvis  # noqa: E402,F401  (fixture by name)
from test_verify_openclaw_host import HostRunner  # noqa: E402


def test_verify_asks_the_injected_runner_for_the_main_pid(sim, script, monkeypatch):
    s = _state()
    _configure(s)
    s.openclaw.gateway_restart_pending = _pending("4242")

    def live(*_a, **_k):
        raise AssertionError("verify reached the live systemctl instead of ctx['runner']")

    monkeypatch.setattr(openclaw, "_gateway_main_pid", live)
    monkeypatch.setattr(host, "default_runner", live)
    calls: list[list[str]] = []
    base = HostRunner()

    def runner(argv, **kw):
        calls.append(list(argv))
        if argv[-3:] == ["-p", "MainPID", "--value"]:
            return 0, "4242"
        return base(argv, **kw)

    found = openclaw.verify({"state": s, "runner": runner})
    assert any(c[-3:] == ["-p", "MainPID", "--value"] for c in calls)
    assert [f.level for f in found if "has not happened" in f.message] == ["error"]


def test_verify_survives_a_scalar_pending_record(sim, script):
    s = _state()
    _configure(s)
    s.openclaw.gateway_restart_pending = "later"
    found = openclaw.verify({"state": s, "runner": lambda a, **k: (0, "9999") if a[-1] == "--value" and "MainPID" in a else HostRunner()(a, **k)})
    assert any("has not happened" in f.message for f in found)


# --- teardown discharges the debt (nothing may demand a restart for a plugin the kit dropped) ------------------

def test_teardown_with_nothing_applied_still_clears_a_deferral(monkeypatch):
    """A deferral leaves `applied` False, so this early-return path is the one that meets it."""
    s = _antigravity_state()
    s.openclaw.gateway_restart_pending = _pending()
    assert openclaw.teardown(s) == []
    assert s.openclaw.gateway_restart_pending == {}


def test_a_full_teardown_clears_a_deferral(jarvis, monkeypatch):
    s = _antigravity_state()
    monkeypatch.setattr(openclaw, "_agy", lambda *_a, **_k: (127, "agy not found"))
    openclaw.configure({"state": s})
    assert s.openclaw.applied
    s.openclaw.gateway_restart_pending = _pending()
    openclaw.teardown(s)
    assert s.openclaw.gateway_restart_pending == {}


def test_engine_teardown_clears_a_deferral(jarvis, monkeypatch):
    s = _antigravity_state()
    monkeypatch.setattr(openclaw, "_agy", lambda *_a, **_k: (127, "agy not found"))
    openclaw.configure({"state": s})
    s.openclaw.gateway_restart_pending = _pending()
    assert openclaw._teardown_engine(s) is True
    assert s.openclaw.gateway_restart_pending == {}


def test_switching_away_from_antigravity_clears_a_deferral(jarvis, monkeypatch):
    s = _antigravity_state()
    monkeypatch.setattr(openclaw, "_agy", lambda *_a, **_k: (127, "agy not found"))
    openclaw.configure({"state": s})
    s.openclaw.engine = "claude-code"
    s.openclaw.gateway_restart_pending = _pending()
    openclaw.configure({"state": s})
    assert s.openclaw.antigravity_applied is False
    assert s.openclaw.gateway_restart_pending == {}

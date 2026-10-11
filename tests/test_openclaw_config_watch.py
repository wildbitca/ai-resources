"""The post-setup watch: revert a setup run if the gateway stops answering (ADR-0003, R3).

`watch_config` runs against a fake runner, a fake clock and a fake notifier; nothing here can reach a
live unit. The launch half (`start_config_watch`) is driven through the wizard's simulated host.
"""
from __future__ import annotations

import json
import shutil

import pytest

from test_openclaw_host_wizard import (  # noqa: F401  (fixtures are used by name)
    _run_wizard, _state, script, sim,
)
from ai_resources import openclaw_host as host
from ai_resources import openclaw_reload_rules as _rr
from ai_resources.setup import state
from ai_resources.setup.cockpits import _openclaw_host as section, openclaw

HOT = {"path": ["tools", "profile"], "previous": "minimal", "had": True, "action": "forced"}
BIND = {"path": ["gateway", "bind"], "previous": "loopback", "had": True, "action": "forced"}
CREATED = {"path": ["gateway", "publicOrigin"], "previous": None, "had": False, "action": "filled",
           "delete": ["gateway", "publicOrigin"]}
TOKEN = {"path": ["gateway", "controlUi", "github", "token"], "previous": None, "had": False, "secret": True,
         "action": "filled", "delete": ["gateway", "controlUi", "github"]}
SECRET_VALUE = "ghp_SUPERSECRETVALUE"


class Rig:
    """A runner + clock + notifier + state box sharing one event list."""

    def __init__(self, tmp_path, changes, *, health=None, unit_states=None, drain_ok=True, run_id="r1"):
        self.events: list[tuple] = []
        self.health = list(health or [])
        self.unit_states = list(unit_states or [])
        self.drain_ok = drain_ok
        self.s = state.SetupState()
        self.s.openclaw.config_watch = {"run_id": run_id, "unit": f"openclaw-config-watch-{run_id}",
                                        "started_at": "", "changes": changes, "done": False, "result": None}
        self.notes: list[str] = []
        self.applied: list[tuple] = []
        self.out: list[str] = []
        self.saves = 0
        self.marker = host.Marker(tmp_path / "watchdog.off")
        self.health_calls = 0
        self.sleeps = 0

    # runner
    def __call__(self, argv, **kw):
        if argv[:3] == ["systemctl", "--user", "show"]:
            if "ActiveState" in argv:
                return 0, (self.unit_states.pop(0) if self.unit_states else "active")
            if "TasksCurrent" in argv:
                self.events.append(("drain",))
                return 0, "[not set]" if self.drain_ok else "5"
        if argv[:3] == ["systemctl", "--user", "stop"] or argv[:3] == ["systemctl", "--user", "start"]:
            self.events.append((argv[2], self.marker.exists()))
            return 0, ""
        if argv[-1:] == ["health"]:
            self.health_calls += 1
            self.events.append(("health",))
            ok = self.health.pop(0) if self.health else False
            return (0 if ok else 1), ""
        if argv[-1:] == ["--version"]:
            return 0, f"OpenClaw {_rr.PINNED_OPENCLAW_VERSION} (abc)"
        raise AssertionError(f"unexpected command {argv}")

    def sleep(self, _s):
        self.sleeps += 1

    def apply_patch(self, patch, *, dry_run=False, replace_paths=None, allow_restart=False, reload_mode=None):
        if not dry_run:
            self.events.append(("patch", tuple(sorted(_leaves(patch))), allow_restart))
        self.applied.append((patch, dry_run, replace_paths, allow_restart))
        return True, "ok"

    def run(self, run_id=None, **kw):
        kw.setdefault("window", 600)
        kw.setdefault("interval", 30)
        return host.watch_config(run_id or self.s.openclaw.config_watch["run_id"], runner=self, sleep=self.sleep,
                                 load=lambda: self.s, save=lambda s: setattr(self, "saves", self.saves + 1),
                                 apply_patch=self.apply_patch, notify=lambda m: self.notes.append(m) or True,
                                 marker=self.marker, reload_mode=lambda: None, out=self.out.append,
                                 drain_interval=1, health_interval=1, **kw)

    def real_writes(self):
        return [a for a in self.applied if not a[1]]

    def systemctl_changes(self):
        return [e for e in self.events if e[0] in ("stop", "start")]


def _leaves(patch, path=()):
    if isinstance(patch, dict) and patch:
        for k, v in patch.items():
            yield from _leaves(v, path + (k,))
    else:
        yield ".".join(path)


@pytest.fixture
def rig(tmp_path):
    def make(changes, **kw):
        return Rig(tmp_path, changes, **kw)
    return make


# --- AC-3 -------------------------------------------------------------------------------------------------

def test_three_failures_while_active_revert_the_hot_keys_once_with_no_restart(rig):
    r = rig([HOT, CREATED])
    assert r.run() == 0
    [(patch, _dry, replace, allow)] = r.real_writes()
    assert patch == {"tools": {"profile": "minimal"}, "gateway": {"publicOrigin": None}}
    assert allow is False
    assert r.systemctl_changes() == [], "a hot revert never stops, starts or restarts anything"
    assert len(r.notes) == 1 and "tools.profile" in r.notes[0] and "gateway.publicOrigin" in r.notes[0]
    assert r.s.openclaw.config_watch["done"] is True and r.saves == 1
    assert r.health_calls == 3


def test_a_healthy_gateway_for_the_whole_window_reverts_nothing_and_stays_bounded(rig):
    r = rig([HOT], health=[True] * 40)
    assert r.run() == 0
    assert r.real_writes() == [] and r.notes == []
    assert r.health_calls == 21 and r.sleeps == 20           # 600 / 30 + 1 ticks, never more
    assert r.s.openclaw.config_watch["done"] is False


def test_two_failures_then_a_success_reset_the_count(rig):
    r = rig([HOT], health=[False, False, True, False, False, True] + [True] * 30)
    assert r.run() == 0
    assert r.real_writes() == []


def test_transient_unit_states_count_neither_way(rig):
    r = rig([HOT], unit_states=["activating"] * 5 + ["reloading", "deactivating"], health=[False, False, False])
    assert r.run() == 0
    assert r.health_calls == 3, "health was not even asked while the unit was activating/reloading/deactivating"
    assert len(r.real_writes()) == 1


def test_watchdog_off_stands_the_watch_down(rig):
    r = rig([HOT])
    r.marker.touch()
    assert r.run() == 0
    assert r.real_writes() == [] and r.health_calls == 0 and r.notes == []


def test_a_superseded_or_cancelled_watch_does_nothing(rig):
    r = rig([HOT])
    r.s.openclaw.config_watch["run_id"] = "someone-else"       # a newer setup run replaced this watch
    assert r.run(run_id="r1") == 0
    assert r.real_writes() == [] and r.health_calls == 0
    r2 = rig([HOT])
    r2.s.openclaw.config_watch = None
    assert r2.run(run_id="r1") == 0 and r2.real_writes() == []


def test_a_finished_watch_never_reverts_twice(rig):
    r = rig([HOT])
    r.s.openclaw.config_watch["done"] = True
    assert r.run() == 0
    assert r.real_writes() == [] and r.notes == []


# --- Q6: restart-required keys are reverted too, but only inside the drained window ----------------------------------

def test_a_restart_required_inverse_is_reverted_only_in_a_drained_window(rig):
    r = rig([HOT, BIND])
    assert r.run() == 0
    kinds = [e for e in r.events if e[0] in ("patch", "stop", "start", "drain", "health")]
    hot_at = kinds.index(("patch", ("tools.profile",), False))
    stop_at = next(i for i, e in enumerate(kinds) if e[0] == "stop")
    bind_at = kinds.index(("patch", ("gateway.bind",), True))
    start_at = next(i for i, e in enumerate(kinds) if e[0] == "start")
    assert hot_at < stop_at < bind_at < start_at, kinds
    assert kinds[stop_at] == ("stop", True), "watchdog.off is held when the gateway stops"
    assert kinds[start_at] == ("start", False), "and removed before it starts again"
    assert not r.marker.exists()
    assert kinds.count(("patch", ("gateway.bind",), True)) == 1
    assert [e for e in kinds if e[0] == "patch" and e[2] is False and "gateway.bind" in e[1]] == [], \
        "never a blind patch of a restart-required key while the gateway is live"
    assert len(r.notes) == 1 and "gateway.bind" in r.notes[0] and "drained window" in r.notes[0]
    assert r.s.openclaw.config_watch["result"]["restart"] == ["gateway.bind"]


def test_a_failed_window_leaves_the_key_pending_and_still_removes_the_marker(rig):
    r = rig([BIND], drain_ok=False)
    r.run(drain_timeout=3)
    assert not r.marker.exists(), "removed on every exit path"
    assert [e[0] for e in r.systemctl_changes()] == ["stop", "start"]
    [entry] = r.s.openclaw.host_restart_pending
    assert entry["path"] == "gateway.bind" and entry["source"] == "watch" and entry["op"] == "restore"
    assert len(r.notes) == 1 and "not reverted" in r.notes[0]


def test_a_gateway_that_is_down_drains_trivially(rig):
    r = rig([BIND], unit_states=["inactive"] * 3)
    assert r.run() == 0
    assert ("patch", ("gateway.bind",), True) in r.events


# --- secrets ---------------------------------------------------------------------------------------------------------

def test_a_secret_leaf_is_named_but_its_value_is_never_printed(rig):
    # The value sits where a buggy watch could leak it: in the records' `previous` fields.
    leaky = {**TOKEN, "previous": SECRET_VALUE}
    had_secret = {"path": ["gateway", "controlUi", "github", "token"], "previous": SECRET_VALUE, "had": True,
                  "secret": True, "action": "forced"}
    r = rig([leaky, had_secret, HOT])
    r.run()
    assert r.notes, "the revert notified the operator"
    everything = " ".join(r.out + r.notes) + json.dumps(r.applied) + json.dumps(r.s.openclaw.config_watch["result"]) \
        + json.dumps(r.s.openclaw.host_restart_pending)
    assert SECRET_VALUE not in everything
    assert "gateway.controlUi.github" in " ".join(r.out + r.notes), "the leaf is named, never its value"
    # A credential that existed before is never deleted or rewritten by a revert.
    assert all("token" not in json.dumps(p) or p == {"gateway": {"controlUi": {"github": None}}}
               for p, *_ in r.real_writes())


# --- change records for the engine section ----------------------------------------------------------------------------

def test_change_records_invert_exactly():
    doc = {"agents": {"defaults": {"model": {"primary": "a/b"}}, "entries": {"main": {"x": 1}}}}
    patch = {"agents": {"defaults": {"model": {"primary": "c/d"}, "subagents": {"allowAgents": ["z"]}},
                        "entries": {"claude": {"id": "claude"}}}}
    recs = host.change_records(doc, patch, ["agents.entries"], "engine")
    by = {".".join(r["path"]): r for r in recs}
    assert by["agents.defaults.model.primary"]["previous"] == "a/b"
    assert by["agents.defaults.subagents.allowAgents"]["had"] is False
    assert by["agents.entries"]["previous"] == {"main": {"x": 1}}, "a replaced subtree is one record"
    restore, rp = host.restore_patch(recs)
    assert restore["agents"]["defaults"]["model"]["primary"] == "a/b"
    assert restore["agents"]["entries"] == {"main": {"x": 1}} and rp == ["agents.entries"]


# --- launch: the wizard starts the watch as a transient unit, never in the foreground ------------------------------------

@pytest.fixture
def ai_resources_on_path(monkeypatch):
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: "/opt/bin/ai-resources" if n == "ai-resources" else real(n, *a, **k))


def _launches(sim):
    return [c for c in sim.systemd.calls if c[0] == "systemd-run"]


def test_setup_starts_a_bounded_transient_unit_and_never_watches_in_process(sim, script, monkeypatch,
                                                                        ai_resources_on_path):
    monkeypatch.setattr(host, "watch_config", lambda *a, **k: pytest.fail("setup must not run the watch in-process"))
    s = _state()
    _run_wizard(s)
    [argv] = _launches(sim)
    assert argv[:2] == ["systemd-run", "--user"] and "--collect" in argv and "RuntimeMaxSec=1800" in argv
    assert "TimeoutStopSec=600" in argv, "a SIGTERM has time to finish the drained window's cleanup"
    unit = next(a for a in argv if a.startswith("--unit=")).split("=", 1)[1]
    assert unit.startswith("openclaw-config-watch-")
    assert argv[-5:] == ["/opt/bin/ai-resources", "openclaw", "config-watch", "--run-id", unit.rsplit("-", 1)[1]]
    cw = s.openclaw.config_watch
    assert cw["unit"] == unit and cw["done"] is False and cw["changes"]
    paths = {".".join(c["path"]) for c in cw["changes"]}
    assert "gateway.bind" in paths and "tools.profile" in paths, "hot and restart-required changes are both recorded"
    msgs = " ".join(m for _l, m in script.log)
    assert f"journalctl --user -u {unit}" in msgs and f"systemctl --user stop {unit}" in msgs


def test_the_run_id_is_on_disk_before_the_unit_starts(sim, script, ai_resources_on_path, monkeypatch):
    real = sim.systemd
    seen = []

    def spy(argv, **kw):
        if argv[0] == "systemd-run":
            cw = state.load().openclaw.config_watch
            seen.append(cw and cw.get("run_id"))
        return real(argv, **kw)

    monkeypatch.setattr(section, "_runner", lambda: spy)
    s = _state()
    _run_wizard(s)
    assert seen and seen[0] == s.openclaw.config_watch["run_id"]


def test_sigterm_becomes_an_exception_so_the_window_cleans_up(monkeypatch):
    import argparse
    import signal

    installed = {}
    monkeypatch.setattr(signal, "signal", lambda sig, h: installed.__setitem__(sig, h))
    monkeypatch.setattr(host, "watch_config", lambda *a, **k: 0)
    host.cmd_config_watch(argparse.Namespace(run_id="r1", window=600, interval=30))
    with pytest.raises(SystemExit):
        installed[signal.SIGTERM](signal.SIGTERM, None)


def test_stop_config_watch_leaves_a_watch_inside_its_window_alone(sim, tmp_path, monkeypatch):
    marker = host.Marker(tmp_path / "watchdog.off")
    monkeypatch.setattr(host, "Marker", lambda *a, **k: marker)
    marker.touch()
    sim.systemd.calls.clear()
    section.stop_config_watch()
    assert not [c for c in sim.systemd.calls if c[:3] == ["systemctl", "--user", "stop"]], "no systemctl stop while watchdog.off exists"
    marker.remove()
    section.stop_config_watch()
    assert any(c[:3] == ["systemctl", "--user", "stop"] for c in sim.systemd.calls)


def test_a_failing_systemd_run_prints_a_skip_and_setup_continues(sim, script, ai_resources_on_path, monkeypatch):
    real = sim.systemd

    def failing(argv, **kw):
        if argv[0] == "systemd-run":
            return 1, "Failed to connect to bus"
        return real(argv, **kw)

    monkeypatch.setattr(section, "_runner", lambda: failing)
    s = _state()
    _run_wizard(s)
    assert any("post-setup watch not started" in m for m in script.messages("warn"))
    assert s.openclaw.config_watch is None
    assert json.loads(sim.cfg.read_text())["tools"]["profile"] == "coding", "the config was written all the same"


def test_a_run_that_writes_nothing_starts_no_watch_and_leaves_the_previous_one(sim, script, ai_resources_on_path):
    s = _state()
    _run_wizard(s)
    first = dict(s.openclaw.config_watch)
    sim.systemd.calls.clear()
    _run_wizard(s)
    assert _launches(sim) == [] and s.openclaw.config_watch == first
    assert not any(c[:3] == ["systemctl", "--user", "stop"] and "openclaw-config-watch-*" in c for c in sim.systemd.calls)


def test_a_new_run_stops_the_previous_watch_before_its_first_write(sim, script, ai_resources_on_path, monkeypatch):
    s = _state()
    _run_wizard(s)
    old = s.openclaw.config_watch["run_id"]
    # Another write is due: put the config back so setup has something to do again.
    sim.cfg.write_bytes(sim.original_config)
    s.openclaw.host_config_changes = []
    order: list[str] = []
    real_oc, real_sd = sim.oc.__call__, sim.systemd.__call__

    def oc(args, stdin=None, timeout=120):
        if args[:2] == ["config", "patch"] and "--dry-run" not in args:
            order.append("write")
        return real_oc(args, stdin=stdin, timeout=timeout)

    def sd(argv, **kw):
        if argv[:3] == ["systemctl", "--user", "stop"] and "openclaw-config-watch-*" in argv:
            order.append("stop-watch")
        return real_sd(argv, **kw)

    monkeypatch.setattr(openclaw, "_openclaw", oc)
    monkeypatch.setattr(section, "_runner", lambda: sd)
    _run_wizard(s)
    assert order and order[0] == "stop-watch" and "write" in order
    assert s.openclaw.config_watch["run_id"] != old


def test_teardown_cancels_the_watch_before_restoring(sim, script, ai_resources_on_path):
    s = _state()
    _run_wizard(s)
    sim.systemd.calls.clear()
    openclaw.teardown(s)
    assert ["systemctl", "--user", "stop", "openclaw-config-watch-*"] in sim.systemd.calls
    assert s.openclaw.config_watch is None


def test_the_config_watch_verb_is_registered():
    import argparse
    root = argparse.ArgumentParser()
    host.add_subparser(root.add_subparsers())
    ns = root.parse_args(["openclaw", "config-watch", "--run-id", "abc"])
    assert ns.run_id == "abc" and ns.func is host.cmd_config_watch
    ns = root.parse_args(["openclaw", "apply-pending", "--yes"])
    assert ns.yes is True and ns.func is host.cmd_apply_pending


def test_restore_bindings_survives_a_restart_required_refusal():
    o = state.SetupState().openclaw
    o.bindings_applied, o.bindings_previous = True, [{"agentId": "main"}]

    def refuse(*a, **k):
        raise openclaw.RestartRequired(["bindings"])

    assert section._restore_bindings(o, {"bindings": []}, refuse) is False
    assert o.bindings_applied is True, "nothing was restored, so the record stays"


# --- E2: the watch needs a baseline ------------------------------------------------------------------------------------

def test_a_failing_baseline_never_reverts_notifies_once_and_exits_zero(rig):
    r = rig([HOT, BIND])
    r.s.openclaw.config_watch["baseline_healthy"] = False
    assert r.run() == 0
    assert r.real_writes() == [] and r.systemctl_changes() == []
    assert len(r.notes) == 1 and "already failing" in r.notes[0]
    assert r.s.openclaw.config_watch["done"] is True
    assert r.s.openclaw.config_watch["result"]["skipped"] == "baseline-unhealthy"


def test_a_healthy_baseline_still_reverts(rig):
    r = rig([HOT])
    r.s.openclaw.config_watch["baseline_healthy"] = True
    assert r.run() == 0
    assert len(r.real_writes()) == 1


def test_an_unchanged_config_sha_starts_no_systemd_run(sim, script, ai_resources_on_path):
    s = _state()
    sim.systemd.calls.clear()
    path = sim.cfg
    baseline = {"sha": __import__("hashlib").sha256(path.read_bytes()).hexdigest(), "healthy": True}
    started = section.start_config_watch(s, [dict(HOT)], config_path=path, baseline=baseline)
    assert started is False and s.openclaw.config_watch is None
    assert not [c for c in sim.systemd.calls if c[0] == "systemd-run"]
    assert any("byte-identical" in m for m in script.messages("info"))


def test_the_wizard_stores_the_baseline_health_it_measured_before_writing(sim, script, ai_resources_on_path, monkeypatch):
    real = sim.oc
    order = []

    def oc(args, stdin=None, timeout=120):
        if args[:1] == ["health"]:
            order.append("health")
            return 1, "down"
        if args[:2] == ["config", "patch"] and "--dry-run" not in args:
            order.append("write")
        return real(args, stdin=stdin, timeout=timeout)

    monkeypatch.setattr(openclaw, "_openclaw", oc)
    s = _state()
    _run_wizard(s)
    assert order and order[0] == "health" and "write" in order
    assert s.openclaw.config_watch["baseline_healthy"] is False


# ======================================================================================================================
# 2.1.1: the watch must not mistake "I could not probe" for "the gateway failed" (D1, 2026-10-10)
# ======================================================================================================================

import datetime as _dt
from pathlib import Path as _Path

FIXTURES = _Path(__file__).parent / "fixtures" / "openclaw" / "watch-2026-10-10"
WALL0 = _dt.datetime(2026, 10, 10, 15, 33, 47, tzinfo=_dt.timezone.utc).timestamp()
NODE_MISSING = "env: 'node': No such file or directory\nenv: use -[v]S to pass options in shebang lines"

# the two records the 2026-10-10 15:32 run wrote (tests/fixtures/openclaw/watch-2026-10-10/setup-state-excerpt.yaml)
TIMEOUT_KEY = {"path": ["agents", "defaults", "timeoutSeconds"], "previous": None, "had": False, "action": "filled",
               "delete": ["agents", "defaults", "timeoutSeconds"]}
IDLE_KEY = {"path": ["mcp", "sessionIdleTtlMs"], "previous": None, "had": False, "action": "filled",
            "delete": ["mcp", "sessionIdleTtlMs"]}
D1_CHANGES = [TIMEOUT_KEY, IDLE_KEY]
POLICY = dict(settle_s=120.0, sustain_s=300.0, need_corroboration=True)


def _journal_line(epoch: float, message: str) -> str:
    stamp = _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return f"{stamp} bithome node[1]: {stamp} [reload] {message}"


class WRig(Rig):
    """A Rig with a virtual clock, scripted probes by time, corroboration signals and a gateway journal."""

    def __init__(self, tmp_path, changes, *, probe=None, version=(0, f"OpenClaw {_rr.PINNED_OPENCLAW_VERSION} (abc)"), pid=lambda t: "100",
                 restarts=lambda t: "0", unit_state=lambda t: "active", listening=lambda t: True,
                 journal=(), patch_ok=True, **kw):
        super().__init__(tmp_path, changes, **kw)
        self.now = 0.0
        self.probe = probe or (lambda t: (1, ""))
        self.version = version
        self.pid, self.restarts, self.unit_state, self.listening = pid, restarts, unit_state, listening
        self.journal = list(journal)               # (epoch, message) pairs, visible once the wall clock reaches them
        self.patch_ok = patch_ok
        self.probes: list[tuple[float, int]] = []
        self.health_timeouts: list = []
        self.commands: list[list[str]] = []

    def clock(self):
        return self.now

    def wall(self):
        return WALL0 + self.now

    def sleep(self, s):
        super().sleep(s)
        self.now += s

    def __call__(self, argv, **kw):
        self.commands.append(list(argv))
        if argv[:3] == ["systemctl", "--user", "show"] and "MainPID,NRestarts,ActiveState" in argv:
            return 0, (f"MainPID={self.pid(self.now)}\nNRestarts={self.restarts(self.now)}\n"
                       f"ActiveState={self.unit_state(self.now)}")
        if argv[:1] == ["journalctl"]:
            return 0, "\n".join(_journal_line(e, m) for e, m in self.journal if e <= self.wall())
        if argv[:1] == ["ss"]:
            return 0, ("LISTEN 0 511 127.0.0.1:18789 0.0.0.0:*" if self.listening(self.now) else "")
        if argv[-1:] == ["--version"]:
            return self.version
        if argv[-1:] == ["health"]:
            self.health_calls += 1
            self.health_timeouts.append(kw.get("timeout"))
            self.events.append(("health",))
            rc, text = self.probe(self.now)
            self.probes.append((self.now, rc))
            return rc, text
        return super().__call__(argv, **kw)

    def apply_patch(self, patch, *, dry_run=False, replace_paths=None, allow_restart=False, reload_mode=None):
        if not self.patch_ok:
            self.applied.append((patch, dry_run, replace_paths, allow_restart))
            return False, "openclaw not found on PATH"
        return super().apply_patch(patch, dry_run=dry_run, replace_paths=replace_paths,
                                   allow_restart=allow_restart, reload_mode=reload_mode)

    def run(self, **kw):
        kw.setdefault("window", 900)
        kw.setdefault("interval", 30)
        for k, v in POLICY.items():
            kw.setdefault(k, v)
        return host.watch_config(self.s.openclaw.config_watch["run_id"], runner=self, sleep=self.sleep,
                                 load=lambda: self.s, save=lambda s: setattr(self, "saves", self.saves + 1),
                                 apply_patch=self.apply_patch, notify=lambda m: self.notes.append(m) or True,
                                 marker=self.marker, reload_mode=lambda: None, out=self.out.append,
                                 drain_interval=1, health_interval=1, clock=self.clock, wall=self.wall, **kw)

    def result(self):
        return self.s.openclaw.config_watch["result"]


@pytest.fixture
def wrig(tmp_path):
    def make(changes, **kw):
        return WRig(tmp_path, changes, **kw)
    return make


def _no_errors(o):
    from ai_resources.setup.cockpits import _openclaw_host as sec
    return [f for f in sec._pending_findings(o) if f.level == "error"]


# --- AC-D1-regression ---------------------------------------------------------------------------------------------

def test_the_2026_10_10_run_is_inconclusive_and_reverts_nothing(wrig):
    """Probes that cannot run (node not on the unit's PATH) once ended in exit 1 and two source=watch restores."""
    r = wrig(D1_CHANGES, probe=lambda t: (127, NODE_MISSING), patch_ok=False)
    assert r.run() == 0
    assert r.applied == [], "the revert path was never entered"
    assert r.s.openclaw.host_restart_pending == []
    assert r.result()["outcome"] == "inconclusive" and "could not run" in r.result()["reason"]
    assert r.s.openclaw.config_watch["done"] is True and r.systemctl_changes() == []
    assert len(r.notes) == 1 and "reverted nothing" in r.notes[0]
    assert _no_errors(r.s.openclaw) == []


def test_legacy_defaults_also_treat_an_unrunnable_probe_as_inconclusive(rig):
    r = rig(D1_CHANGES)
    r.health = []
    real = r.__call__

    def runner(argv, **kw):
        if argv[-1:] == ["health"]:
            return 127, NODE_MISSING
        return real(argv, **kw)

    out = host.watch_config("r1", runner=runner, sleep=r.sleep, load=lambda: r.s, save=lambda s: None,
                            apply_patch=r.apply_patch, notify=lambda m: r.notes.append(m) or True,
                            marker=r.marker, reload_mode=lambda: None, out=r.out.append, window=600, interval=30)
    assert out == 0 and r.real_writes() == [] and r.applied == []
    assert r.s.openclaw.host_restart_pending == []


def test_a_failing_version_check_ends_inconclusive_before_any_probe_is_counted(wrig):
    r = wrig(D1_CHANGES, version=(127, NODE_MISSING))
    assert r.run() == 0
    assert r.health_calls == 0 and r.applied == []
    assert r.result()["outcome"] == "inconclusive"


def test_probes_that_fail_with_a_live_unit_and_no_corroboration_are_inconclusive(wrig):
    r = wrig(D1_CHANGES, probe=lambda t: (1, "gateway closed"))          # same pid, port listening, no restart
    assert r.run() == 0
    assert r.applied == [] and r.s.openclaw.host_restart_pending == []
    assert r.result()["outcome"] == "inconclusive" and "corroborat" in r.result()["reason"]
    assert len(r.notes) == 1


def test_a_timeout_is_slow_not_unrunnable_and_is_never_reverted_without_corroboration(wrig):
    r = wrig(D1_CHANGES, probe=lambda t: (124, "timed out"))
    assert r.run() == 0
    assert r.applied == []
    assert "could not run" not in r.result()["reason"]
    assert r.health_calls > 3, "a timeout does not end the watch the way an unrunnable probe does"


def test_every_probe_leaves_one_journal_line(wrig):
    r = wrig(D1_CHANGES, probe=lambda t: (0, "ok"))
    r.run(window=90)
    lines = [m for m in r.out if m.startswith("tick=")]
    assert len(lines) == r.health_calls == 4
    assert lines[0] == "tick=0 rc=0 class=answered elapsed=0s" and lines[1].endswith("elapsed=30s")


# --- AC-D1-real-outage: a corroborated outage after the settle period is still reverted ------------------------------

def test_a_corroborated_sustained_outage_reverts_hot_keys_and_only_then(wrig):
    r = wrig([HOT, CREATED], probe=lambda t: (0, "") if t < 90 else (1, ""),
             pid=lambda t: "100" if t < 90 else "200")
    assert r.run() == 0
    [(patch, _d, _rp, allow)] = r.real_writes()
    assert patch == {"tools": {"profile": "minimal"}, "gateway": {"publicOrigin": None}} and allow is False
    assert r.systemctl_changes() == []
    assert r.result()["outcome"] == "reverted"
    revert_at = next(t for t, _rc in r.probes if t >= 90 + 300)
    assert r.now >= revert_at, "not before the failure was sustained for 5 minutes"
    assert r.now < 90 + 300 + 3 * 30


def test_a_restart_required_key_is_reverted_only_in_the_drained_window_after_a_corroborated_outage(wrig):
    r = wrig([HOT, BIND], probe=lambda t: (0, "") if t < 30 else (1, ""), pid=lambda t: "100" if t < 30 else "300")
    assert r.run() == 0
    assert ("patch", ("gateway.bind",), True) in r.events
    assert not any(e[0] == "patch" and e[2] is False and "gateway.bind" in e[1] for e in r.events)


def test_probes_are_ignored_until_the_settle_period_after_the_last_reload_has_passed(wrig):
    reloads = [(WALL0 + 100, "config hot reload applied (tools.profile)")]
    r = wrig([HOT], pid=lambda t: "100" if t < 1 else "200", journal=reloads)   # failing; the process changed at once
    assert r.run() == 0
    assert len(r.real_writes()) == 1
    # settle ends at 100 + 120; the first counted probe is at 240 and the failure must last 300 s from there
    assert r.now >= 240 + 300


def test_a_probe_answered_after_the_hot_reload_keeps_the_key_and_nothing_is_queued(wrig):
    applied = (WALL0 - 48, "config hot reload applied (agents.defaults.timeoutSeconds, mcp.sessionIdleTtlMs)")
    r = wrig(D1_CHANGES, probe=lambda t: (0, "") if t < 30 else (1, ""), pid=lambda t: "100" if t < 30 else "400",
             journal=[applied])
    assert r.run() == 0
    assert r.applied == [] and r.s.openclaw.host_restart_pending == []
    assert sorted(r.result()["kept_confirmed"]) == ["agents.defaults.timeoutSeconds", "mcp.sessionIdleTtlMs"]
    assert r.result()["outcome"] == "inconclusive"


def test_a_baseline_that_was_not_healthy_is_never_blamed_on_the_run(wrig):
    r = wrig([HOT, BIND], pid=lambda t: "9")
    r.s.openclaw.config_watch["baseline_healthy"] = False
    assert r.run() == 0
    assert r.applied == [] and r.result()["outcome"] == "baseline-unhealthy"
    assert r.result()["skipped"] == "baseline-unhealthy" and "already failing" in r.notes[0]


def test_a_stop_in_the_journal_after_the_change_corroborates(wrig):
    r = wrig([HOT])                                   # same pid, port listening: only the journal speaks
    original = r.__call__

    def runner(argv, **kw):
        if argv[:1] == ["journalctl"]:
            stamp = _dt.datetime.fromtimestamp(WALL0 + 20, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
            return 0, f"{stamp} bithome systemd[1]: openclaw-gateway.service: Main process exited, code=killed"
        return original(argv, **kw)

    code = host.watch_config("r1", runner=runner, sleep=r.sleep, load=lambda: r.s, save=lambda s: None,
                             apply_patch=r.apply_patch, notify=lambda m: r.notes.append(m) or True, marker=r.marker,
                             reload_mode=lambda: None, out=r.out.append, window=900, interval=30,
                             clock=r.clock, wall=r.wall, drain_interval=1, health_interval=1, **POLICY)
    assert code == 0 and len(r.real_writes()) == 1


# --- AC-D1-exit7 ---------------------------------------------------------------------------------------------------

def test_a_rejected_dry_run_of_the_drained_window_queues_no_restore(wrig):
    r = wrig([BIND], pid=lambda t: "100" if t < 10 else "500", patch_ok=False)
    assert r.run() == 0
    assert r.s.openclaw.host_restart_pending == []
    assert r.systemctl_changes() == [], "nothing was stopped"
    assert r.result()["outcome"] == "inconclusive" and "problems" in r.result() and r.result()["problems"] == []
    assert _no_errors(r.s.openclaw) == []


def test_a_window_that_could_not_drain_queues_the_restore_as_a_corroborated_entry(wrig):
    r = wrig([BIND], pid=lambda t: "100" if t < 10 else "500", drain_ok=False)
    r.run(drain_timeout=3)
    [entry] = r.s.openclaw.host_restart_pending
    assert entry["source"] == "watch" and entry["corroborated"] is True
    assert r.result()["outcome"] == "revert-failed"


# --- 2.1.1 F5: the kill path and the failed-window queue rule ------------------------------------------------------

def test_sigterm_inside_the_drained_window_starts_the_gateway_again_and_queues_nothing(wrig, monkeypatch):
    import argparse
    import signal

    r = wrig([BIND], pid=lambda t: "100" if t < 10 else "500",
             probe=lambda t: (0, "") if any(c[:3] == ["systemctl", "--user", "start"] for c in r.commands)
             else (1, ""))
    installed = {}
    monkeypatch.setattr(signal, "signal", lambda sig, h: installed.__setitem__(sig, h))
    real = WRig.apply_patch

    def killed_while_applying(patch, *, dry_run=False, **kw):
        if not dry_run:
            installed[signal.SIGTERM](signal.SIGTERM, None)      # what systemd sends at TimeoutStopSec
        return real(r, patch, dry_run=dry_run, **kw)

    r.apply_patch = killed_while_applying
    original = host.watch_config

    def run_the_rig(*_a, **_k):
        monkeypatch.setattr(host, "watch_config", original)     # r.run() calls the real one
        return r.run()

    monkeypatch.setattr(host, "watch_config", run_the_rig)
    with pytest.raises(SystemExit) as exc:
        host.cmd_config_watch(argparse.Namespace(run_id="r1", window=900, interval=30))
    assert exc.value.code == 143
    assert [(e[0], e[1]) for e in r.systemctl_changes()] == [("stop", True), ("start", False)], \
        "the gateway was stopped inside the window and started again with watchdog.off already removed"
    assert not r.marker.exists()
    assert r.s.openclaw.host_restart_pending == [], "a killed watch queues no restore"
    assert r.s.openclaw.config_watch["done"] is False and r.s.openclaw.config_watch["result"] is None


def test_a_watch_left_unfinished_is_never_an_error():
    o = _o(config_watch={"run_id": "r1", "unit": "openclaw-config-watch-r1", "done": False, "result": None,
                         "changes": [BIND]})
    assert [lv for lv, _m in _levels(o)] == []
    assert host.pending_summary(o) == {}, "not finished: it claims no revert and no failure"
    assert not any("reverted" in line or "could not finish" in line
                   for line in host.pending_lines(host.pending_summary(o)))


def _failing_window(wrig, *, patch_ok=True, marker_opens=False, healthy_after_start=True, **kw):
    holder = {}

    def probe(t):
        started = any(c[:3] == ["systemctl", "--user", "start"] for c in holder["r"].commands)
        return (0, "") if (started and healthy_after_start) or t < 10 else (1, "")

    r = holder["r"] = wrig([BIND], probe=probe, pid=lambda t: "100" if t < 10 else "500", **kw)
    real = WRig.apply_patch

    def patch(p, *, dry_run=False, **k):
        if dry_run:
            if marker_opens:
                r.marker.touch()              # another window opened between the dry run and ours
            return real(r, p, dry_run=True, **k)
        return (True, "ok") if patch_ok else (False, "boom")

    r.apply_patch = patch
    return r


@pytest.mark.parametrize("label,kw,queued,problem", [
    ("exit 2: another window is open", dict(marker_opens=True), True,
     "restart-required keys not reverted (drained window exit 2)"),
    ("exit 4: the patch failed inside the window", dict(patch_ok=False), True,
     "restart-required keys not reverted (drained window exit 4)"),
    # Exit 5 happens after the write: nothing was left unreverted, so there is no problem to report.
    ("exit 5: the patch was written, the gateway did not come back", dict(healthy_after_start=False), False, None),
])
def test_a_failed_drained_window_queues_a_restore_only_when_the_write_did_not_happen(wrig, label, kw, queued,
                                                                                    problem):
    r = _failing_window(wrig, **kw)
    r.run(drain_timeout=3, health_timeout=3)
    pending = r.s.openclaw.host_restart_pending
    assert bool(pending) is queued, label
    if queued:
        [entry] = pending
        assert entry["source"] == "watch" and entry["corroborated"] is True
        assert r.result()["outcome"] == "revert-failed"
        assert r.result()["problems"] == [problem], "the exit code of the drained window is carried verbatim"
    else:
        assert r.result()["outcome"] == "reverted"
        assert r.result()["problems"] == []


def test_an_uncorroborated_failure_never_reaches_the_window_and_queues_nothing(wrig):
    r = wrig([BIND], probe=lambda t: (1, ""))        # same MainPID, active, listening: nothing corroborates
    assert r.run() == 0
    assert r.s.openclaw.host_restart_pending == [] and r.systemctl_changes() == []
    assert r.result()["outcome"] == "inconclusive"


# --- AC-D1-routing -------------------------------------------------------------------------------------------------

def _recorded(path, kind=None, version=_rr.PINNED_OPENCLAW_VERSION):
    rec = {"path": path, "previous": None, "had": False, "action": "filled", "delete": path}
    if kind:
        rec.update(kind=kind, openclaw_version=version)
    return rec


def test_recorded_hot_keys_stay_on_the_hot_path_when_the_version_cannot_be_read(wrig, monkeypatch):
    monkeypatch.setattr(host.openclaw_reload_rules, "installed_openclaw_version", lambda runner, **kw: None)
    r = wrig([_recorded(["agents", "defaults", "timeoutSeconds"], "hot"),
              _recorded(["mcp", "sessionIdleTtlMs"], "hot")], pid=lambda t: "100" if t < 10 else "9")
    assert r.run() == 0
    [(patch, _d, _rp, allow)] = r.real_writes()
    assert allow is False and r.systemctl_changes() == [], "no drained window for a hot key"
    assert r.result()["outcome"] == "reverted"


def test_a_legacy_record_with_no_version_still_routes_to_the_drained_window(wrig, monkeypatch):
    monkeypatch.setattr(host.openclaw_reload_rules, "installed_openclaw_version", lambda runner, **kw: None)
    r = wrig([_recorded(["tools", "profile"])], pid=lambda t: "100" if t < 10 else "9")
    assert r.run() == 0
    assert ("patch", ("tools.profile",), True) in r.events, "fail closed: restart routing"
    assert [e[0] for e in r.systemctl_changes()] == ["stop", "start"]


def test_a_different_readable_version_reclassifies_instead_of_trusting_the_record(wrig, monkeypatch):
    monkeypatch.setattr(host.openclaw_reload_rules, "installed_openclaw_version", lambda runner, **kw: "2099.1.1")
    r = wrig([_recorded(["tools", "profile"], "hot", _rr.PINNED_OPENCLAW_VERSION)], pid=lambda t: "100" if t < 10 else "9")
    assert r.run() == 0
    assert ("patch", ("tools.profile",), True) in r.events, "the pin does not know 2099.1.1: fail closed"


# --- the pure verdict ------------------------------------------------------------------------------------------------

def _p(t, cls):
    return host.Probe(t, cls, {"answered": 0, "no-answer": 1, "slow": 124, "cannot-probe": 127}[cls], "")


@pytest.mark.parametrize("probes,kw,kind,why", [
    ([_p(0, "answered"), _p(30, "answered")], {}, "healthy", ""),
    ([_p(0, "no-answer"), _p(30, "no-answer")], dict(sustain_s=300), "watching", ""),
    ([_p(0, "no-answer"), _p(300, "no-answer")], dict(sustain_s=300, need_corroboration=True), "inconclusive",
     "corroborat"),
    ([_p(0, "no-answer"), _p(300, "slow")], dict(sustain_s=300, need_corroboration=True, corroborated=True),
     "failing", ""),
    ([_p(0, "no-answer"), _p(300, "no-answer")], dict(sustain_s=300, need_corroboration=True, corroborated=True,
                                                      baseline_healthy=False), "inconclusive", "baseline-unhealthy"),
    ([_p(0, "cannot-probe")], {}, "inconclusive", "could not run"),
    ([_p(0, "no-answer"), _p(30, "answered"), _p(60, "no-answer"), _p(90, "no-answer")],
     dict(sustain_s=300), "watching", ""),
    ([_p(i * 30, "no-answer") for i in range(3)], {}, "failing", ""),                    # legacy: three in a row
    ([_p(0, "no-answer"), _p(130, "no-answer"), _p(430, "no-answer")],
     dict(settle_until=120, sustain_s=300, need_corroboration=True, corroborated=True), "failing", ""),
    ([_p(0, "no-answer"), _p(130, "no-answer"), _p(300, "no-answer")],
     dict(settle_until=120, sustain_s=300, need_corroboration=True, corroborated=True), "watching", ""),
])
def test_judge_table(probes, kw, kind, why):
    v = host.judge(probes, **kw)
    assert v.kind == kind and why in v.reason


@pytest.mark.parametrize("rc,text,cls", [
    (0, "ok", "answered"), (1, "gateway closed (1006)", "no-answer"), (124, "Command timed out", "slow"),
    (127, "x", "cannot-probe"), (126, "", "cannot-probe"), (1, NODE_MISSING, "cannot-probe"),
    (1, "openclaw not found on PATH", "cannot-probe"), (1, "ECONNREFUSED 127.0.0.1:18789", "no-answer"),
])
def test_probe_classes(rc, text, cls):
    assert host.classify_probe(rc, text) == cls


# --- the reload-line parser, on the lines 2026.9.9 really wrote -------------------------------------------------------

def test_reload_events_come_from_the_recorded_gateway_journal():
    lines = (FIXTURES / "gateway-reload.journal").read_text(encoding="utf-8").splitlines()
    paths = ["agents.defaults.timeoutSeconds", "mcp.sessionIdleTtlMs"]
    ev = host.last_reload_event(lines, paths)
    assert ev is not None and ev.applied is True and sorted(ev.paths) == paths
    assert _dt.datetime.fromtimestamp(ev.epoch, tz=_dt.timezone.utc).isoformat() == "2026-10-10T15:32:59+00:00"
    assert host.applied_paths(lines, paths) == {p: ev.epoch for p in paths}
    # another key's reload and the superseded line name no recorded path
    assert host.last_reload_event(lines, ["gateway.bind"]) is None


def test_a_disposal_failure_and_a_superseded_reload_are_not_failures():
    lines = (FIXTURES / "gateway-reload.journal").read_text(encoding="utf-8").splitlines()
    kinds = [(e.applied, e.paths) for e in host.parse_reload_events(lines)]
    assert (False, ["tools.media.models"]) in kinds and (True, ["tools.media.models"]) in kinds
    assert all("disposal" not in str(k) for k in kinds)
    assert host.journal_shows_crash(lines, since=0) is False


def test_an_unparseable_journal_degrades_to_nothing():
    assert host.parse_reload_events(["garbage", "", "[reload] config hot reload applied (a.b)"]) == []


# --- the unit's own environment (primary fix) ------------------------------------------------------------------------

def test_the_unit_path_has_openclaw_and_node_and_never_an_ephemeral_dir(tmp_path, monkeypatch):
    brew = tmp_path / "brew" / "bin"
    brew.mkdir(parents=True)
    (brew / "openclaw").write_text("#!/usr/bin/env node\n")
    (brew / "node").write_text("")
    monkeypatch.setattr(host, "read_host_env", lambda *a, **k: {"OPENCLAW_EXTRA_PATH": "/run/user/1000/fnm_multishells/1/bin"})
    path = host.unit_path(openclaw=str(brew / "openclaw"), which=lambda n: None)
    parts = path.split(":")
    assert parts[0] == str(brew), "the dir of the resolved openclaw comes first, node sits next to it"
    assert not any("multishell" in p or p.startswith("/run/user/") for p in parts)
    assert parts[-3:] == ["/usr/local/bin", "/usr/bin", "/bin"]
    assert len(parts) == len(set(parts))


def test_the_unit_path_honours_the_operator_extra_path_first(tmp_path, monkeypatch):
    monkeypatch.setattr(host, "read_host_env", lambda *a, **k: {"OPENCLAW_EXTRA_PATH": "/opt/mine/bin"})
    monkeypatch.delenv("OPENCLAW_EXTRA_PATH", raising=False)
    path = host.unit_path(openclaw="/home/linuxbrew/.linuxbrew/bin/openclaw", which=lambda n: None)
    assert path.split(":")[0] == "/opt/mine/bin"


def test_the_config_watch_verb_runs_with_the_unit_path_and_restores_the_environment(monkeypatch):
    import argparse
    import os
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(host, "unit_path", lambda **k: "/x/brew/bin:/usr/bin")
    seen = {}

    def stub(*a, **k):
        seen["path"] = os.environ["PATH"]
        seen["kw"] = k
        return 0

    monkeypatch.setattr(host, "watch_config", stub)
    monkeypatch.setattr("signal.signal", lambda *a: None)
    host.cmd_config_watch(argparse.Namespace(run_id="r1", window=900, interval=30))
    assert seen["path"].split(":")[:2] == ["/x/brew/bin", "/usr/bin"]
    # the post-setup unit must run with the Q1 policy, not the legacy "3 failures in a row" rule
    assert seen["kw"]["settle_s"] == 120.0
    assert seen["kw"]["sustain_s"] == 300.0
    assert seen["kw"]["need_corroboration"] is True
    assert host.WATCH_POLICY == {"settle_s": 120, "sustain_s": 300, "need_corroboration": True}
    assert host.HEALTH_PROBE_TIMEOUT_S == 60
    assert os.environ["PATH"] == "/usr/bin:/bin"


def test_setup_starts_the_unit_with_its_own_path_and_records_version_mode_and_kind(sim, script, monkeypatch,
                                                                                    ai_resources_on_path):
    monkeypatch.setattr(host, "unit_path", lambda **k: "/home/linuxbrew/.linuxbrew/bin:/usr/bin:/bin")
    s = _state()
    _run_wizard(s)
    [argv] = _launches(sim)
    setenv = [a for a in argv if a.startswith("--setenv=PATH=")]
    assert setenv == ["--setenv=PATH=/home/linuxbrew/.linuxbrew/bin:/usr/bin:/bin"]
    assert argv.index(setenv[0]) < argv.index("/opt/bin/ai-resources")
    cw = s.openclaw.config_watch
    assert cw["openclaw_version"] == _rr.PINNED_OPENCLAW_VERSION
    assert "reload_mode" in cw
    kinds = {".".join(c["path"]): c.get("kind") for c in cw["changes"]}
    assert kinds["tools.profile"] == "hot" and kinds["gateway.bind"] == "restart"
    assert all(c.get("openclaw_version") == _rr.PINNED_OPENCLAW_VERSION for c in cw["changes"])


# --- AC-D1-report: inconclusive and leftover entries are warnings, never errors -------------------------------------

def _o(**kw):
    o = state.SetupState().openclaw
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def _levels(o):
    from ai_resources.setup.cockpits import _openclaw_host as sec
    return [(f.level, f.message) for f in sec._pending_findings(o)]


def test_an_inconclusive_watch_is_reported_as_a_warning_with_its_reason():
    o = _o(config_watch={"run_id": "r", "done": True, "changes": [], "result": {
        "outcome": "inconclusive", "reason": "probe could not run: env: 'node'", "hot": [], "restart": [],
        "problems": [], "kept_confirmed": []}})
    levels = _levels(o)
    assert [lv for lv, _m in levels] == ["warn"]
    assert "inconclusive" in levels[0][1] and "nothing was reverted or queued" in levels[0][1]
    assert any("inconclusive" in line for line in host.pending_lines(host.pending_summary(o)))


def test_a_failed_revert_stays_an_error():
    o = _o(config_watch={"run_id": "r", "done": True, "changes": [], "result": {
        "outcome": "revert-failed", "hot": [], "restart": [], "problems": ["hot keys not reverted: x"]}})
    assert [lv for lv, _m in _levels(o)] == ["error"]


def test_a_leftover_2_1_0_watch_restore_is_a_warning_naming_the_discard_command():
    entry = {"op": "restore", "path": "mcp.sessionIdleTtlMs", "source": "watch", "change": IDLE_KEY}
    o = _o(host_restart_pending=[entry])
    levels = _levels(o)
    assert all(lv == "warn" for lv, _m in levels)
    text = " ".join(m for _lv, m in levels)
    assert "likely false positive" in text
    summary = host.pending_summary(o)
    assert summary["leftover_watch"] == ["mcp.sessionIdleTtlMs"]
    assert any("--discard-watch-restores" in line for line in host.pending_lines(summary))


def test_discard_dry_run_changes_nothing_and_confirm_removes_only_watch_entries(tmp_path):
    keep = {"op": "profile", "path": "gateway.bind", "action": "forced"}
    leftovers = [{"op": "restore", "path": "mcp.sessionIdleTtlMs", "source": "watch", "change": IDLE_KEY},
                 {"op": "restore", "path": "agents.defaults.timeoutSeconds", "source": "watch", "change": TIMEOUT_KEY}]
    o = _o(host_restart_pending=[keep, *leftovers])
    lines: list[str] = []
    dropped = host.discard_watch_restores(o, apply=False, out=lines.append)
    assert dropped == ["agents.defaults.timeoutSeconds", "mcp.sessionIdleTtlMs"]
    assert len(o.host_restart_pending) == 3, "a dry run mutates nothing"
    assert any("dry run" in line for line in lines)
    host.discard_watch_restores(o, apply=True, out=lines.append)
    assert o.host_restart_pending == [keep]


def test_apply_pending_never_applies_a_leftover_watch_restore_that_would_change_the_config(sim, script):
    doc = json.loads(sim.cfg.read_text())
    doc.setdefault("mcp", {})["sessionIdleTtlMs"] = 1800000      # the live guard the 2.1.0 watch wanted removed
    sim.cfg.write_text(json.dumps(doc))
    s = _state()
    entry = {"op": "restore", "path": "mcp.sessionIdleTtlMs", "source": "watch", "change": dict(IDLE_KEY)}
    s.openclaw.host_restart_pending = [entry]
    sim.systemd.calls.clear()
    assert section.apply_pending(s, assume_yes=True) == 0
    assert s.openclaw.host_restart_pending == [entry], "never dropped silently"
    assert not [c for c in sim.systemd.calls if c[:3] == ["systemctl", "--user", "stop"]]
    assert json.loads(sim.cfg.read_text())["mcp"]["sessionIdleTtlMs"] == 1800000
    assert any("likely false positives" in m for m in script.messages("warn"))


def test_every_health_probe_in_a_watch_run_uses_the_sixty_second_timeout(wrig):
    r = wrig(D1_CHANGES, probe=lambda t: (1, "gateway closed"))
    assert r.run() == 0
    assert r.health_timeouts and all(t == 60 for t in r.health_timeouts)


# --- G1: a patch with no changed leaf is never sent and never cancels the previous watch ------------------------------

def _voice_run(sim, s):
    """One `configure()` pass that only the voice-notes section can write; returns (real patches, stops)."""
    sim.oc.calls.clear()
    sim.systemd.calls.clear()
    openclaw.configure({"state": s, "dry_run": False})
    stops = [c for c in sim.systemd.calls if c[:3] == ["systemctl", "--user", "stop"] and "openclaw-config-watch-*" in c]
    return sim.oc.real_patches(), stops


def test_a_second_run_with_the_same_voice_patch_sends_nothing_and_keeps_the_watch(sim, script, ai_resources_on_path):
    s = _state()
    _run_wizard(s)                                     # run 1: the profile, plus its watch
    s.openclaw.voice = "agy"
    patches, _stops = _voice_run(sim, s)               # a real voice change goes out as before
    assert [p for _a, p in patches if "tools" in p and "media" in p["tools"]], "the first voice write is sent"

    watch = {"run_id": "r1", "unit": "openclaw-config-watch-r1", "started_at": "", "changes": [dict(HOT)],
             "done": False, "result": None}
    s.openclaw.config_watch = dict(watch)              # run 1's safety net, still inside its window
    patches, stops = _voice_run(sim, s)                # run 2: the identical media patch
    assert patches == [], "a patch with no changed leaf must not be sent"
    assert stops == [] and s.openclaw.config_watch == watch, "and it must not cancel the watch of run 1"


def test_a_real_voice_change_still_supersedes_the_previous_watch(sim, script, ai_resources_on_path):
    s = _state()
    s.openclaw.voice = "agy"
    _voice_run(sim, s)
    s.openclaw.config_watch = {"run_id": "r1", "unit": "openclaw-config-watch-r1", "started_at": "",
                               "changes": [dict(HOT)], "done": False, "result": None}
    s.openclaw.voice_language = "es"                   # a different transcription language: a real change
    patches, stops = _voice_run(sim, s)
    assert patches and stops, "a patch that changes a leaf is written and supersedes the previous watch"
    assert s.openclaw.config_watch is None


def test_apply_patch_skips_a_noop_and_fires_the_first_write_hook_only_for_a_real_write(sim, monkeypatch):
    fired: list[int] = []
    doc = json.loads(sim.cfg.read_text())
    current = doc["tools"]["profile"]
    monkeypatch.setattr(openclaw, "_before_first_write", lambda: fired.append(1))
    ok, _out = openclaw.apply_patch({"tools": {"profile": current}})
    assert ok and fired == [] and sim.oc.real_patches() == []
    ok, _out = openclaw.apply_patch({"tools": {"profile": current + "-changed"}})
    assert ok and fired == [1] and len(sim.oc.real_patches()) == 1


# --- G2: discarding the watch restores also closes a legacy 2.1.0 failure record ----------------------------------------

def _legacy_failed_watch_state():
    entry = {"op": "restore", "path": "mcp.sessionIdleTtlMs", "source": "watch", "change": dict(IDLE_KEY)}
    return _o(host_restart_pending=[entry],
              config_watch={"run_id": "old", "unit": "openclaw-config-watch-old", "done": True,
                            "result": {"hot": [], "restart": [], "problems": ["restart-required not reverted: x"]}})


def test_a_legacy_failed_watch_is_still_an_error_until_the_restores_are_discarded():
    o = _legacy_failed_watch_state()
    assert [m for lv, m in _levels(o) if lv == "error" and "could not finish its revert" in m]
    host.discard_watch_restores(o, apply=False, out=lambda _m: None)
    assert [m for lv, m in _levels(o) if lv == "error" and "could not finish its revert" in m], \
        "a dry run changes nothing"


def test_discarding_the_watch_restores_closes_the_legacy_watch_result_without_an_error():
    o = _legacy_failed_watch_state()
    host.discard_watch_restores(o, apply=True, out=lambda _m: None)
    assert o.host_restart_pending == []
    r = o.config_watch["result"]
    assert r["outcome"] == "inconclusive" and r["reason"] == "discarded by the operator" and r["problems"] == []
    levels = _levels(o)
    assert not [lv for lv, _m in levels if lv == "error"]
    assert "could not finish its revert" not in " ".join(host.pending_lines(host.pending_summary(o)))


def test_discard_leaves_a_2_1_1_failed_revert_record_alone():
    o = _o(host_restart_pending=[{"op": "restore", "path": "gateway.bind", "source": "watch", "corroborated": True,
                                  "change": dict(BIND)}],
           config_watch={"run_id": "n", "unit": "u", "done": True,
                         "result": {"outcome": "revert-failed", "hot": [], "restart": [],
                                    "problems": ["drained window exit 2"]}})
    host.discard_watch_restores(o, apply=True, out=lambda _m: None)
    assert o.config_watch["result"]["problems"] == ["drained window exit 2"]
    assert [lv for lv, _m in _levels(o) if lv == "error"]


def test_discard_without_yes_and_without_a_terminal_is_one_line_and_changes_nothing(monkeypatch, capsys):
    from ai_resources.setup import ui

    def eof(*_a, **_k):
        raise EOFError

    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    monkeypatch.setattr(ui, "confirm", eof)
    messages: list[str] = []
    monkeypatch.setattr(ui, "info", messages.append)
    monkeypatch.setattr(ui, "error", messages.append)
    s = state.SetupState()
    entry = {"op": "restore", "path": "mcp.sessionIdleTtlMs", "source": "watch", "change": dict(IDLE_KEY)}
    s.openclaw.host_restart_pending = [entry]
    assert section.discard_watch_restores(s, assume_yes=False) == 1
    assert messages[-1] == "needs a terminal; re-run with --yes"
    assert s.openclaw.host_restart_pending == [entry]
    assert any("dry run" in m for m in messages), "the dry run is printed first"

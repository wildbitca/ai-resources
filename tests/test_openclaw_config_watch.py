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
            return 0, "OpenClaw 2026.9.9 (abc)"
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

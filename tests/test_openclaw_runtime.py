"""Gateway running on a deleted runtime binary (pitfall T57): the check reads a fake /proc tree, never the host."""
from __future__ import annotations

import os

from ai_resources import openclaw_runtime as rt
from test_openclaw_host_wizard import script, sim  # noqa: F401  (fixtures by name)
from test_openclaw_cockpit import jarvis  # noqa: F401  (fixture by name)

UNIT = "openclaw-gateway.service"


def _runner(pid):
    def run(argv, **_kw):
        return (0, f"{pid}\n") if pid is not None else (1, "")
    return run


def _proc(tmp_path, pid, target):
    d = tmp_path / str(pid)
    d.mkdir()
    os.symlink(target, d / "exe")
    return str(tmp_path)


def test_a_deleted_exe_is_a_warning_naming_the_drained_path(tmp_path):
    root = _proc(tmp_path, 42, "/opt/node/26.12.1/bin/node (deleted)")
    found = rt.runtime_findings(_runner(42), UNIT, {}, proc_root=root)
    assert len(found) == 1 and found[0][0] == "warn"
    assert "/opt/node/26.12.1/bin/node" in found[0][1] and "ai-resources openclaw doctor" in found[0][2]


def test_an_exe_path_that_no_longer_exists_is_a_warning(tmp_path):
    root = _proc(tmp_path, 42, str(tmp_path / "gone" / "node"))
    assert rt.gateway_exe_state(_runner(42), UNIT, {}, proc_root=root)["state"] == "missing"
    assert rt.runtime_findings(_runner(42), UNIT, {}, proc_root=root)[0][0] == "warn"


def test_a_healthy_exe_path_reports_nothing(tmp_path):
    real = tmp_path / "node"
    real.write_text("")
    root = _proc(tmp_path, 42, str(real))
    assert rt.gateway_exe_state(_runner(42), UNIT, {}, proc_root=root)["state"] == "ok"
    assert rt.runtime_findings(_runner(42), UNIT, {}, proc_root=root) == []


def test_no_main_pid_reports_nothing(tmp_path):
    for pid in ("0", None):
        assert rt.gateway_exe_state(_runner(pid), UNIT, {}, proc_root=str(tmp_path))["state"] == "no-pid"
        assert rt.runtime_findings(_runner(pid), UNIT, {}, proc_root=str(tmp_path)) == []


def test_an_unreadable_link_is_unknown_not_a_fault(tmp_path):
    assert rt.runtime_findings(_runner(42), UNIT, {}, proc_root=str(tmp_path)) == []


# --- wiring: status and verify render the warning; a fake runner never reaches the host's /proc ----------------------------

def _fake_proc(tmp_path, monkeypatch, pid, target):
    root = tmp_path / "proc"
    root.mkdir()
    _proc(root, pid, target)
    monkeypatch.setattr(rt, "PROC_ROOT", str(root))


def test_the_default_proc_root_is_the_module_constant_not_the_host(tmp_path, monkeypatch):
    _fake_proc(tmp_path, monkeypatch, 77, "/opt/node/old/bin/node (deleted)")
    assert rt.gateway_exe_state(_runner(77), UNIT, {})["state"] == "deleted"
    monkeypatch.setattr(rt, "PROC_ROOT", str(tmp_path / "empty"))
    assert rt.gateway_exe_state(_runner(77), UNIT, {})["state"] == "unknown"


def test_cmd_status_prints_the_deleted_runtime_warning(tmp_path, monkeypatch, capsys):
    import argparse
    from ai_resources import openclaw_host as host
    _fake_proc(tmp_path, monkeypatch, 4242, "/opt/node/old/bin/node (deleted)")
    monkeypatch.setattr(host, "collect_status", lambda *a, **k: {"unit": UNIT})
    monkeypatch.setattr(host, "render_status", lambda r: "REPORT")
    monkeypatch.setattr(host, "recorded_selection", lambda: None)
    monkeypatch.setattr(host, "recorded_pending", lambda: {"paths": [], "watch": None})
    monkeypatch.setattr(host, "journal_findings", lambda *a, **k: [])

    def runner(argv, **_kw):
        return (0, "4242\n") if "MainPID" in argv else (1, "")

    assert host.cmd_status(argparse.Namespace(no_doctor=True), runner=runner) == 0
    out = capsys.readouterr().out
    assert "WARN  the gateway (pid 4242) runs on a runtime that was deleted: /opt/node/old/bin/node" in out
    assert "ai-resources openclaw doctor" in out


def test_verify_returns_the_deleted_runtime_warning(tmp_path, monkeypatch, sim, script):
    from test_openclaw_host_wizard import _configure, _state
    from test_verify_openclaw_host import HostRunner
    from ai_resources.setup.cockpits import openclaw
    _fake_proc(tmp_path, monkeypatch, 4242, "/opt/node/old/bin/node (deleted)")
    s = _state()
    _configure(s)
    base = HostRunner()

    def runner(argv, **kw):
        if argv[-3:] == ["-p", "MainPID", "--value"]:
            return 0, "4242"
        return base(argv, **kw)

    found = openclaw.verify({"state": s, "runner": runner})
    assert [f.level for f in found if "runs on a runtime that was deleted" in f.message] == ["warn"]

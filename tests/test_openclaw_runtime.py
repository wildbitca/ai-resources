"""Gateway running on a deleted runtime binary (pitfall T57): the check reads a fake /proc tree, never the host."""
from __future__ import annotations

import os

from ai_resources import openclaw_runtime as rt

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

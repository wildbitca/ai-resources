"""doctor: read-only count of MCP child processes under the gateway unit (FU-1, kit side)."""
from __future__ import annotations

import argparse
from pathlib import Path

from ai_resources import doctor
from ai_resources.setup import state, ui


def _tree(tmp_path: Path, cmdlines: list[str]):
    cg = tmp_path / "cg" / "user.slice" / "openclaw-gateway.service"
    cg.mkdir(parents=True)
    proc = tmp_path / "proc"
    pids = []
    for i, cmd in enumerate(cmdlines, start=100):
        d = proc / str(i)
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(cmd.replace(" ", "\0").encode() + b"\0")
        pids.append(str(i))
    (cg / "cgroup.procs").write_text("\n".join(pids) + "\n")
    return tmp_path / "cg", proc


def _runner(control_group: str, rc: int = 0):
    def run(argv, **_kw):
        return rc, f"ControlGroup={control_group}\n"
    return run


def _capture(monkeypatch):
    out = {"warn": [], "detail": [], "ok": []}
    monkeypatch.setattr(ui, "warn", lambda m: out["warn"].append(m))
    monkeypatch.setattr(ui, "detail", lambda m: out["detail"].append(m))
    monkeypatch.setattr(ui, "ok", lambda m: out["ok"].append(m))
    return out


def test_counts_children_by_server(tmp_path):
    cg, proc = _tree(tmp_path, ["node /x/supabase-mcp --a", "node /x/supabase-mcp", "engram mcp serve",
                                "bash -c sleep", "openclaw-gateway"])
    counts = doctor.count_mcp_children("/user.slice/openclaw-gateway.service", cgroup_root=cg, proc_root=proc)
    assert counts == {"supabase": 2, "engram": 1}


def test_warns_above_threshold_with_the_restart_remedy_and_kills_nothing(tmp_path, monkeypatch):
    out = _capture(monkeypatch)
    cg, proc = _tree(tmp_path, ["node supabase-mcp"] * 5)
    n = doctor._check_mcp_children(runner=_runner("/user.slice/openclaw-gateway.service"),
                                   cgroup_root=cg, proc_root=proc, threshold=4)
    assert n == 1
    assert any("5" in w and "supabase" in w for w in out["warn"])
    text = " ".join(out["detail"])
    assert "systemctl --user restart openclaw-gateway.service" in text
    assert "not proven" in text.lower()


def test_below_threshold_is_quiet(tmp_path, monkeypatch):
    out = _capture(monkeypatch)
    cg, proc = _tree(tmp_path, ["node supabase-mcp"] * 2)
    assert doctor._check_mcp_children(runner=_runner("/user.slice/openclaw-gateway.service"),
                                      cgroup_root=cg, proc_root=proc, threshold=4) == 0
    assert out["warn"] == []


def test_no_systemd_unit_degrades_without_error(tmp_path, monkeypatch):
    out = _capture(monkeypatch)
    assert doctor._check_mcp_children(runner=_runner("", rc=127), cgroup_root=tmp_path, proc_root=tmp_path) == 0
    assert out["warn"] == []


class _Console:
    def print(self, *a, **k): ...
    def rule(self, *a, **k): ...


def test_only_runs_when_lanes_are_enabled(monkeypatch):
    calls = []
    monkeypatch.setattr(doctor, "_check_mcp_children", lambda **k: calls.append(1) or 0)
    monkeypatch.setattr(ui, "require_deps", lambda: None)
    monkeypatch.setattr(ui, "console", lambda: _Console())
    s = state.SetupState()
    monkeypatch.setattr(state, "load", lambda: s)
    doctor.cmd_doctor(argparse.Namespace(skip_smoke=True))
    assert calls == []
    s.profile.lanes = {"agents": {"kit-lane-x": {"backend": "claude-cli"}}, "roles": {}}
    doctor.cmd_doctor(argparse.Namespace(skip_smoke=True))
    assert calls == [1]

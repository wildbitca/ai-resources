"""R5/R7: the gateway reconcile. A stale plugin (path, version or time rule) and an MCP-child overflow (216 against a
threshold of 24) are cleared by ONE drained restart that refuses while turns run; a clean gateway is a no-op; a
second run after the restart is a no-op; the live checks read the result back."""
from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import openclaw_host as host  # noqa: E402
from ai_resources import openclaw_reconcile as rec  # noqa: E402

CG = "/user.slice/openclaw-gateway.service"


def _tree(tmp_path, n_engram, n_supabase):
    cg = tmp_path / "cg" / "user.slice" / "openclaw-gateway.service"
    cg.mkdir(parents=True)
    proc = tmp_path / "proc"
    cmds = ["engram mcp"] * n_engram + ["node supabase-mcp"] * n_supabase + ["openclaw-gateway"]
    for i, cmd in enumerate(cmds, start=100):
        (proc / str(i)).mkdir(parents=True)
        (proc / str(i) / "cmdline").write_bytes(cmd.replace(" ", "\0").encode() + b"\0")
    (cg / "cgroup.procs").write_text("\n".join(str(100 + i) for i in range(len(cmds))) + "\n")
    return tmp_path / "cg", proc


class Fleet:
    """A gateway the fake runner can stop and start; `fresh` is what the MCP stacks look like afterwards."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.started = 0
        self.healthy = True

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:4] == ["systemctl", "--user", "show", host.GATEWAY_UNIT] and "ControlGroup" in argv:
            return 0, f"ControlGroup={CG}\n"
        if argv[:3] == ["systemctl", "--user", "show"]:
            return 0, "[not set]"
        if argv[:3] == ["systemctl", "--user", "start"]:
            self.started += 1
        if argv[:2] == ["openclaw", "health"]:
            return (0, "ok") if self.healthy else (1, "down")
        return 0, ""

    def stops(self):
        return [c for c in self.calls if c[:3] == ["systemctl", "--user", "stop"]]


def _run(tmp_path, fleet, tree, stale=None, **kw):
    cg, proc = tree
    return rec.reconcile(fleet, plugin_dir="/kit/openclaw-plugin/ai-resources", stale_reason=lambda *a, **k: stale,
                         cgroup_root=cg, proc_root=proc, self_cgroup_path=tmp_path / "no-cgroup",
                         sleep=lambda s: None, marker=host.Marker(tmp_path / "w.off"), openclaw_bin="openclaw",
                         lock_path=tmp_path / "lock", drain_timeout=3, drain_interval=1, health_timeout=5,
                         health_interval=1, **kw)


def test_the_216_children_case_counts_by_server_and_is_an_overflow(tmp_path):
    cg, proc = _tree(tmp_path, 57, 159)
    counts = rec.count_mcp_children(CG, cgroup_root=cg, proc_root=proc)
    assert counts == {"engram": 57, "supabase": 159} and rec.mcp_overflow(counts)
    assert not rec.mcp_overflow({"engram": 24}) and rec.mcp_overflow({"engram": 25})


def test_a_clean_gateway_is_a_no_op_and_stops_nothing(tmp_path):
    fleet = Fleet()
    out = _run(tmp_path, fleet, _tree(tmp_path, 3, 2))
    assert out.code == 0 and out.reasons == [] and not out.restarted and fleet.stops() == []


@pytest.mark.parametrize("why", ["path", "version", "time"])
def test_a_stale_plugin_by_any_rule_is_one_drained_restart(tmp_path, why):
    fleet = Fleet()
    out = _run(tmp_path, fleet, _tree(tmp_path, 1, 1), stale=why)
    assert out.restarted and out.code == 0
    assert [r.kind for r in out.reasons] == ["plugin-stale"] and why in out.reasons[0].text
    assert len(fleet.stops()) == 1 and fleet.started == 1 and not (tmp_path / "w.off").exists()


def test_the_mcp_overflow_is_resolved_by_the_drained_restart(tmp_path):
    fleet = Fleet()
    out = _run(tmp_path, fleet, _tree(tmp_path, 57, 159))
    assert out.restarted and [r.kind for r in out.reasons] == ["mcp-overflow"]
    assert "216" in out.reasons[0].text and "24" in out.reasons[0].text
    assert len(fleet.stops()) == 1 and fleet.started == 1


def test_both_reasons_share_one_restart(tmp_path):
    fleet = Fleet()
    out = _run(tmp_path, fleet, _tree(tmp_path, 57, 159), stale="version")
    assert {r.kind for r in out.reasons} == {"plugin-stale", "mcp-overflow"}
    assert len(fleet.stops()) == 1


def test_a_second_run_after_the_restart_is_a_no_op(tmp_path):
    fleet = Fleet()
    assert _run(tmp_path, fleet, _tree(tmp_path, 57, 159), stale="time").restarted
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    again = _run(tmp_path, fleet, _tree(fresh, 2, 1), stale=None)
    assert again.reasons == [] and not again.restarted and len(fleet.stops()) == 1


def test_dry_run_says_what_is_owed_and_stops_nothing(tmp_path):
    fleet = Fleet()
    out = _run(tmp_path, fleet, _tree(tmp_path, 57, 159), stale="path", dry_run=True)
    assert out.code == 0 and not out.restarted and fleet.stops() == []
    assert any("dry run" in line for line in out.lines) and any("restart owed" in line for line in out.lines)


def test_busy_gateway_defers_and_stops_nothing(tmp_path):
    fleet = Fleet()
    out = _run(tmp_path, fleet, _tree(tmp_path, 57, 159), busy_probe=lambda: 2)
    assert out.deferred and out.code == host.EXIT_REFUSED_BUSY and fleet.stops() == []
    assert any("deferred" in line for line in out.lines)


def test_inside_the_gateway_cgroup_it_defers(tmp_path):
    cg = tmp_path / "self"
    cg.write_text("0::/app.slice/openclaw-gateway.service\n")
    fleet = Fleet()
    out = rec.reconcile(fleet, plugin_dir="/k", stale_reason=lambda *a, **k: "path", self_cgroup_path=cg,
                        lock_path=tmp_path / "lock", marker=host.Marker(tmp_path / "w.off"),
                        cgroup_root=tmp_path, proc_root=tmp_path)
    assert out.deferred and out.code == host.EXIT_REFUSED_SELF and fleet.stops() == []


def test_lock_contention_defers(tmp_path):
    fd = host.acquire_restart_lock(tmp_path / "lock")
    try:
        fleet = Fleet()
        out = _run(tmp_path, fleet, _tree(tmp_path, 1, 1), stale="path")
        assert out.deferred and out.code == host.EXIT_REFUSED and fleet.stops() == []
    finally:
        host.release_restart_lock(fd)


def test_a_gateway_that_does_not_come_back_is_reported_not_deferred_and_resumable(tmp_path):
    fleet = Fleet()
    fleet.healthy = False
    out = _run(tmp_path, fleet, _tree(tmp_path, 1, 1), stale="path")
    assert out.code == host.EXIT_NOT_HEALTHY and not out.deferred and not out.restarted
    assert fleet.started == 1 and not (tmp_path / "w.off").exists()
    assert any(rec.RECONCILE_COMMAND in line for line in out.lines)
    fleet.healthy = True                           # resume: the same command, now it completes
    again = tmp_path / "again"
    again.mkdir()
    assert _run(tmp_path, fleet, _tree(again, 1, 1), stale="path").restarted


def test_an_unreadable_cgroup_is_not_a_reason(tmp_path):
    def blind(argv, **kw):
        return 127, ""
    assert rec.gateway_mcp_children(blind) is None
    assert rec.restart_reasons(plugin_dir="/k", stale_reason=lambda *a, **k: None, mcp_counts=None) == []

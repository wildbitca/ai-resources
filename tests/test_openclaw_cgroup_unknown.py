"""A process that cannot read its own cgroup cannot tell whether it is a child of the gateway, so every path that
stops or restarts the unit refuses (kit 2.2c, 3e). A host without procfs (no such file) is not "inside" anything."""
from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import openclaw_host as host  # noqa: E402
from ai_resources.setup.cockpits import openclaw  # noqa: E402


@pytest.fixture
def unreadable(tmp_path):
    d = tmp_path / "cgroup-dir"
    d.mkdir()                       # reading a directory raises IsADirectoryError: it exists, it cannot be read
    return d


def test_the_cgroup_state_distinguishes_inside_outside_and_unknown(tmp_path, unreadable):
    inside = tmp_path / "in"
    inside.write_text("0::/user.slice/app.slice/openclaw-gateway.service\n", encoding="utf-8")
    outside = tmp_path / "out"
    outside.write_text("0::/user.slice/session-3.scope\n", encoding="utf-8")
    binary = tmp_path / "bin"
    binary.write_bytes(b"\xff\xfe\x00")
    assert host.gateway_cgroup_state(inside) == "inside"
    assert host.gateway_cgroup_state(outside) == "outside"
    assert host.gateway_cgroup_state(tmp_path / "missing") == "outside"      # no procfs: nothing to be inside of
    assert host.gateway_cgroup_state(unreadable) == "unknown"
    assert host.gateway_cgroup_state(binary) == "unknown"
    assert host.inside_gateway_cgroup(unreadable) is False                   # the boolean keeps its meaning


def test_the_drained_window_refuses_when_it_cannot_tell_and_stops_nothing(unreadable):
    lines: list[str] = []
    ran: list[int] = []
    calls: list = []

    def runner(argv, **kw):
        calls.append(argv)
        return 0, ""
    code = host.drained_window(lambda: ran.append(1) or True, runner=runner, openclaw_bin="openclaw",
                               out=lines.append, self_cgroup_path=unreadable)
    assert code == host.EXIT_REFUSED_SELF and ran == [] and calls == []
    assert len(lines) == 1 and "cannot tell" in lines[0] and "nothing was stopped" in lines[0]


def test_doctor_refuses_when_it_cannot_tell(unreadable):
    lines: list[str] = []
    calls: list = []
    code = host.doctor(runner=lambda argv, **kw: calls.append(argv) or (0, ""), out=lines.append,
                       openclaw_bin="openclaw", self_cgroup_path=unreadable)
    assert code == host.EXIT_REFUSED_SELF and calls == []
    assert len([ln for ln in lines if "cannot tell" in ln]) == 1


def test_the_setup_restart_is_deferred_when_it_cannot_tell(unreadable, monkeypatch):
    calls: list = []
    monkeypatch.setattr(openclaw, "_openclaw", lambda *a, **k: calls.append(a) or (0, "ok"))
    with pytest.raises(openclaw.RestartDeferred) as e:
        openclaw.restart_gateway(probe=lambda: (0, []), interactive=False, self_cgroup_path=str(unreadable))
    assert "cannot tell" in e.value.reason and "\n" not in e.value.reason
    assert calls == [] and not host.WATCHDOG_OFF.exists()


def test_a_missing_cgroup_file_does_not_stop_the_setup_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(openclaw, "_openclaw", lambda *a, **k: (0, "ok"))
    monkeypatch.setattr(openclaw, "backend_registered", lambda _b: True)
    assert openclaw.restart_gateway(probe=lambda: (0, []), interactive=False,
                                    self_cgroup_path=str(tmp_path / "none")) is True


def test_the_graceful_restart_refuses_when_it_cannot_tell_and_acts_on_nothing(tmp_path, unreadable):
    calls: list = []
    lines: list[str] = []
    res = host.graceful_restart(reason="manual", runner=lambda argv, **kw: calls.append(argv) or (0, ""),
                                knobs=dict(host.RESTART_KNOB_DEFAULTS), marker=host.Marker(tmp_path / "off"),
                                state_path=tmp_path / "state", lock_path=tmp_path / "lock",
                                snapshot_root=tmp_path / "snap", self_cgroup_path=unreadable,
                                openclaw_bin="openclaw", out=lines.append)
    assert res["exit_code"] == host.EXIT_REFUSED_SELF and res["result"] == "refused-self"
    assert calls == [] and not (tmp_path / "lock").exists() and not (tmp_path / "off").exists()
    assert len(lines) == 1 and "cannot tell" in lines[0]

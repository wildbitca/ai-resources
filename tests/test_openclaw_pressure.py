"""The pressure detector (ADR-0004): signals, confirmation, frozen detection, fail-closed probes.

Everything runs against fixture trees under tests/fixtures/{cgroup,proc} and a fake runner: nothing here
reads the host's cgroup or runs a real command. The fixtures were shaped from the live gateway on
2026-10-10 (MemoryHigh 12 GiB; 13.25 GB before the restart, 5.56 GB after).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import shutil
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import openclaw_host as host  # noqa: E402
from ai_resources import openclaw_pressure as pr  # noqa: E402

FIX = REPO / "tests" / "fixtures"
JOURNAL = FIX / "journal" / "gateway-restart-recovery-2026-10-10.txt"
GIB = 1024 ** 3
NOW = 1_790_000_000.0
ENTERED = NOW - 7200            # the unit has been active for 2 h


class Fake:
    """A runner that answers the three read-only commands and records every call."""

    def __init__(self, *, pid="1001", health_rc=0, health_text="ok", swap_used=15 * GIB, swap_total=16 * GIB,
                 active="active", entered=ENTERED, cgroup="/openclaw-gateway.service", show_rc=0):
        self.calls: list[list[str]] = []
        self.pid, self.health_rc, self.health_text = pid, health_rc, health_text
        self.swap_used, self.swap_total, self.active, self.entered = swap_used, swap_total, active, entered
        self.cgroup, self.show_rc = cgroup, show_rc
        self.on_health = None

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        if argv[:3] == ["systemctl", "--user", "show"]:
            if self.show_rc:
                return self.show_rc, "boom"
            return 0, (f"ControlGroup={self.cgroup}\nMainPID={self.pid}\nActiveState={self.active}\n"
                       f"ActiveEnterTimestamp=@{int(self.entered)}")
        if argv[-1:] == ["health"]:
            if self.on_health:
                self.on_health()
            return self.health_rc, self.health_text
        if argv[:2] == ["free", "-b"]:
            return 0, ("              total        used        free\n"
                       f"Mem:    33000000000 20000000000 1000000000\n"
                       f"Swap:   {self.swap_total} {self.swap_used} {self.swap_total - self.swap_used}\n")
        if argv[0] == "journalctl":
            return 0, JOURNAL.read_text(encoding="utf-8")
        raise AssertionError(f"the detector must stay read-only, got {argv}")


@pytest.fixture
def tree(tmp_path):
    """A writable copy of the cgroup fixtures, so a test can change a counter between samples."""
    dst = tmp_path / "cgroup"
    shutil.copytree(FIX / "cgroup", dst)
    return dst


def run(scenario_root, fake, *, confirm=60, sleep=None, **kw):
    return pr.evaluate(runner=fake, openclaw_bin="openclaw", cgroup_root=scenario_root,
                       proc_root=FIX / "proc", confirm_seconds=confirm, sleep=sleep or (lambda s: None),
                       clock=lambda: NOW, **kw)


def bump_high(root, scenario, by=500):
    p = root / scenario / "openclaw-gateway.service" / "memory.events"
    lines = []
    for line in p.read_text().splitlines():
        k, v = line.split()
        lines.append(f"{k} {int(v) + by if k == 'high' else v}")
    p.write_text("\n".join(lines) + "\n")


# --- readers ---------------------------------------------------------------------------------------------

def test_read_cgroup_reads_every_signal():
    c = pr.read_cgroup("/openclaw-gateway.service", FIX / "cgroup" / "pressure")
    assert c["current"] == 12079595520 and c["high"] == 12 * GIB and c["max"] == "max"
    assert c["events"] == {"high": 1000, "max": 0, "oom": 0, "oom_kill": 0}
    assert c["anon"] and c["file"] and c["psi_some_avg10"] == 40.0 and c["psi_full_avg10"] == 40.0
    assert c["pids"] == 625 and c["procs"] == 5


def test_missing_optional_files_degrade_to_none(tmp_path):
    d = tmp_path / "gw"
    shutil.copytree(FIX / "cgroup" / "normal" / "openclaw-gateway.service", d)
    for name in ("memory.stat", "memory.pressure", "pids.current", "cgroup.procs", "memory.max"):
        (d / name).unlink()
    c = pr.read_cgroup("/gw", tmp_path)
    assert c["current"] and c["anon"] is None and c["psi_full_avg10"] is None and c["pids"] is None
    assert c["procs"] is None and c["max"] is None


@pytest.mark.parametrize("missing", ["memory.current", "memory.high", "memory.events"])
def test_a_missing_required_file_raises_probe_error(tmp_path, missing):
    d = tmp_path / "gw"
    shutil.copytree(FIX / "cgroup" / "normal" / "openclaw-gateway.service", d)
    (d / missing).unlink()
    with pytest.raises(pr.ProbeError):
        pr.read_cgroup("/gw", tmp_path)


def test_read_proc_handles_state_and_wchan_and_a_comm_with_parentheses(tmp_path):
    assert pr.read_proc("1002", FIX / "proc") == {"state": "D", "wchan": "__mem_cgroup_handle_over_high"}
    d = tmp_path / "7"
    d.mkdir()
    (d / "stat").write_text("7 (a (b) c) R 1 7\n")
    assert pr.read_proc(7, tmp_path) == {"state": "R", "wchan": None}
    assert pr.read_proc(999, tmp_path) == {"state": None, "wchan": None}


# --- classification --------------------------------------------------------------------------------------

def test_a_normal_gateway_is_healthy(tree):
    out = run(tree / "normal", Fake(swap_used=0))
    assert out["classification"] == "healthy"


def test_confirmed_pressure_needs_both_samples_swap_and_a_positive_events_delta(tree):
    out = run(tree / "pressure", Fake(), sleep=lambda s: bump_high(tree, "pressure"))
    assert out["classification"] == "pressure"
    assert out["evidence"]["high_events_delta"] == 500 and out["evidence"]["memory_pct"][0] >= 90


def test_the_absolute_events_counter_is_never_used(tree):
    # high=1000 is a large cumulative count, but it did not move between the samples
    out = run(tree / "pressure", Fake())
    assert out["classification"] == "healthy"
    assert out["evidence"]["high_events_delta"] == 0


def test_pressure_without_swap_pressure_is_not_confirmed(tree):
    out = run(tree / "pressure", Fake(swap_used=GIB), sleep=lambda s: bump_high(tree, "pressure"))
    assert out["classification"] == "healthy"


def test_a_spike_that_clears_in_the_second_sample_is_healthy(tree):
    def clear(_s):
        bump_high(tree, "pressure")
        (tree / "pressure" / "openclaw-gateway.service" / "memory.current").write_text("6442450944\n")

    out = run(tree / "pressure", Fake(), sleep=clear)
    assert out["classification"] == "healthy"


def test_hard_is_pressure_at_or_above_the_ceiling_in_both_samples(tree):
    out = run(tree / "hard", Fake(), sleep=lambda s: bump_high(tree, "hard"))
    assert out["classification"] == "hard"
    assert all(p >= 105 for p in out["evidence"]["memory_pct"])


def test_a_ceiling_knob_changes_the_boundary(tree):
    out = run(tree / "hard", Fake(), sleep=lambda s: bump_high(tree, "hard"), hard_pct=120)
    assert out["classification"] == "pressure"


def test_a_throttled_main_thread_is_frozen_and_wins_over_everything(tree):
    out = run(tree / "frozen", Fake(pid="1002", health_rc=1, health_text="Gateway is still starting (phase: x)."),
              sleep=lambda s: bump_high(tree, "frozen"))
    assert out["classification"] == "frozen" and out["evidence"]["frozen_by"] == "memory-throttle"


def test_still_starting_longer_than_the_threshold_is_frozen_without_wchan(tree):
    fake = Fake(health_rc=1, health_text="Gateway is still starting (phase: waiting for Gateway health)",
                entered=NOW - 1200)
    out = run(tree / "normal", fake)
    assert out["classification"] == "frozen" and out["evidence"]["frozen_by"] == "still-starting"


def test_still_starting_for_a_short_time_is_just_a_health_failure(tree):
    fake = Fake(health_rc=1, health_text="Gateway is still starting (phase: x).", entered=NOW - 30)
    assert run(tree / "normal", fake)["classification"] == "health-fail"


def test_over_the_limit_and_not_answering_is_frozen_never_a_restart(tree):
    out = run(tree / "pressure", Fake(health_rc=1, health_text="timeout"), sleep=lambda s: bump_high(tree, "pressure"))
    assert out["classification"] == "frozen" and out["evidence"]["frozen_by"] == "unresponsive-over-limit"


def test_health_failing_without_pressure_is_health_fail(tree):
    assert run(tree / "normal", Fake(health_rc=1, health_text="refused"))["classification"] == "health-fail"


def test_an_unreadable_required_file_is_refused_probe(tree):
    out = run(tree / "unreadable", Fake())
    assert out["classification"] == "refused-probe" and "memory.current" in out["evidence"]["reason"]


def test_a_failed_systemctl_or_an_inactive_unit_is_refused_probe(tree):
    assert run(tree / "normal", Fake(show_rc=1))["classification"] == "refused-probe"
    assert run(tree / "normal", Fake(active="deactivating"))["classification"] == "refused-probe"


def test_no_memory_high_set_is_healthy_not_an_error(tree):
    (tree / "normal" / "openclaw-gateway.service" / "memory.high").write_text("max\n")
    out = run(tree / "normal", Fake())
    assert out["classification"] == "healthy" and out["evidence"]["memory_high"] is None


def test_wchan_unreadable_degrades_to_the_still_starting_variant(tmp_path, tree):
    proc = tmp_path / "proc"
    (proc / "1002").mkdir(parents=True)
    (proc / "1002" / "stat").write_text("1002 (n) D 1 1002\n")          # no wchan file
    fake = Fake(pid="1002")
    out = pr.evaluate(runner=fake, cgroup_root=tree / "frozen", proc_root=proc, confirm_seconds=0,
                      sleep=lambda s: None, clock=lambda: NOW)
    assert out["classification"] != "frozen"      # D alone, with health ok, is not enough to call it


# --- window ----------------------------------------------------------------------------------------------

def _epoch(y, mo, d, h, mi):
    import datetime as dt
    return dt.datetime(y, mo, d, h, mi, tzinfo=dt.timezone.utc).timestamp()


def test_the_window_is_evaluated_in_the_operators_zone_not_the_host_clock():
    # 08:00 UTC is 03:00 in America/Guayaquil (UTC-5): inside 02:00-05:00 although UTC says 08:00
    assert pr.window_open(_epoch(2026, 10, 10, 8, 0), "02:00-05:00", "America/Guayaquil")
    assert not pr.window_open(_epoch(2026, 10, 10, 8, 0), "02:00-05:00", "UTC")
    assert not pr.window_open(_epoch(2026, 10, 10, 18, 20), "02:00-05:00", "America/Guayaquil")   # 13:20 local


def test_window_edges_are_inclusive_at_minute_granularity():
    assert not pr.window_open(_epoch(2026, 10, 10, 1, 59), "02:00-05:00", "UTC")
    assert pr.window_open(_epoch(2026, 10, 10, 2, 0), "02:00-05:00", "UTC")
    assert pr.window_open(_epoch(2026, 10, 10, 5, 0), "02:00-05:00", "UTC")
    assert pr.window_open(_epoch(2026, 10, 10, 5, 0) + 59, "02:00-05:00", "UTC"), "open through 05:00:59"
    assert not pr.window_open(_epoch(2026, 10, 10, 5, 1), "02:00-05:00", "UTC")


def test_a_whole_day_window_is_open_at_every_minute_including_the_last():
    for window in ("00:00-23:59", "00:00-24:00"):
        for h, m in ((0, 0), (12, 30), (23, 58), (23, 59)):
            assert pr.window_open(_epoch(2026, 10, 10, h, m), window, "UTC"), (window, h, m)


def test_a_window_may_wrap_past_midnight():
    for h, m, want in ((23, 0, True), (1, 59, True), (3, 59, True), (4, 0, True), (4, 1, False),
                       (12, 0, False), (21, 59, False)):
        assert pr.window_open(_epoch(2026, 10, 10, h, m), "22:00-04:00", "UTC") is want, (h, m)


@pytest.mark.parametrize("window", ["", "none", "25:00-26:00", "02:00", "02:00-02:00"])
def test_a_malformed_or_empty_window_is_closed(window):
    assert pr.window_open(_epoch(2026, 10, 10, 3, 0), window, "UTC") is False


def test_an_unknown_zone_is_closed():
    assert pr.window_open(_epoch(2026, 10, 10, 3, 0), "00:00-23:59", "Not/AZone") is False


# --- the recovery report (AC-RSG-12) ---------------------------------------------------------------------

def test_the_report_counts_equal_the_captured_fixture_annotations():
    rep = pr.parse_recovery_report(JOURNAL.read_text(encoding="utf-8"))
    assert rep["marked_interrupted"] == 3 and rep["aborted_runs"] == 4
    assert rep["shutdown_deadline"] == 1 and rep["recovery_started"] == 1 and rep["tombstoned"] == 0
    assert rep["use_resume_true"] == 3 and rep["use_resume_false"] == 3
    header = [ln for ln in JOURNAL.read_text(encoding="utf-8").splitlines() if "marked_interrupted=" in ln][0]
    for pair in header.lstrip("# ").split():
        k, v = pair.split("=")
        assert rep[k] == int(v), k


def test_the_report_counts_tombstones_and_refusals_and_ignores_session_text():
    text = "x tombstoned session\n[restart] GATEWAY_RESTART_PREPARATION_REFUSED: x\nuseResume=true useResume=false\n"
    rep = pr.parse_recovery_report(text)
    assert rep["tombstoned"] == 1 and rep["restart_refused"] == 1 and rep["use_resume_true"] == 1


@pytest.mark.parametrize("since,want", [("1790000000", "@1790000000"), ("1790000000.9", "@1790000000"),
                                         ("2026-10-10T13:15:00Z", "2026-10-10 13:15:00 UTC"),
                                         ("2026-10-10T13:15:00", "2026-10-10 13:15:00")])
def test_since_is_normalised_for_journalctl(since, want):
    assert pr.journal_since_arg(since) == want


# --- the CLIs are read-only --------------------------------------------------------------------------------

def _args(**kw):
    return argparse.Namespace(**{"json": True, "confirm_seconds": 0.0, "since": "1790000000", **kw})


def test_pressure_cli_json_shape_and_read_only_calls(tree, capsys, monkeypatch):
    monkeypatch.setattr(host, "resolve_openclaw_bin", lambda: "openclaw")
    fake = Fake(swap_used=0)
    rc = host.cmd_pressure(_args(), runner=fake, cgroup_root=tree / "normal", proc_root=FIX / "proc")
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["classification"] == "healthy"
    assert set(out) >= {"classification", "evidence", "ts", "confirm_seconds", "thresholds"}
    allowed = (["systemctl", "--user", "show"], ["openclaw", "health"], ["free", "-b"])
    for call in fake.calls:
        assert any(call[:len(a)] == a for a in allowed), call
        assert not {"restart", "stop", "start", "kill", "set-property", "patch"} & set(call)


def test_restart_report_cli_reads_the_journal_only(capsys):
    fake = Fake()
    rc = host.cmd_restart_report(_args(), runner=fake)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["available"] is True and out["aborted_runs"] == 4
    assert [c[0] for c in fake.calls] == ["journalctl"]


def test_restart_report_says_unavailable_instead_of_zero_counts(capsys):
    class NoJournal(Fake):
        def __call__(self, argv, **kw):
            return 1, "no journal"
    assert host.cmd_restart_report(_args(), runner=NoJournal()) == 1
    assert json.loads(capsys.readouterr().out)["available"] is False


def test_the_knobs_prefer_env_then_file_then_default(tmp_path):
    k = host.restart_knobs(env={"OPENCLAW_RESTART_WINDOW": "01:00-02:00"},
                           host_env={"OPENCLAW_RESTART_WINDOW": "03:00-04:00", "OPENCLAW_RESTART_DAILY_CAP": "1"})
    assert k["OPENCLAW_RESTART_WINDOW"] == "01:00-02:00" and k["OPENCLAW_RESTART_DAILY_CAP"] == "1"
    assert k["OPENCLAW_GRACEFUL_RESTART"] == "notify" and k["OPENCLAW_RESTART_TZ"] == "America/Guayaquil"
    assert host.knob_number({"OPENCLAW_RESTART_COOLDOWN_S": "junk"}, "OPENCLAW_RESTART_COOLDOWN_S") == 10800.0


def test_the_cli_is_registered():
    ap = argparse.ArgumentParser()
    host.add_subparser(ap.add_subparsers(dest="cmd"))
    assert ap.parse_args(["openclaw", "pressure", "--json", "--confirm-seconds", "0"]).confirm_seconds == 0.0
    assert ap.parse_args(["openclaw", "restart-report", "--since", "1"]).since == "1"

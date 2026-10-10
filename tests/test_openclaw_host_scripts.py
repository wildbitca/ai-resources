"""The OpenClaw host scripts under scripts/openclaw/.

Two kinds of test. Structural ones read the script text and pin the invariants each file
exists for (the drain, the checksum written last, the literal cluster context). Behavioural
ones run the real bash against stub `systemctl` / `openclaw` / `kubectl` binaries placed first
on PATH through OPENCLAW_EXTRA_PATH, in a throwaway HOME: nothing touches the host's units,
gateway or Telegram. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts" / "openclaw"
SH = sorted(SCRIPTS.glob("*.sh"))
ALL_FILES = SH + [SCRIPTS / "openclaw-team-watch.py", SCRIPTS / "openclaw-team-send.py",
                  SCRIPTS / "openclaw-host.env.example"]

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash is required")


def _text(name: str) -> str:
    return (SCRIPTS / name).read_text(encoding="utf-8")


# --- AC-3.1 / 3.2: hygiene ---------------------------------------------------------------------------

@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.name)
def test_no_host_literals(path):
    text = path.read_text(encoding="utf-8")
    for literal in ("/home/bitgandtter", "7961376547", "wildbit", "bithome", "10.43.255.250"):
        assert literal not in text, f"{path.name} carries the host literal {literal!r}"


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.name)
def test_prose_is_english(path):
    text = path.read_text(encoding="utf-8")
    bad = sorted({c for c in text if c.isalpha() and ord(c) > 127})
    assert not bad, f"{path.name} has non-ASCII letters (untranslated prose?): {bad}"


@pytest.mark.parametrize("path", SH, ids=lambda p: p.name)
def test_bash_syntax(path):
    r = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_the_python_watcher_compiles():
    compile((SCRIPTS / "openclaw-team-watch.py").read_text(encoding="utf-8"), "watch", "exec")
    compile((SCRIPTS / "openclaw-team-send.py").read_text(encoding="utf-8"), "send", "exec")


# --- AC-3.3: the cluster context is never inherited -------------------------------------------------------

@pytest.mark.parametrize("path", SH, ids=lambda p: p.name)
def test_every_kubectl_and_flux_call_carries_a_literal_context(path):
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        code = line.split("#", 1)[0]
        if re.search(r"(^|[\s(|;`])(kubectl|flux)\s", code):
            assert "--context default" in code, f"{path.name}:{n} calls a cluster without --context default"


# --- harness for behavioural tests -------------------------------------------------------------------------

STUB_LOG = "calls.log"


class Host:
    """A throwaway HOME plus stub binaries that record every call instead of doing it."""

    def __init__(self, tmp: pathlib.Path):
        self.home = tmp / "home"
        self.bin = tmp / "stubs"
        self.run_dir = tmp / "run"
        self.backups = tmp / "backups"
        for d in (self.home / ".openclaw" / "logs", self.home / ".claude", self.bin, self.run_dir,
                  self.backups / "daily"):
            d.mkdir(parents=True, exist_ok=True)
        self.log = tmp / STUB_LOG
        self.log.write_text("", encoding="utf-8")
        self.state: dict[str, str] = {"is-active": "active", "is-enabled": "enabled"}
        self.openclaw_health_rc = 0
        for name in ("systemctl", "loginctl", "openclaw", "ai-resources", "kubectl", "flux", "sleep", "curl", "free",
                     "journalctl"):
            self._stub(name)
        self.env_file = self.home / ".openclaw" / "kit-host.env"
        self.uptime_file = tmp / "uptime"
        self.set_pressure("healthy", mem=10.0, swap=0.0)

    def _stub(self, name: str):
        script = self.bin / name
        if name == "sleep":
            script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        else:
            script.write_text(f"""#!/bin/sh
echo "{name} $*" >> "{self.log}"
case "{name} $1 $2" in
  "systemctl --user is-active") cat "{self.bin}/is-active" 2>/dev/null || echo active; exit 0 ;;
  "systemctl --user is-enabled") echo enabled; exit 0 ;;
  "systemctl --user list-timers") for i in 1 2 3 4 5 6 7 8 9 10; do echo "n openclaw-t$i.timer"; done; exit 0 ;;
  "systemctl --user show") case "$*" in *ActiveExitTimestampMonotonic*) cat "{self.bin}/exit-mono" 2>/dev/null || echo "[not set]" ;; *MemoryCurrent*) cat "{self.bin}/mem-current" 2>/dev/null || echo "[not set]" ;; *MemoryHigh*) cat "{self.bin}/mem-high" 2>/dev/null || echo "[not set]" ;; *) echo "[not set]" ;; esac; exit 0 ;;
  "free -b"*) cat "{self.bin}/free-out" 2>/dev/null || echo "Swap: 0 0 0"; exit 0 ;;
  "journalctl --user"*) cat "{self.bin}/journal-out" 2>/dev/null; exit 0 ;;
  "openclaw message send") exit "$(cat "{self.bin}/send-rc" 2>/dev/null || echo 0)" ;;
  "loginctl show-user"*) echo yes; exit 0 ;;
  "openclaw health "*) exit "$(cat "{self.bin}/health-rc" 2>/dev/null || echo 0)" ;;
  "ai-resources models"*) cat "{self.bin}/models-out" 2>/dev/null; exit "$(cat "{self.bin}/models-rc" 2>/dev/null || echo 0)" ;;
  "ai-resources openclaw busy"*) exit "$(cat "{self.bin}/busy-rc" 2>/dev/null || echo 0)" ;;
  "ai-resources openclaw pressure") cat "{self.bin}/pressure-out" 2>/dev/null; exit 0 ;;
  "ai-resources openclaw graceful-restart") cat "{self.bin}/graceful-out" 2>/dev/null; exit "$(cat "{self.bin}/graceful-rc" 2>/dev/null || echo 0)" ;;
  "ai-resources openclaw"*) echo "Repaired legacy bindings 2"; exit "$(cat "{self.bin}/doctor-rc" 2>/dev/null || echo 0)" ;;
esac
exit 0
""", encoding="utf-8")
        script.chmod(0o755)

    def set_active(self, value: str):
        (self.bin / "is-active").write_text(value + "\n", encoding="utf-8")

    def set_busy_rc(self, rc: int):
        (self.bin / "busy-rc").write_text(str(rc), encoding="utf-8")

    def set_stalls(self, n: int):
        (self.bin / "journal-out").write_text("CLI produced no output\n" * n, encoding="utf-8")

    def set_memory_pressure(self):
        (self.bin / "mem-current").write_text("95\n", encoding="utf-8")
        (self.bin / "mem-high").write_text("100\n", encoding="utf-8")
        (self.bin / "free-out").write_text("Swap: 1000 950 50\n", encoding="utf-8")
        self.set_pressure("pressure")

    def set_pressure(self, classification: str, *, mem: float = 94.0, swap: float = 96.0, **evidence):
        """What `ai-resources openclaw pressure --json` answers (the detector has its own tests)."""
        payload = {"classification": classification,
                   "evidence": {"memory_pct": [mem, mem], "swap_pct": [swap, swap], **evidence}}
        (self.bin / "pressure-out").write_text(json.dumps(payload), encoding="utf-8")

    def set_graceful(self, rc: int, payload: dict | None = None):
        """What `ai-resources openclaw graceful-restart --json` answers and exits with."""
        (self.bin / "graceful-rc").write_text(str(rc), encoding="utf-8")
        (self.bin / "graceful-out").write_text(json.dumps(payload or {}), encoding="utf-8")

    def set_doctor_rc(self, rc: int):
        (self.bin / "doctor-rc").write_text(str(rc), encoding="utf-8")

    def set_models(self, rc: int, payload: dict | str = ""):
        (self.bin / "models-rc").write_text(str(rc), encoding="utf-8")
        (self.bin / "models-out").write_text(payload if isinstance(payload, str) else json.dumps(payload),
                                             encoding="utf-8")

    def set_send_rc(self, rc: int):
        (self.bin / "send-rc").write_text(str(rc), encoding="utf-8")

    def set_deactivating_for(self, seconds: float, exit_mono_us: int = 1_000_000_000):
        """Fake clock: uptime is exit_mono + seconds, so the unit has been deactivating that long."""
        (self.bin / "exit-mono").write_text(f"{exit_mono_us}\n", encoding="utf-8")
        self.uptime_file.write_text(f"{exit_mono_us / 1e6 + seconds:.2f} 0.00\n", encoding="utf-8")

    def add_stability_bundle(self, name: str, raw: str | None = None, events: list | None = None):
        d = self.home / ".openclaw" / "logs" / "stability"
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(raw if raw is not None else json.dumps(
            {"version": 1, "reason": "gateway.stop_shutdown_timeout", "snapshot": {"events": events or []}}),
            encoding="utf-8")

    def sent(self) -> list[str]:
        return [c for c in self.calls() if c.startswith("openclaw message send")]

    def set_health(self, rc: int):
        (self.bin / "health-rc").write_text(str(rc), encoding="utf-8")
        # The health-restart script reads health through the pressure CLI; keep both views in step.
        self.set_pressure("health-fail" if rc else "healthy", mem=10.0, swap=0.0)

    def write_env(self, **kv: str):
        self.env_file.write_text("".join(f"{k}={v}\n" for k, v in kv.items()), encoding="utf-8")

    def fresh_backup(self, age_hours: float = 1.0):
        tarball = self.backups / "daily" / "openclaw-20260101-000000.tar.gz"
        tarball.write_bytes(b"x")
        tarball.with_name(tarball.name + ".sha256").write_text("x\n", encoding="utf-8")
        ts = __import__("time").time() - age_hours * 3600
        os.utime(tarball, (ts, ts))

    def settings(self, commands: list[str]):
        hooks = {"PreToolUse": [{"hooks": [{"type": "command", "command": c} for c in commands]}]}
        (self.home / ".claude" / "settings.json").write_text(json.dumps({"hooks": hooks}), encoding="utf-8")

    def run(self, script: str, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
        env = {
            "HOME": str(self.home), "USER": "tester", "LOGNAME": "tester",
            "XDG_RUNTIME_DIR": str(self.run_dir), "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent",
            "OPENCLAW_EXTRA_PATH": str(self.bin), "OPENCLAW_BACKUP_DIR": str(self.backups),
            "OPENCLAW_HOST_ENV": str(self.env_file), "OPENCLAW_UPTIME_FILE": str(self.uptime_file),
            "PATH": os.environ["PATH"],
        }
        return subprocess.run(["bash", str(SCRIPTS / script), *args], env=env, capture_output=True,
                              text=True, timeout=timeout)

    def calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines()


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


# --- AC-3.4: the watchdog only acts on a real death ---------------------------------------------------------

@pytest.mark.parametrize("state", ["deactivating", "activating", "reloading"])
def test_watchdog_does_not_intervene_in_a_transition_state(host, state):
    host.set_active(state)
    r = host.run("openclaw-watchdog.sh")
    assert r.returncode == 0, r.stderr
    assert not any(c.startswith("systemctl --user start") for c in host.calls())
    assert "not intervening" in (host.home / ".openclaw" / "logs" / "watchdog.log").read_text()


@pytest.mark.parametrize("state", ["inactive", "failed"])
def test_watchdog_starts_a_dead_gateway_and_reports_it(host, state):
    host.set_active(state)
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    r = host.run("openclaw-watchdog.sh")
    assert r.returncode == 0, r.stderr
    calls = host.calls()
    assert any(c.startswith("systemctl --user start openclaw-gateway.service") for c in calls)
    sent = [c for c in calls if c.startswith("openclaw message send")]
    assert len(sent) == 1 and "--target 42" in sent[0]


def test_watchdog_is_paused_by_the_marker_file(host):
    host.set_active("inactive")
    (host.home / ".openclaw" / "watchdog.off").write_text("", encoding="utf-8")
    r = host.run("openclaw-watchdog.sh")
    assert r.returncode == 0
    assert not any("start" in c for c in host.calls())


def test_watchdog_exits_non_zero_when_the_gateway_stays_down(host):
    host.set_active("inactive")
    host.set_health(1)
    r = host.run("openclaw-watchdog.sh")
    assert r.returncode == 1


def test_watchdog_stays_quiet_when_there_is_nobody_to_notify(host):
    host.set_active("inactive")
    r = host.run("openclaw-watchdog.sh")
    assert r.returncode == 0
    assert not any(c.startswith("openclaw message send") for c in host.calls())


# --- T39: the watchdog says so when a stop is stuck or hit its shutdown timeout ---------------------------------

BUNDLE_OLD = "openclaw-stability-2026-09-18T01-25-07-371Z-485771-gateway.stop_shutdown_timeout.json"
BUNDLE_NEW = "openclaw-stability-2026-10-06T17-14-23-353Z-1051-gateway.stop_shutdown_timeout.json"


def _no_lifecycle_calls(host):
    assert not any(c.startswith(("systemctl --user start", "systemctl --user stop",
                                 "systemctl --user restart")) for c in host.calls())


def test_watchdog_alerts_once_when_deactivating_past_the_threshold(host):
    host.set_active("deactivating")
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.set_deactivating_for(7 * 60)
    assert host.run("openclaw-watchdog.sh").returncode == 0
    assert len(host.sent()) == 1 and "deactivating" in host.sent()[0]
    host.run("openclaw-watchdog.sh")
    assert len(host.sent()) == 1, "the same stop must not page twice"
    _no_lifecycle_calls(host)


def test_watchdog_alerts_again_for_a_new_stop(host):
    host.set_active("deactivating")
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.set_deactivating_for(400)
    host.run("openclaw-watchdog.sh")
    host.set_deactivating_for(400, exit_mono_us=5_000_000_000)
    host.run("openclaw-watchdog.sh")
    assert len(host.sent()) == 2


def test_watchdog_only_logs_when_deactivating_below_the_threshold(host):
    host.set_active("deactivating")
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.set_deactivating_for(30)
    host.run("openclaw-watchdog.sh")
    assert host.sent() == []
    assert "not intervening" in (host.home / ".openclaw" / "logs" / "watchdog.log").read_text()


def test_watchdog_retries_a_failed_deactivating_alert(host):
    host.set_active("deactivating")
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.set_deactivating_for(400)
    host.set_send_rc(1)
    host.run("openclaw-watchdog.sh")
    assert len(host.sent()) == 1
    assert not (host.home / ".openclaw" / "logs" / "watchdog.deactivating-alerted").exists()
    host.set_send_rc(0)
    host.run("openclaw-watchdog.sh")
    assert len(host.sent()) == 2
    host.run("openclaw-watchdog.sh")
    assert len(host.sent()) == 2


def test_watchdog_resets_the_deactivating_marker_when_the_unit_goes_active(host):
    marker = host.home / ".openclaw" / "logs" / "watchdog.deactivating-alerted"
    host.set_active("deactivating")
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.set_deactivating_for(400)
    host.run("openclaw-watchdog.sh")
    assert marker.exists() and len(host.sent()) == 1
    host.set_active("active")
    host.run("openclaw-watchdog.sh")
    assert not marker.exists()
    # the same stop timestamp seen again (a new stop that reuses it) must page again
    host.set_active("deactivating")
    host.run("openclaw-watchdog.sh")
    assert len(host.sent()) == 2


def test_watchdog_honours_the_deactivating_threshold_from_the_host_env(host):
    host.set_active("deactivating")
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42", OPENCLAW_WATCHDOG_DEACTIVATING_ALERT_SEC="100")
    host.set_deactivating_for(90)
    host.run("openclaw-watchdog.sh")
    assert host.sent() == []
    host.set_deactivating_for(110)
    host.run("openclaw-watchdog.sh")
    assert len(host.sent()) == 1


def test_watchdog_deactivating_threshold_defaults_to_240_seconds(host):
    host.set_active("deactivating")
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.set_deactivating_for(230)
    host.run("openclaw-watchdog.sh")
    assert host.sent() == []
    host.set_deactivating_for(250)
    host.run("openclaw-watchdog.sh")
    assert len(host.sent()) == 1


def test_watchdog_bundle_alert_names_the_stalled_count_as_a_lower_bound(host):
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.run("openclaw-watchdog.sh")
    stalls = [{"type": "session.stalled", "reason": "blocked_tool_call"}] * 3 + [{"type": "session.long_running"}]
    host.add_stability_bundle(BUNDLE_NEW, events=stalls)
    host.run("openclaw-watchdog.sh")
    assert len(host.sent()) == 1
    assert "(3 stalled sessions recorded, a lower bound)" in host.sent()[0]


@pytest.mark.parametrize("raw", ["{not json", json.dumps({"version": 1, "snapshot": [1]}), "[]"])
def test_watchdog_bundle_alert_still_sends_when_the_counts_cannot_be_read(host, raw):
    seen = host.home / ".openclaw" / "logs" / "watchdog.seen-stability"
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.run("openclaw-watchdog.sh")
    host.add_stability_bundle(BUNDLE_NEW, raw=raw)
    host.run("openclaw-watchdog.sh")
    assert len(host.sent()) == 1 and "T39" in host.sent()[0]
    assert "stalled sessions recorded" not in host.sent()[0]
    assert seen.read_text().strip() == BUNDLE_NEW
    host.run("openclaw-watchdog.sh")
    assert len(host.sent()) == 1


def test_watchdog_first_run_baselines_existing_bundles_silently(host):
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.add_stability_bundle(BUNDLE_OLD)
    host.add_stability_bundle(BUNDLE_NEW)
    assert host.run("openclaw-watchdog.sh").returncode == 0
    assert host.sent() == []
    assert (host.home / ".openclaw" / "logs" / "watchdog.seen-stability").read_text().strip() == BUNDLE_NEW


def test_watchdog_alerts_once_on_a_new_stop_shutdown_timeout_bundle_while_active(host):
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.add_stability_bundle(BUNDLE_OLD)
    host.run("openclaw-watchdog.sh")
    host.add_stability_bundle(BUNDLE_NEW)
    host.run("openclaw-watchdog.sh")
    assert len(host.sent()) == 1 and "T39" in host.sent()[0]
    host.run("openclaw-watchdog.sh")
    assert len(host.sent()) == 1
    _no_lifecycle_calls(host)


def test_watchdog_ignores_bundles_with_other_reasons(host):
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.run("openclaw-watchdog.sh")
    host.add_stability_bundle("openclaw-stability-2026-10-05T17-18-14-775Z-3724199-gateway.stop_close_failed.json")
    host.run("openclaw-watchdog.sh")
    assert host.sent() == []


def test_watchdog_retries_a_failed_bundle_alert(host):
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.run("openclaw-watchdog.sh")
    host.add_stability_bundle(BUNDLE_NEW)
    host.set_send_rc(1)
    host.run("openclaw-watchdog.sh")
    host.set_send_rc(0)
    host.run("openclaw-watchdog.sh")
    assert len(host.sent()) == 2
    host.run("openclaw-watchdog.sh")
    assert len(host.sent()) == 2


def test_watchdog_pause_marker_silences_the_new_alerts(host):
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.set_active("deactivating")
    host.set_deactivating_for(900)
    host.add_stability_bundle(BUNDLE_NEW)
    (host.home / ".openclaw" / "watchdog.off").write_text("", encoding="utf-8")
    host.run("openclaw-watchdog.sh")
    assert host.sent() == []


def test_watchdog_makes_no_send_attempt_without_an_owner_id(host):
    host.set_active("deactivating")
    host.set_deactivating_for(900)
    host.run("openclaw-watchdog.sh")
    host.add_stability_bundle(BUNDLE_NEW)
    host.run("openclaw-watchdog.sh")
    assert host.sent() == []


# --- AC-3.3 / 3.5: verify -----------------------------------------------------------------------------------------

def test_verify_with_every_optional_check_off_runs_only_the_generic_ones(host):
    host.fresh_backup()
    host.settings(['python3 "/k/hooks/kit_handoff_guard.py"'])
    r = host.run("openclaw-verify.sh")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ALL OK" in r.stdout
    for absent in ("OTLP", "tool_input", "flux", "http"):
        assert absent not in r.stdout
    assert not any(c.startswith(("kubectl", "flux", "curl")) for c in host.calls())


def test_verify_expects_the_hooks_the_host_env_says_are_enabled(host):
    host.fresh_backup()
    host.write_env(OPENCLAW_NARRATION="milestones", OPENCLAW_GUARD="1")
    host.settings(['python3 "/k/hooks/kit_handoff_guard.py"'])
    r = host.run("openclaw-verify.sh")
    assert r.returncode == 1
    assert "openclaw_team_progress" in r.stdout and "openclaw_gateway_guard" in r.stdout

    host.settings(['python3 "/k/hooks/kit_handoff_guard.py"',
                   'python3 "/k/hooks/openclaw_team_progress.py"',
                   'python3 "/k/hooks/openclaw_gateway_guard.py"'])
    assert host.run("openclaw-verify.sh").returncode == 0


def test_verify_finds_the_kit_hook_names_not_the_legacy_hyphenated_one(host):
    host.fresh_backup()
    host.write_env(OPENCLAW_NARRATION="milestones")
    host.settings(['python3 "/k/hooks/kit_handoff_guard.py"',
                   'python3 "$HOME/.claude/hooks/openclaw-team-progress.py"'])
    assert host.run("openclaw-verify.sh").returncode == 1


def test_verify_flags_a_stale_backup_and_a_dead_gateway(host):
    host.fresh_backup(age_hours=40)
    host.set_active("inactive")
    host.settings(['python3 "/k/hooks/kit_handoff_guard.py"'])
    r = host.run("openclaw-verify.sh")
    assert r.returncode == 1
    assert "[FAIL] local backup" in r.stdout and "[FAIL] gateway" in r.stdout


def test_verify_optional_checks_turn_on_from_the_host_env(host):
    host.fresh_backup()
    host.settings(['python3 "/k/hooks/kit_handoff_guard.py"'])
    host.write_env(OPENCLAW_VERIFY_FLUX="1", OPENCLAW_VERIFY_ALLOY_FILTER="1")
    host.run("openclaw-verify.sh")
    cluster = [c for c in host.calls() if c.startswith(("kubectl", "flux"))]
    assert cluster and all("--context default" in c for c in cluster)


def test_verify_notify_mode_is_silent_when_everything_is_fine(host):
    host.fresh_backup()
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.settings(['python3 "/k/hooks/kit_handoff_guard.py"'])
    r = host.run("openclaw-verify.sh", "--notify")
    assert r.returncode == 0
    assert not any(c.startswith("openclaw message send") for c in host.calls())


def test_verify_notify_mode_reports_only_the_failed_lines(host):
    host.fresh_backup(age_hours=40)
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.settings(['python3 "/k/hooks/kit_handoff_guard.py"'])
    r = host.run("openclaw-verify.sh", "--notify")
    assert r.returncode == 1
    # The message is multi-line, so read the whole call log rather than one line of it.
    log = "\n".join(host.calls())
    assert log.count("openclaw message send") == 1
    sent = log[log.index("openclaw message send"):]
    assert "local backup" in sent and "[ok]" not in sent
    assert "local backup" in (host.home / ".openclaw" / "logs" / "verify.log").read_text()


def test_the_verify_script_expects_ten_timers():
    assert re.search(r'-ge 10\b', _text("openclaw-verify.sh"))
    assert not re.search(r'-ge (6|8)\b', _text("openclaw-verify.sh"))


# --- AC-4.1: the backup invariants ----------------------------------------------------------------------------------

def test_backup_writes_the_checksum_strictly_after_the_size_and_listing_assertions():
    text = _text("openclaw-backup.sh")
    size = text.index('[ "$BYTES" -ge "$MIN_BYTES" ]')
    listing = text.index('tar tzf "$DEST/$NAME.tar.gz"')
    checksum = text.index('sha256sum "$NAME.tar.gz" >')
    verify = text.index("sha256sum -c")
    assert size < listing < checksum < verify


def test_backup_stages_on_disk_never_in_tmp():
    text = _text("openclaw-backup.sh")
    assert 'mktemp -d -p "$BASE"' in text
    assert not re.search(r"mktemp[^\n]*/tmp", text) and "TMPDIR" not in text


def test_backup_has_no_or_true_on_a_critical_line():
    critical = ("sha256sum", "tar czf", "tar tzf", "openclaw backup create", "VACUUM", "mv ", "stat -c")
    for n, line in enumerate(_text("openclaw-backup.sh").splitlines(), 1):
        code = line.split("#", 1)[0]
        if any(k in code for k in critical):
            assert "|| true" not in code, f"openclaw-backup.sh:{n}"


def test_backup_uses_strict_mode_and_a_lock():
    text = _text("openclaw-backup.sh")
    assert "set -euo pipefail" in text and "flock -n" in text


# --- AC-4.2: nothing the host needs is left out of the backup -----------------------------------------------------------

def test_backup_lists_all_six_scripts_all_ten_units_and_the_host_env():
    text = _text("openclaw-backup.sh")
    for script in ("openclaw-backup.sh", "openclaw-maintenance.sh", "openclaw-watchdog.sh",
                   "openclaw-verify.sh", "openclaw-team-watch.py", "openclaw-team-send.py"):
        assert f".local/bin/{script}" in text
    for unit in ("openclaw-backup@.service", "openclaw-backup-daily.timer", "openclaw-backup-weekly.timer",
                 "openclaw-backup-monthly.timer", "openclaw-maintenance.service", "openclaw-maintenance.timer",
                 "openclaw-watchdog.service", "openclaw-watchdog.timer", "openclaw-verify.service",
                 "openclaw-verify.timer"):
        assert f".config/systemd/user/{unit}" in text
    assert ".openclaw/kit-host.env" in text and ".openclaw/bin" in text


def test_backup_reads_workspaces_from_the_config_not_from_a_literal_list():
    text = _text("openclaw-backup.sh")
    assert "workspaces()" in text and "OPENCLAW_BACKUP_EXTRA" in text


# --- maintenance keeps what is its own (AC-6.5) ----------------------------------------------------------------

def _executable(name: str) -> str:
    """The script without comments and without the prose inside `say "..."` report lines."""
    lines = (ln.split("#", 1)[0] for ln in _text(name).splitlines())
    return "\n".join(ln for ln in lines if 'say "' not in ln)


def test_maintenance_has_no_drain_and_no_doctor_fix_of_its_own():
    code = _executable("openclaw-maintenance.sh")
    assert "TasksCurrent" not in code
    assert "doctor --fix" not in code.replace("ai-resources openclaw doctor", "")
    assert 'systemctl --user stop' not in code and 'systemctl --user start' not in code


def test_maintenance_never_touches_the_pause_marker_itself():
    """The wrapper owns watchdog.off: removing it here would end another window's pause."""
    code = _executable("openclaw-maintenance.sh")
    assert "watchdog.off" not in code and "$OFF" not in code


def test_maintenance_calls_the_wrapper_exactly_once_and_keeps_its_own_checks(host):
    host.fresh_backup()
    r = host.run("openclaw-maintenance.sh")
    assert r.returncode == 0, r.stderr
    calls = host.calls()
    assert len([c for c in calls if c.startswith("ai-resources openclaw doctor")]) == 1
    assert "--cleanup-sessions" in "\n".join(calls)
    assert any(c.startswith("openclaw update status") for c in calls)
    assert not any(c.startswith("systemctl --user stop") for c in calls)
    log = (host.home / ".openclaw" / "logs" / "maintenance.log").read_text()
    assert "backups: 1 dailies on disk" in log


@pytest.mark.parametrize("rc,needle", [(2, "another maintenance window"), (3, "did not drain"),
                                       (4, "did not complete"), (5, "does NOT answer")])
def test_maintenance_reports_each_wrapper_failure_and_notifies(host, rc, needle):
    host.fresh_backup()
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.set_doctor_rc(rc)
    r = host.run("openclaw-maintenance.sh")
    assert r.returncode == 1
    log = "\n".join(host.calls())
    assert log.count("openclaw message send") == 1 and needle in log


def test_maintenance_is_quiet_when_everything_is_fine(host):
    host.fresh_backup()
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    assert host.run("openclaw-maintenance.sh").returncode == 0
    assert not any(c.startswith("openclaw message send") for c in host.calls())


def test_maintenance_flags_a_stale_backup(host):
    host.fresh_backup(age_hours=40)
    assert host.run("openclaw-maintenance.sh").returncode == 1


# --- openclaw-models-update.sh (the daily background model upgrade) ------------------------------------------------

def _approval_payload(cls="haiku", new="claude-haiku-5-5"):
    return {"rc": 10, "outcome": "pending_approval", "message": "", "deferrals": 0,
            "proposals": [{"cls": cls, "old": "claude-haiku-4-5", "new": new, "kind": "major", "price": "unknown",
                           "decision": "needs_approval", "reasons": ["major jump", "price unknown"]}]}


def _models_calls(host):
    return [c for c in host.calls() if c.startswith("ai-resources models")]


def test_models_update_stands_down_while_a_maintenance_window_is_open(host):
    (host.home / ".openclaw" / "watchdog.off").touch()
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    r = host.run("openclaw-models-update.sh")
    assert r.returncode == 0 and _models_calls(host) == [] and host.sent() == []


def test_models_update_runs_the_unattended_command(host):
    host.set_models(0, {"rc": 0, "outcome": "no_change", "message": "", "proposals": []})
    assert host.run("openclaw-models-update.sh").returncode == 0
    assert _models_calls(host) == ["ai-resources models update --unattended --json"]


def test_models_update_notifies_once_per_proposal_set_with_the_exact_approve_command(host):
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.set_models(10, _approval_payload())
    assert host.run("openclaw-models-update.sh").returncode == 10
    assert host.run("openclaw-models-update.sh").returncode == 10
    assert len(host.sent()) == 1
    assert "ai-resources models approve haiku claude-haiku-5-5" in "\n".join(host.calls())
    host.set_models(10, _approval_payload("opus", "claude-opus-5-5"))
    host.run("openclaw-models-update.sh")
    assert len(host.sent()) == 2


@pytest.mark.parametrize("rc", [1, 2, 4, 6])
def test_models_update_notifies_on_failures(host, rc):
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.set_models(rc, {"rc": rc, "outcome": "x", "message": "boom", "proposals": []})
    assert host.run("openclaw-models-update.sh").returncode == rc
    assert len(host.sent()) == 1
    if rc == 6:
        assert "CRITICAL" in "\n".join(host.calls())


def test_models_update_ignores_stray_output_around_the_json_report(host):
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    noisy = "warning: overlay unreadable {not json}\n" + json.dumps(_approval_payload()) + "\ngateway restart slow\n"
    host.set_models(10, noisy)
    assert host.run("openclaw-models-update.sh").returncode == 10
    assert len(host.sent()) == 1
    assert "ai-resources models approve haiku claude-haiku-5-5" in "\n".join(host.calls())
    host.set_models(0, "note: something\n" + json.dumps(
        {"rc": 0, "outcome": "switched", "message": "switched and healthy", "proposals": []}))
    host.run("openclaw-models-update.sh")
    assert len(host.sent()) == 2


def test_models_update_notifies_on_a_switch(host):
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.set_models(0, {"rc": 0, "outcome": "switched", "message": "switched and healthy", "proposals": []})
    host.run("openclaw-models-update.sh")
    assert len(host.sent()) == 1


@pytest.mark.parametrize("rc,payload,sent", [
    (73, {"rc": 73, "outcome": "locked", "proposals": []}, 0),
    (75, {"rc": 75, "outcome": "deferred", "deferrals": 1, "proposals": []}, 0),
    (75, {"rc": 75, "outcome": "deferred", "deferrals": 3, "proposals": []}, 1),
    (0, {"rc": 0, "outcome": "no_change", "proposals": []}, 0),
])
def test_models_update_is_quiet_when_there_is_nothing_to_say(host, rc, payload, sent):
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.set_models(rc, payload)
    host.run("openclaw-models-update.sh")
    assert len(host.sent()) == sent


def test_models_update_logs_every_run(host):
    host.set_models(0, {"rc": 0, "outcome": "no_change", "proposals": []})
    host.run("openclaw-models-update.sh")
    assert "rc=0" in (host.home / ".openclaw" / "logs" / "models-update.log").read_text()


def test_backup_enumerates_the_models_update_script_units_and_overlay():
    text = _text("openclaw-backup.sh")
    for needle in (".local/bin/openclaw-models-update.sh", ".config/systemd/user/openclaw-models-update.service",
                   ".config/systemd/user/openclaw-models-update.timer", ".config/ai-resources/model-pins.json"):
        assert needle in text


def test_models_update_unit_gives_the_restart_and_health_poll_time():
    text = (REPO / "templates" / "systemd" / "openclaw-models-update.service.template").read_text()
    assert "TimeoutStartSec=900" in text and "Type=oneshot" in text
    timer = (REPO / "templates" / "systemd" / "openclaw-models-update.timer.template").read_text()
    assert "OnCalendar=*-*-* 04:45:00" in timer and "RandomizedDelaySec=30min" in timer and "Persistent=true" in timer


def test_models_update_notice_names_a_non_claude_slot_in_full_and_says_newer_models(host):
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    payload = _approval_payload()
    payload["proposals"][0].update({"cls": "gemini-flash", "slot": "google:gemini-flash", "provider": "google",
                                    "new": "gemini-3.9-flash"})
    host.set_models(10, payload)
    assert host.run("openclaw-models-update.sh").returncode == 10
    calls = "\n".join(host.calls())
    assert "ai-resources models approve --slot google:gemini-flash gemini-3.9-flash" in calls
    assert "newer models are waiting" in calls and "newer Claude models" not in calls


def test_models_update_notice_keeps_the_short_form_for_a_claude_slot(host):
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    payload = _approval_payload()
    payload["proposals"][0]["slot"] = "anthropic:haiku"
    host.set_models(10, payload)
    host.run("openclaw-models-update.sh")
    assert "ai-resources models approve haiku claude-haiku-5-5" in "\n".join(host.calls())


def test_models_update_switch_notice_lists_the_re_rendered_files_instead_of_the_setup_hint(host):
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.set_models(0, {"rc": 0, "outcome": "switched", "message": "switched and healthy", "proposals": [],
                        "rerendered": ["executors", "litellm", "aider-conf"]})
    host.run("openclaw-models-update.sh")
    text = "\n".join(host.calls())
    assert "Re-rendered: executors, litellm, aider-conf." in text and "ai-resources setup" not in text.split("models updated")[-1]


# --- openclaw-health-restart.sh: never forces anything over live runs (ADR-0003, AC-4) ------------------------------------

def _hr(host, *args):
    return host.run("openclaw-health-restart.sh", *args)


def _doctor_calls(host):
    return [c for c in host.calls() if c.startswith("ai-resources openclaw doctor")]


def _graceful_calls(host):
    return [c for c in host.calls() if c.startswith("ai-resources openclaw graceful-restart")]


def _forbidden_calls(host, *, mode_on: bool = False):
    """Calls that change the gateway. The graceful-restart CLI is the one sanctioned mutation, and only
    in mode `on` (ADR-0004); everywhere else it is as forbidden as the doctor or a bare systemctl."""
    bad = [c for c in host.calls() if c.startswith("ai-resources openclaw doctor")
           or re.match(r"systemctl --user (restart|stop|start)\b", c)]
    if not mode_on:
        bad += _graceful_calls(host)
    return bad


@pytest.mark.parametrize("busy_rc", [1, 2])
def test_busy_or_unknown_with_a_health_failure_never_acts_and_notifies_once(host, busy_rc):
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.set_health(1)
    host.set_busy_rc(busy_rc)
    assert _hr(host).returncode == 0
    assert _forbidden_calls(host) == []
    assert len(host.sent()) == 1
    # A second tick in the same episode stays quiet.
    assert _hr(host).returncode == 0
    assert _forbidden_calls(host) == [] and len(host.sent()) == 1


def test_a_new_episode_notifies_again_after_the_gateway_was_healthy(host):
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.set_busy_rc(1)
    host.set_health(1)
    _hr(host)
    host.set_health(0)
    _hr(host)                      # healthy: the episode ends
    host.set_health(1)
    _hr(host)
    assert len(host.sent()) == 2


@pytest.mark.parametrize("trigger", ["stalls", "health"])
def test_the_health_and_stalls_triggers_honour_the_busy_probe(host, trigger):
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42", OPENCLAW_GRACEFUL_RESTART="on")
    host.set_busy_rc(1)
    if trigger == "stalls":
        host.set_stalls(8)
    else:
        host.set_health(1)
    _hr(host)
    assert _forbidden_calls(host) == [] and len(host.sent()) == 1


def _pressure_env(host, **extra):
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42", **extra)
    host.set_busy_rc(1)                 # 6-12 runs in flight is this host's normal state
    host.set_pressure("pressure")


OK_RESULT = {"result": "ok", "exit_code": 0, "reason": "memory", "memory_before": 14 * 1024 ** 3,
             "memory_after": 6 * 1024 ** 3, "report": "/snap/report.txt", "snapshot_dir": "/snap",
             "recovery": {"marked_interrupted": 3, "aborted_runs": 4, "recovery_started": 1, "tombstoned": 0}}


def test_confirmed_memory_pressure_with_runs_in_flight_in_window_mode_on_restarts_once(host):
    _pressure_env(host, OPENCLAW_GRACEFUL_RESTART="on")
    host.set_graceful(0, OK_RESULT)
    r = _hr(host)
    assert r.returncode == 0, r.stderr
    [call] = _graceful_calls(host)
    assert "--reason memory" in call and "--classification pressure" in call and "--force" not in call
    assert _doctor_calls(host) == [], "the memory path never goes through the doctor"
    [notice] = host.sent()
    assert "14.0 GiB -> 6.0 GiB" in notice and "3 marked interrupted" in notice and "4 aborted" in notice
    assert "/snap/report.txt" in notice


def test_mode_notify_and_mode_off_never_call_graceful_restart(host):
    _pressure_env(host, OPENCLAW_GRACEFUL_RESTART="notify")
    assert _hr(host).returncode == 0
    assert _graceful_calls(host) == [] and len(host.sent()) == 1
    assert "nothing was restarted" in host.sent()[0]
    host.log.write_text("", encoding="utf-8")
    (host.home / ".openclaw" / "logs" / "health-restart.notified").unlink()
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42", OPENCLAW_GRACEFUL_RESTART="off")
    assert _hr(host).returncode == 0
    assert _graceful_calls(host) == [] and host.sent() == []


def test_an_unrecognised_mode_fails_safe_to_notify(host):
    _pressure_env(host, OPENCLAW_GRACEFUL_RESTART="yes please")
    assert _hr(host).returncode == 0
    assert _graceful_calls(host) == [] and len(host.sent()) == 1


def test_frozen_exits_two_notifies_once_and_never_restarts(host):
    _pressure_env(host, OPENCLAW_GRACEFUL_RESTART="on")
    host.set_pressure("frozen", mem=114.0, frozen_by="memory-throttle")
    assert _hr(host).returncode == 2
    assert _forbidden_calls(host, mode_on=True) == [] and _graceful_calls(host) == []
    assert len(host.sent()) == 1 and "frozen" in host.sent()[0] and "T41" in host.sent()[0]
    assert _hr(host).returncode == 2 and len(host.sent()) == 1, "a second tick of the same episode is quiet"


def test_a_frozen_notice_that_could_not_be_sent_is_retried_on_the_next_tick(host):
    _pressure_env(host)
    host.set_pressure("frozen", mem=114.0, frozen_by="memory-throttle")
    host.set_send_rc(1)
    assert _hr(host).returncode == 2
    assert len(host.sent()) == 1                    # attempted, not delivered
    host.set_send_rc(0)
    assert _hr(host).returncode == 2
    assert len(host.sent()) == 2                    # retried and delivered
    assert _hr(host).returncode == 2
    assert len(host.sent()) == 2                    # and then quiet


def test_a_probe_that_cannot_read_a_signal_never_acts_or_notifies(host):
    _pressure_env(host, OPENCLAW_GRACEFUL_RESTART="on")
    host.set_pressure("refused-probe", reason="memory.current: No such file or directory")
    assert _hr(host).returncode == 0
    assert _forbidden_calls(host, mode_on=True) == [] and host.sent() == []


def test_a_missing_pressure_command_is_a_refused_probe(host):
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42", OPENCLAW_GRACEFUL_RESTART="on")
    (host.bin / "pressure-out").write_text("not json at all", encoding="utf-8")
    assert _hr(host).returncode == 0
    assert _forbidden_calls(host, mode_on=True) == [] and host.sent() == []


def test_a_gate_refusal_is_one_notice_naming_the_gate(host):
    _pressure_env(host, OPENCLAW_GRACEFUL_RESTART="on")
    host.set_graceful(10, {"result": "refused-gate", "gate": "cooldown", "exit_code": 10,
                           "gates": [{"name": "cooldown", "ok": False, "detail": "last restart 600s ago, cooldown 10800s"}]})
    assert _hr(host).returncode == 0
    assert len(host.sent()) == 1 and "cooldown" in host.sent()[0]
    assert _hr(host).returncode == 0 and len(host.sent()) == 1


def test_a_failed_restart_is_one_notice_exit_one_and_no_marker_left(host):
    _pressure_env(host, OPENCLAW_GRACEFUL_RESTART="on")
    host.set_graceful(11, {"result": "failed", "exit_code": 11, "snapshot_dir": "/snap"})
    r = _hr(host)
    assert r.returncode == 1 and len(host.sent()) == 1 and "failed" in host.sent()[0]
    assert not (host.home / ".openclaw" / "watchdog.off").exists()


def test_two_ticks_in_one_episode_send_one_notice_and_a_healthy_tick_starts_a_new_episode(host):
    _pressure_env(host, OPENCLAW_GRACEFUL_RESTART="notify")
    _hr(host)
    _hr(host)
    assert len(host.sent()) == 1
    host.set_pressure("healthy", mem=10.0, swap=0.0)
    _hr(host)
    host.set_pressure("pressure")
    _hr(host)
    assert len(host.sent()) == 2


def test_dry_run_in_mode_on_passes_dry_run_and_changes_nothing(host):
    _pressure_env(host, OPENCLAW_GRACEFUL_RESTART="on")
    host.set_graceful(0, {"result": "dry-run", "exit_code": 0})
    assert _hr(host, "--dry-run").returncode == 0
    [call] = _graceful_calls(host)
    assert "--dry-run" in call
    assert host.sent() == []
    assert not (host.home / ".openclaw" / "health-restart.state").exists()
    assert not (host.home / ".openclaw" / "watchdog.off").exists()
    assert not (host.home / ".openclaw" / "logs" / "health-restart.notified").exists()


def test_dry_run_with_frozen_sends_nothing_and_writes_no_episode_file(host):
    _pressure_env(host)
    host.set_pressure("frozen", mem=114.0, frozen_by="memory-throttle")
    assert _hr(host, "--dry-run").returncode == 0
    assert host.sent() == [] and not (host.home / ".openclaw" / "logs" / "health-restart.notified").exists()


def test_idle_with_a_health_failure_runs_the_drained_doctor_once_and_writes_the_cooldown(host):
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.set_health(1)
    host.set_busy_rc(0)
    assert _hr(host).returncode == 0
    assert len(_doctor_calls(host)) == 1
    assert not any(re.match(r"systemctl --user (restart|stop|start)\b", c) for c in host.calls()), \
        "the script itself never stops or starts anything"
    state = (host.home / ".openclaw" / "health-restart.state").read_text()
    assert re.search(r"^last_restart=\d+$", state, re.M)
    # Inside the cooldown a second tick does nothing at all.
    host.log.write_text("", encoding="utf-8")
    assert _hr(host).returncode == 0
    assert _doctor_calls(host) == [] and host.sent() == []


def test_a_failed_drained_doctor_is_reported_and_exits_non_zero(host):
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.set_health(1)
    host.set_doctor_rc(4)
    r = _hr(host)
    assert r.returncode == 1 and len(host.sent()) == 1


def test_a_healthy_gateway_does_nothing(host):
    assert _hr(host).returncode == 0
    assert host.sent() == [] and _forbidden_calls(host) == []


def test_watchdog_off_stands_the_health_check_down(host):
    (host.home / ".openclaw" / "watchdog.off").touch()
    host.set_health(1)
    assert _hr(host).returncode == 0
    assert not any(c.startswith(("openclaw health", "ai-resources")) for c in host.calls())


@pytest.mark.parametrize("state", ["inactive", "failed", "activating", "deactivating"])
def test_a_gateway_that_is_not_active_is_left_to_the_watchdog(host, state):
    host.set_active(state)
    host.set_health(1)
    assert _hr(host).returncode == 0
    assert _forbidden_calls(host) == [] and host.sent() == []


def test_dry_run_decides_but_does_not_act(host):
    host.write_env(OPENCLAW_OWNER_TELEGRAM_ID="42")
    host.set_health(1)
    assert _hr(host, "--dry-run").returncode == 0
    assert _doctor_calls(host) == []
    # ADR-0004: stricter than 2.0.2. No notice, no state, no episode file, no marker.
    assert host.sent() == []
    assert not (host.home / ".openclaw" / "health-restart.state").exists()
    assert not (host.home / ".openclaw" / "logs" / "health-restart.notified").exists()
    assert not (host.home / ".openclaw" / "watchdog.off").exists()


def test_the_only_gateway_mutation_is_the_graceful_restart_cli():
    code = "\n".join(ln.split("#", 1)[0] for ln in _text("openclaw-health-restart.sh").splitlines())
    assert not re.search(r"systemctl\s+--user\s+(restart|stop|start|kill)", code)
    assert "gateway restart" not in code and "--force" not in code
    assert "kill" not in code.replace("skipping", "")
    assert not re.search(r"\b\d{6,}\b", code), "no hard-coded chat id or other long numeric literal"
    assert "OPENCLAW_OWNER_TELEGRAM_ID" in _text("_common.sh")
    call_sites = re.findall(r"ai-resources openclaw graceful-restart", code)
    assert len(call_sites) == 1, "exactly one call site (a dry run is the same call with --dry-run)"
    # the gate logic is not duplicated in bash: no cooldown arithmetic for the memory path, no window parsing
    assert "OPENCLAW_RESTART_WINDOW" not in code and "OPENCLAW_RESTART_DAILY_CAP" not in code


def test_the_health_restart_units_carry_the_adr_0004_budget_and_cadence():
    service = (REPO / "templates" / "systemd" / "openclaw-health-restart.service.template").read_text()
    timer = (REPO / "templates" / "systemd" / "openclaw-health-restart.timer.template").read_text()
    assert "TimeoutStartSec=25min" in service and "ADR-0004" in service
    assert "OnUnitActiveSec=15min" in timer and "OnBootSec=30min" in timer
    # the budget comment names every term of the sum, so a future edit knows what it must keep
    for term in ("confirm 60 s", "restart up to 600 s", "retries 140 s", "health 180 s", "settle 120 s"):
        assert term in service, term


def test_the_script_defaults_ship_mode_notify_and_the_operators_window():
    common = _text("_common.sh")
    for needle in (": \"${OPENCLAW_GRACEFUL_RESTART:=notify}\"", "OPENCLAW_RESTART_WINDOW=02:00-05:00",
                   ": \"${OPENCLAW_RESTART_TZ:=America/Guayaquil}\"", ": \"${OPENCLAW_RESTART_HARD_PCT:=105}\"",
                   ": \"${OPENCLAW_RESTART_COOLDOWN_S:=10800}\"", ": \"${OPENCLAW_RESTART_DAILY_CAP:=2}\"",
                   ": \"${OPENCLAW_HEALTH_CONFIRM_S:=60}\""):
        assert needle in common, needle


def test_the_bash_and_python_knob_defaults_are_one_set():
    import sys
    sys.path.insert(0, str(REPO / "scripts"))
    from ai_resources import openclaw_host as oh
    found = dict(re.findall(r': "\$\{(OPENCLAW_[A-Z_]+):?=([^}]*)\}"', _text("_common.sh")))
    for key, default in oh.RESTART_KNOB_DEFAULTS.items():
        assert found.get(key) == default, f"{key}: bash {found.get(key)!r} vs python {default!r}"

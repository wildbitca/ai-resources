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
        for name in ("systemctl", "loginctl", "openclaw", "ai-resources", "kubectl", "flux", "sleep", "curl"):
            self._stub(name)
        self.env_file = self.home / ".openclaw" / "kit-host.env"

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
  "systemctl --user list-timers") for i in 1 2 3 4 5 6; do echo "n openclaw-t$i.timer"; done; exit 0 ;;
  "systemctl --user show") echo "{'[not set]'}"; exit 0 ;;
  "loginctl show-user"*) echo yes; exit 0 ;;
  "openclaw health "*) exit "$(cat "{self.bin}/health-rc" 2>/dev/null || echo 0)" ;;
  "ai-resources openclaw"*) echo "Repaired legacy bindings 2"; exit "$(cat "{self.bin}/doctor-rc" 2>/dev/null || echo 0)" ;;
esac
exit 0
""", encoding="utf-8")
        script.chmod(0o755)

    def set_active(self, value: str):
        (self.bin / "is-active").write_text(value + "\n", encoding="utf-8")

    def set_doctor_rc(self, rc: int):
        (self.bin / "doctor-rc").write_text(str(rc), encoding="utf-8")

    def set_health(self, rc: int):
        (self.bin / "health-rc").write_text(str(rc), encoding="utf-8")

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
            "OPENCLAW_HOST_ENV": str(self.env_file),
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


def test_the_verify_script_expects_six_timers():
    assert re.search(r'-ge 6\b', _text("openclaw-verify.sh"))
    assert not re.search(r'-ge 5\b', _text("openclaw-verify.sh"))


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

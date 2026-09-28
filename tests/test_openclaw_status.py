"""`ai-resources openclaw status`: nine sections from fixture command output, never a repair.

The runner answers from canned output; nothing here reaches systemd, ss, openclaw or a bucket.
Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import openclaw_host as host  # noqa: E402

FIXTURES = REPO / "tests" / "fixtures" / "openclaw_status"
NOW = 1_800_000_000.0


class Canned:
    def __init__(self, doctor: str = "", offbox_listing: str = "", health_rc: int = 0, ss: str = "",
                 doctor_rc: int = 0):
        self.doctor_rc = doctor_rc
        self.timeouts: dict[str, float | None] = {}
        self.doctor, self.offbox_listing, self.health_rc = doctor, offbox_listing, health_rc
        self.ss = ss or ("LISTEN 0 511 127.0.0.1:18789 0.0.0.0:*\n"
                         "LISTEN 0 511 100.64.0.9:18789 0.0.0.0:*\nLISTEN 0 4096 0.0.0.0:22 0.0.0.0:*\n")
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kw):
        self.calls.append(list(argv))
        a = list(argv)
        if a and os.path.basename(a[0]) == "openclaw":
            self.timeouts[a[1]] = _kw.get("timeout")
            a[0] = "openclaw"
        if a[:3] == ["systemctl", "--user", "is-active"]:
            return 0, "active"
        if a[:3] == ["systemctl", "--user", "is-enabled"]:
            return 0, "enabled"
        if a[:3] == ["systemctl", "--user", "show"]:
            return 0, "Sun 2026-09-20 03:30:00 UTC"
        if a[0] == "loginctl":
            return 0, "yes"
        if a[0] == "ss":
            return 0, self.ss
        if a[:2] == ["openclaw", "health"]:
            return self.health_rc, ""
        if a[:2] == ["openclaw", "doctor"]:
            return self.doctor_rc, self.doctor
        if a[0] == "remote-ls":
            return 0, self.offbox_listing
        raise AssertionError(f"status must not run {argv}")


OC_BIN = "/opt/oc/bin/openclaw"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(host, "resolve_openclaw_bin", lambda: OC_BIN)
    home = tmp_path / "home"
    (home / ".openclaw").mkdir(parents=True)
    (home / ".openclaw" / "openclaw.json").write_text(json.dumps({
        "gateway": {"port": 18789},
        "agents": {"defaults": {"model": {"primary": "anthropic/claude-haiku-4-5"}},
                   "entries": {"main": {"model": {"primary": "anthropic/claude-sonnet-5"}}, "util": {}}}}), encoding="utf-8")
    backups = tmp_path / "backups"
    for t in ("daily", "weekly", "monthly"):
        (backups / t).mkdir(parents=True)
    hostenv = home / ".openclaw" / "kit-host.env"
    hostenv.write_text(f"OPENCLAW_BACKUP_DIR={backups}\n", encoding="utf-8")

    def backup(tier, name, age_hours, size=2048):
        f = backups / tier / name
        f.write_bytes(b"x" * size)
        f.with_name(name + ".sha256").write_text("x", encoding="utf-8")
        ts = NOW - age_hours * 3600
        os.utime(f, (ts, ts))

    class E:
        pass
    e = E()
    e.home, e.backups, e.hostenv, e.backup = home, backups, hostenv, backup
    return e


def collect(env, canned):
    return host.collect_status(canned, home=env.home, host_env_path=env.hostenv, now=NOW)


def doctor_text(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- AC-12.1 ------------------------------------------------------------------------------------------------------

def test_the_report_has_all_nine_sections_from_fixture_output(env):
    env.backup("daily", "openclaw-1.tar.gz", 3)
    report = collect(env, Canned(doctor=doctor_text("doctor_noise_only.txt")))
    assert list(report) == list(host.STATUS_SECTIONS) and len(report) == 9
    text = host.render_status(report)
    for needle in ("unit ", "boot ", "listeners ", "health ", "timers", "backups", "off-box", "models", "doctor "):
        assert needle in text
    assert text.count(".timer") == 6
    assert "main" in text and "anthropic/claude-haiku-4-5 (default)" in text  # effective model per agent


def _write_models(env, defaults, entries):
    cfg = env.home / ".openclaw" / "openclaw.json"
    doc = json.loads(cfg.read_text(encoding="utf-8"))
    doc["agents"] = {"defaults": {"model": defaults}, "entries": entries}
    cfg.write_text(json.dumps(doc), encoding="utf-8")


def test_short_form_models_render_like_the_long_form(env):
    long_form = {"main": {"model": {"primary": "anthropic/claude-sonnet-5"}}, "util": {}}
    short_form = {"main": {"model": "anthropic/claude-sonnet-5"}, "util": {}}
    _write_models(env, {"primary": "anthropic/claude-haiku-4-5"}, long_form)
    as_long = host.render_status(collect(env, Canned()))
    _write_models(env, "anthropic/claude-haiku-4-5", short_form)
    report = collect(env, Canned())
    assert [m["model"] for m in report["models"]] == ["anthropic/claude-sonnet-5",
                                                       "anthropic/claude-haiku-4-5 (default)"]
    short = [ln for ln in host.render_status(report).splitlines() if ln.startswith("  ") and not ln.startswith("   ") and "anthropic" in ln]
    assert short == [ln for ln in as_long.splitlines() if ln.startswith("  ") and not ln.startswith("   ") and "anthropic" in ln]


def test_a_short_form_model_gets_the_remedy_line_and_a_long_form_does_not(env):
    _write_models(env, {"primary": "anthropic/claude-haiku-4-5"},
                  {"main": {"model": "anthropic/claude-sonnet-5"}, "app": {"model": {"primary": "x/y"}}})
    report = collect(env, Canned())
    assert {m["agent"]: m["shorthand"] for m in report["models"]} == {"main": True, "app": False}
    text = host.render_status(report)
    assert "openclaw config set agents.entries.main.model.primary anthropic/claude-sonnet-5" in text
    assert "agents.entries.app.model.primary" not in text
    _write_models(env, {"primary": "anthropic/claude-haiku-4-5"}, {"app": {"model": {"primary": "x/y"}}})
    assert "openclaw config set" not in host.render_status(collect(env, Canned()))


def test_status_sets_the_environment_systemctl_needs_and_only_runs_probes(env, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    seen = []

    class Spy(Canned):
        def __call__(self, argv, **kw):
            seen.append(kw.get("env") or {})
            return super().__call__(argv, **kw)
    canned = Spy(doctor="")
    collect(env, canned)
    assert all("XDG_RUNTIME_DIR" in e and "DBUS_SESSION_BUS_ADDRESS" in e for e in seen)
    forbidden = ("start", "stop", "restart", "enable", "disable", "set-property", "--fix", "reload")
    for call in canned.calls:
        assert not any(tok in forbidden for tok in call), call
    assert not any(c[1:3] == ["config", "set"] or c[1:3] == ["config", "patch"] for c in canned.calls)


def test_status_never_fails_the_shell_and_never_writes(env, tmp_path, capsys):
    before = sorted(p for p in env.home.rglob("*"))
    text = host.render_status(collect(env, Canned(doctor="", health_rc=1)))
    assert "DOES NOT ANSWER" in text
    assert sorted(p for p in env.home.rglob("*")) == before


def test_a_wildcard_listener_on_the_gateway_port_is_flagged(env):
    canned = Canned(ss="LISTEN 0 511 0.0.0.0:18789 0.0.0.0:*\n")
    assert "ATTENTION: bound to all interfaces" in host.render_status(collect(env, canned))
    assert "ATTENTION" not in host.render_status(collect(env, Canned()))  # tailnet + loopback only


# --- AC-12.2: the T30 noise catalogue ---------------------------------------------------------------------------------------

def test_a_doctor_report_with_only_known_noise_reads_clean(env):
    report = collect(env, Canned(doctor=doctor_text("doctor_noise_only.txt")))
    assert report["doctor"]["signal"] == [] and report["doctor"]["noise"] >= 7
    assert "clean" in host.render_status(report)


def test_an_unknown_warning_is_surfaced_verbatim(env):
    report = collect(env, Canned(doctor=doctor_text("doctor_with_signal.txt")))
    assert report["doctor"]["signal"] == ["[warning] core/doctor/example - The kit had never seen this warning before."]
    assert "The kit had never seen this warning before." in host.render_status(report)


@pytest.mark.parametrize("name", [n for n, _ in host.DOCTOR_NOISE])
def test_every_noise_pattern_matches_its_own_line_and_none_other(name):
    samples = {
        "heap": "runtime V8 ceiling: not measured",
        "shell-path": "PATH missing required dirs: /x/fnm_multishells/1_2/bin",
        "owned-unit": 'Run "openclaw gateway install --force" when you want to replace the unit',
        "desktop": "Host desktop disabled",
        "legacy-bindings": "Legacy session bindings or retired session model route state detected.",
        "whisper": "whisper-cli backend cannot be proven without loading a model",
        "privacy-mode": "telegram bot privacy mode is on",
        "dashboard-conflict": 'Plugin command "/dashboard" conflicts with an existing Telegram command',
        "claude-kit-model": "issue: Unknown model: claude-kit/claude-sonnet-5. Run `openclaw models list`",
    }
    matched = [n for n, pat in host.DOCTOR_NOISE if pat.search(samples[name])]
    assert matched == [name]


def test_wrapped_bullets_are_joined_before_matching():
    text = doctor_text("doctor_noise_only.txt")
    entries = [e for _, e in host.parse_doctor_entries(text)]
    assert any(e.startswith("Legacy session bindings or retired session model route state detected.") for e in entries)
    assert any("recommend a minimal PATH." in e for e in entries)


# --- AC-12.3: backups ----------------------------------------------------------------------------------------------------------------------

def test_a_daily_backup_older_than_36_hours_is_flagged_and_status_still_exits_zero(env, capsys):
    env.backup("daily", "openclaw-old.tar.gz", 40)
    report = collect(env, Canned())
    assert report["backups"]["daily"]["stale"] is True
    assert "STALE" in host.render_status(report)
    assert host.cmd_status.__name__ == "cmd_status"


def test_a_fresh_daily_is_not_flagged(env):
    env.backup("daily", "openclaw-new.tar.gz", 35)
    assert collect(env, Canned())["backups"]["daily"]["stale"] is False


def test_the_newest_file_of_each_tier_is_the_one_reported(env):
    env.backup("weekly", "openclaw-a.tar.gz", 100)
    env.backup("weekly", "openclaw-b.tar.gz", 20)
    info = collect(env, Canned())["backups"]["weekly"]
    assert info["name"] == "openclaw-b.tar.gz" and info["count"] == 2 and info["stale"] is False


def test_a_tarball_without_its_checksum_is_called_out(env):
    env.backup("daily", "openclaw-x.tar.gz", 1)
    (env.backups / "daily" / "openclaw-x.tar.gz.sha256").unlink()
    assert "NO .sha256" in host.render_status(collect(env, Canned()))


def test_a_missing_tier_is_reported_as_none(env):
    assert "none" in host.render_status(collect(env, Canned()))


# --- off-box ---------------------------------------------------------------------------------------------------------------------------------------

def test_off_box_is_reported_as_unconfigured_by_default(env):
    assert "not configured" in host.render_status(collect(env, Canned()))


def test_off_box_presence_of_the_newest_daily_is_checked_through_the_configured_command(env):
    env.backup("daily", "openclaw-20260101.tar.gz", 2)
    env.hostenv.write_text(env.hostenv.read_text(encoding="utf-8") + "OPENCLAW_OFFBOX_LIST_CMD=remote-ls bucket/daily/\n",
                           encoding="utf-8")
    present = host.render_status(collect(env, Canned(offbox_listing="openclaw-20260101.tar.gz\n")))
    missing = host.render_status(collect(env, Canned(offbox_listing="openclaw-19990101.tar.gz\n")))
    assert "is present" in present and "MISSING off-box" in missing


def test_the_cli_registers_status():
    from ai_resources import cli
    assert cli.build_parser().parse_args(["openclaw", "status"]).func is host.cmd_status


# --- E3: resolve the binary, name the budget, tell the truth ------------------------------------------------------

def _doctor_calls(canned):
    return [c for c in canned.calls if os.path.basename(c[0]) == "openclaw" and c[1] == "doctor"]


def test_health_and_doctor_run_the_absolute_resolved_binary_with_the_named_budget(env):
    canned = Canned(doctor="")
    collect(env, canned)
    assert [c[0] for c in canned.calls if c[1:2] in (["health"], ["doctor"])] == [OC_BIN, OC_BIN]
    assert canned.timeouts["doctor"] == host.DOCTOR_TIMEOUT == 600


@pytest.mark.parametrize("rc,status,needle", [
    (127, "missing", "was not found"),
    (124, "timeout", "timed out after 600s"),
    (2, "failed", "failed (rc=2)"),
])
def test_each_doctor_failure_is_told_apart(env, rc, status, needle):
    report = collect(env, Canned(doctor="", doctor_rc=rc))
    assert report["doctor"]["status"] == status and report["doctor"]["ran"] is False
    text = host.render_status(report)
    assert needle in text and "could not run" not in text
    if rc == 127:
        assert OC_BIN in text and "PATH" in text
    if rc == 124:
        assert "ai-resources openclaw doctor" in text


def test_a_clean_doctor_keeps_the_legacy_ran_key_and_the_noise_verdict(env):
    report = collect(env, Canned(doctor=doctor_text("doctor_noise_only.txt")))
    assert report["doctor"]["ran"] is True and report["doctor"]["status"] == "ok"
    assert "clean (" in host.render_status(report)


def test_no_doctor_issues_no_doctor_argv_at_all(env):
    canned = Canned(doctor="")
    report = host.collect_status(canned, home=env.home, host_env_path=env.hostenv, now=NOW, run_doctor=False)
    assert _doctor_calls(canned) == []
    assert report["doctor"] == {"ran": False, "status": "skipped"}
    assert "doctor      not run (--no-doctor)" in host.render_status(report)


def test_the_status_verb_takes_no_doctor():
    from ai_resources import cli
    assert cli.build_parser().parse_args(["openclaw", "status", "--no-doctor"]).no_doctor is True


def test_the_default_runner_tells_a_timeout_from_an_oserror(monkeypatch):
    import subprocess

    def boom(exc):
        def run(*_a, **_k):
            raise exc
        return run
    monkeypatch.setattr(host.subprocess, "run", boom(subprocess.TimeoutExpired("x", 1)))
    assert host.default_runner(["x"])[0] == 124
    monkeypatch.setattr(host.subprocess, "run", boom(PermissionError("nope")))
    assert host.default_runner(["x"])[0] == 1
    monkeypatch.setattr(host.subprocess, "run", boom(FileNotFoundError()))
    assert host.default_runner(["x"])[0] == 127


def test_the_live_claude_kit_model_warning_is_noise_and_leaves_no_signal():
    noise, signal = host.filter_doctor_warnings(doctor_text("doctor_live_claude_kit.txt"))
    assert signal == []
    assert any("Unknown model: claude-kit/claude-sonnet-5" in n for n in noise)


def test_an_unknown_model_of_a_real_provider_stays_signal():
    text = doctor_text("doctor_live_claude_kit.txt").replace("claude-kit/claude-sonnet-5", "anthropic/some-model")
    _noise, signal = host.filter_doctor_warnings(text)
    assert any("Unknown model: anthropic/some-model" in s for s in signal)

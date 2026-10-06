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

def test_the_report_has_all_eleven_sections_from_fixture_output(env):
    env.backup("daily", "openclaw-1.tar.gz", 3)
    report = collect(env, Canned(doctor=doctor_text("doctor_noise_only.txt")))
    assert list(report) == list(host.STATUS_SECTIONS) and len(report) == 11
    text = host.render_status(report)
    for needle in ("unit ", "boot ", "listeners ", "health ", "timers", "backups", "off-box", "models", "identity", "stability ", "doctor "):
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


# --- E4: identity, where the operator saw the wrong name ---------------------------------------------------------------

def test_two_agents_sharing_a_workspace_show_both_names_the_shared_path_and_the_remedy(env, tmp_path):
    shared = tmp_path / "Development"
    shared.mkdir()
    ident = shared / "IDENTITY.md"
    ident.write_text("# IDENTITY\n\n- Name: security\n", encoding="utf-8")
    cfg = env.home / ".openclaw" / "openclaw.json"
    doc = json.loads(cfg.read_text(encoding="utf-8"))
    doc["agents"]["entries"] = {"claude": {"workspace": str(shared), "identity": None},
                                "security": {"workspace": str(shared), "identity": {"name": "security"}},
                                "main": {"workspace": str(tmp_path / "main"), "identity": {"name": "Jarvis"}}}
    cfg.write_text(json.dumps(doc), encoding="utf-8")
    before = {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}
    text = host.render_status(collect(env, Canned()))
    identity = text[text.index("identity"):text.index("doctor")]
    assert "claude" in identity and "security" in identity and str(shared) in identity
    assert "IDENTITY.md: security" in identity and "Jarvis" in identity
    assert "openclaw config set agents.entries.claude.identity.name claude --dry-run" in identity
    assert "agents.entries.security.identity.name" not in identity
    assert {p: p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()} == before


def test_a_workspace_of_one_named_agent_carries_no_attention_line(env):
    assert "ATTENTION: " not in host.render_status(collect(env, Canned())).split("identity")[1].split("doctor")[0]


@pytest.mark.parametrize("text,expected", [
    ("# IDENTITY\n\n- Name: security\n", "security"),
    ("- **Name:** Jarvis\n", "Jarvis"),
    ("- **Name**: Jarvis\n", "Jarvis"),
    ("- **Name:**\n  _(pick something you like)_\n- **Creature:**\n", None),   # the unfilled template
    ("- Name:\n", None),
    ("no name here\n", None),
])
def test_the_identity_name_line_is_read_and_an_unfilled_template_is_no_name(tmp_path, text, expected):
    (tmp_path / "IDENTITY.md").write_text(text, encoding="utf-8")
    assert host.identity_file_name(tmp_path) == expected


# --- T39: the newest stability bundle -------------------------------------------------------------------------

import datetime as _dt
import shutil

STABILITY = REPO / "tests" / "fixtures" / "stability"
B_1006 = "openclaw-stability-2026-10-06T17-14-23-353Z-1051-gateway.stop_shutdown_timeout.json"
B_0918 = "openclaw-stability-2026-09-18T01-25-07-371Z-485771-gateway.stop_shutdown_timeout.json"
B_BAD = "openclaw-stability-2026-10-07T00-00-00-000Z-1-gateway.stop_close_failed.json"
T_1006 = _dt.datetime(2026, 10, 6, 17, 14, 23, tzinfo=_dt.timezone.utc).timestamp()


def _bundles(dest, *names):
    dest.mkdir(parents=True, exist_ok=True)
    for n in names:
        shutil.copy(STABILITY / n, dest / n)
    return dest


def test_stability_summary_of_the_10_06_shape(tmp_path):
    s = host.stability_summary(_bundles(tmp_path / "s", B_1006), now=T_1006 + 3 * 3600)
    assert s["present"] and s["reason"] == "gateway.stop_shutdown_timeout"
    assert s["stalled"] == {"blocked_tool_call": 6, "active_work_without_progress": 2}
    assert s["tools"] == {"Bash": 4, "mcp__openclaw__ask_user": 2}
    assert s["long_running"] == 5 and s["dropped"] == 9154
    assert s["max_stalled_age_s"] == 1980 and abs(s["age_hours"] - 3) < 0.01


def test_stability_summary_of_a_bundle_without_stalls(tmp_path):
    s = host.stability_summary(_bundles(tmp_path / "s", B_0918), now=T_1006)
    assert s["present"] and s["stalled"] == {} and s["tools"] == {} and "error" not in s


def test_stability_summary_survives_corrupt_json(tmp_path):
    s = host.stability_summary(_bundles(tmp_path / "s", B_BAD), now=T_1006)
    assert s["present"] and "error" in s


def test_stability_summary_without_a_directory(tmp_path):
    assert host.stability_summary(tmp_path / "nope") == {"present": False}


def test_stability_summary_picks_the_newest_name_not_the_newest_mtime(tmp_path):
    d = _bundles(tmp_path / "s", B_1006, B_0918)
    os.utime(d / B_1006, (1, 1))
    os.utime(d / B_0918, (NOW, NOW))
    assert host.stability_summary(d, now=T_1006)["name"] == B_1006


def test_stability_summary_skips_an_oversized_bundle(tmp_path, monkeypatch):
    d = _bundles(tmp_path / "s", B_1006)
    monkeypatch.setattr(host, "STABILITY_MAX_BYTES", 10)
    assert "error" in host.stability_summary(d, now=T_1006)


def test_stability_summary_falls_back_to_the_summary_counts(tmp_path):
    d = tmp_path / "s"
    d.mkdir()
    (d / B_1006).write_text(json.dumps({"version": 1, "reason": "gateway.stop_shutdown_timeout",
                                        "generatedAt": "2026-10-06T17:14:23.353Z",
                                        "snapshot": {"summary": {"byType": {"session.stalled": 73}}}}),
                            encoding="utf-8")
    assert host.stability_summary(d, now=T_1006)["stalled"] == {"unknown": 73}


def test_stability_summary_never_returns_event_payloads(tmp_path):
    s = host.stability_summary(_bundles(tmp_path / "s", B_1006), now=T_1006)
    assert "events" not in json.dumps(s)


MALFORMED = {
    "snapshot is a list": {"version": 1, "snapshot": [1]},
    "events is an int": {"version": 1, "snapshot": {"events": 5}},
    "summary is a list": {"version": 1, "snapshot": {"summary": [1]}},
    "byType is a list": {"version": 1, "snapshot": {"summary": {"byType": [1]}}},
    "non-numeric stalled count": {"version": 1, "snapshot": {"summary": {"byType": {"session.stalled": "many"}}}},
    "non-numeric long_running count": {"version": 1, "snapshot": {"summary": {"byType": {"session.long_running": "x"}}}},
    "non-numeric dropped": {"version": 1, "snapshot": {"events": [], "dropped": "lots"}},
    "list-valued dropped": {"version": 1, "snapshot": {"events": [], "dropped": [1]}},
}


@pytest.mark.parametrize("label", sorted(MALFORMED))
def test_stability_summary_never_raises_on_a_malformed_bundle(tmp_path, label):
    d = tmp_path / "s"
    d.mkdir()
    (d / B_BAD).write_text(json.dumps(MALFORMED[label]), encoding="utf-8")
    s = host.stability_summary(d, now=T_1006)
    assert s["present"] is True and s["error"], label


@pytest.mark.parametrize("label", sorted(MALFORMED))
def test_status_survives_a_malformed_bundle(env, label):
    d = env.home / ".openclaw" / "logs" / "stability"
    d.mkdir(parents=True, exist_ok=True)
    (d / B_BAD).write_text(json.dumps(MALFORMED[label]), encoding="utf-8")
    text = host.render_status(host.collect_status(Canned(), home=env.home, host_env_path=env.hostenv,
                                                  now=T_1006, run_doctor=False))
    assert "could not be read" in next(ln for ln in text.splitlines() if ln.startswith("stability"))


def _status_with(env, *names):
    _bundles(env.home / ".openclaw" / "logs" / "stability", *names)
    return host.render_status(host.collect_status(Canned(), home=env.home, host_env_path=env.hostenv,
                                                  now=T_1006 + 3 * 3600, run_doctor=False))


def test_status_names_counts_tools_and_the_lower_bound(env):
    line = next(ln for ln in _status_with(env, B_1006).splitlines() if ln.startswith("stability"))
    for needle in ("gateway.stop_shutdown_timeout", ">=8 stalled", "blocked_tool_call 6", "Bash",
                   "mcp__openclaw__ask_user", "33 min", "9154", "lower bounds", "T39"):
        assert needle in line, needle


def test_status_without_stalls_or_bundles(env):
    assert "no stalled sessions" in _status_with(env, B_0918)
    other = host.render_status(host.collect_status(Canned(), home=env.home / "elsewhere", host_env_path=env.hostenv,
                                                   now=T_1006, run_doctor=False))
    assert "stability   no bundles" in other


def test_status_stability_reads_never_write(env):
    _bundles(env.home / ".openclaw" / "logs" / "stability", B_1006)
    def snap():
        return {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in env.home.rglob("*") if p.is_file()}
    before_paths = sorted(env.home.rglob("*"))
    before = snap()
    host.collect_status(Canned(), home=env.home, host_env_path=env.hostenv, now=T_1006, run_doctor=False)
    assert sorted(env.home.rglob("*")) == before_paths
    assert snap() == before, "no file may be rewritten, not even in place"

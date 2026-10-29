"""Agent SQLite fault detection, the off-box credential self-heal, and MemoryHigh applied by setup (2026-10-28)."""
from __future__ import annotations

import json

import pytest

from ai_resources import openclaw_host as host
from ai_resources import verify

LINE = ('Oct 28 12:36:28 bithome node[1]: [cron/maintenance] Cron maintenance failed error="Agent snoutzone '
        '(/home/u/.openclaw/agents/snoutzone/agent/openclaw-agent.sqlite): SQLite session reclamation commit checkpoint '
        'expired\\nSessions remain unavailable. Stop the gateway and run openclaw doctor --fix"')


def test_a_checkpoint_expired_line_names_the_stuck_agents():
    out = host.agent_db_fault(LINE + "\n" + LINE.replace("snoutzone", "kit-lane-agy"), None)
    assert out["fault"] and out["agents"] == ["kit-lane-agy", "snoutzone"]
    assert "snoutzone" in host.agent_db_fault_message(out) and "drained window" in host.agent_db_fault_message(out)


def test_ingress_pressure_in_health_is_a_fault_and_old_dead_letters_alone_are_not():
    health = {"deliveryQueues": {"failed": [{"queueName": "session", "count": 14}]}, "eventLoop": {"ingressPressure": "high"}}
    out = host.agent_db_fault("", health)
    assert out["fault"] and out["ingress_pressure"] and out["dead_letters"] == 14
    quiet = host.agent_db_fault("", {"deliveryQueues": {"failed": [{"count": 5}]}, "eventLoop": {"ingressPressure": 0}})
    assert not quiet["fault"] and quiet["dead_letters"] == 5


def _probe(journal, health):
    def runner(argv, **_kw):
        if argv[0] == "journalctl":
            return 0, journal
        return 0, json.dumps(health)
    return runner


def test_the_cli_exit_codes_and_one_alert_per_fault(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(host, "read_host_env", lambda path=None: {"OPENCLAW_NOTIFY_TARGET": "user:U0567PW9D6W"})
    sent = []

    def runner(argv, **kw):
        if argv[0] == "journalctl":
            return 0, LINE
        if "message" in argv:
            sent.append(argv)
            return 0, ""
        return 0, "{}"
    monkeypatch.setattr(host, "resolve_openclaw_bin", lambda: "openclaw")
    args = type("A", (), {"json": True, "notify": True})()
    assert host.cmd_db_fault(args, runner) == 1 and host.cmd_db_fault(args, runner) == 1
    assert len(sent) == 1 and sent[0][sent[0].index("--channel") + 1] == "slack"
    healthy = lambda argv, **kw: (0, "" if argv[0] == "journalctl" else "{}")        # noqa: E731
    assert host.cmd_db_fault(args, healthy) == 0
    assert host.cmd_db_fault(args, runner) == 1 and len(sent) == 2                  # a new episode alerts again


# --- the off-box credential ------------------------------------------------------------------------------------

WIF = {"type": "external_account", "audience": "x", "credential_source": {"executable": {"command": "/bin/true"}}}


@pytest.fixture
def wif(tmp_path, monkeypatch):
    src = tmp_path / "src.json"
    src.write_text(json.dumps(WIF))
    monkeypatch.setattr(host, "_WIF_DEFAULT_SOURCE", src)
    monkeypatch.setattr(host, "OFFBOX_WIF_CRED_PATH", tmp_path / "cfg" / "offbox-wif-cred.json")
    monkeypatch.setattr(host, "HOST_ENV_PATH", tmp_path / "kit-host.env")
    monkeypatch.delenv("CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    return host.OFFBOX_WIF_CRED_PATH


def test_a_deleted_derived_credential_is_regenerated(wif):
    env = {"OPENCLAW_OFFBOX_BUCKET": "gs://b", "OPENCLAW_OFFBOX_CRED_FILE": str(wif)}
    assert host.offbox_cred_repair(env)[0] == "repaired" and wif.is_file()
    assert oct(wif.stat().st_mode & 0o777) == "0o600"
    assert host.offbox_cred_repair(env)[0] == "ok"
    wif.unlink()
    assert host.offbox_cred_repair(env)[0] == "repaired" and wif.is_file()


def test_an_operator_named_credential_is_never_recreated(wif, tmp_path):
    env = {"OPENCLAW_OFFBOX_BUCKET": "gs://b", "OPENCLAW_OFFBOX_CRED_FILE": str(tmp_path / "mine.json")}
    status, detail = host.offbox_cred_repair(env)
    assert status == "failed" and "operator's own" in detail and not (tmp_path / "mine.json").exists()


def test_no_bucket_means_nothing_to_repair(wif):
    assert host.offbox_cred_repair({})[0] == "skipped"


def test_verify_repairs_and_reports_it(wif, monkeypatch):
    monkeypatch.setattr(host, "read_host_env", lambda path=None: {"OPENCLAW_OFFBOX_BUCKET": "gs://b",
                                                                  "OPENCLAW_OFFBOX_CRED_FILE": str(wif)})
    found = verify.repair_findings()
    assert [f.level for f in found] == ["ok"] and "repaired" in found[0].message and wif.is_file()


def test_the_suite_never_points_at_the_real_credential_path():
    # conftest redirects it; the real file is ~/.config/ai-resources/offbox-wif-cred.json
    from pathlib import Path
    assert host.OFFBOX_WIF_CRED_PATH != Path.home() / ".config" / "ai-resources" / "offbox-wif-cred.json"
    assert host.HOST_ENV_PATH != Path.home() / ".openclaw" / "kit-host.env"


# --- MemoryHigh ------------------------------------------------------------------------------------------------

class _Ctx:
    memory_high = "16G"
    dry_run = False

    def __init__(self, current):
        self.current, self.commands = current, []

    def run(self, argv, **_kw):
        return 0, self.current

    def mutate(self, step, argv, why, **_kw):
        self.commands.append(argv)
        return 0, ""


def test_memory_high_is_raised_but_never_lowered():
    low = _Ctx("12884901888")
    assert host._step_memory_high(low).status == "changed" and low.commands
    higher = _Ctx(str(20 * 1024 ** 3))
    assert host._step_memory_high(higher).status == "satisfied" and not higher.commands
    unset = _Ctx("infinity")
    assert host._step_memory_high(unset).status == "changed"


# --- db-fault ignores errors older than the last unit start (2026-10-29) ---------------------------------------------

def _since_probe(journal_by_since, started):
    seen = {}

    def runner(argv, **_kw):
        if argv[0] == "journalctl":
            since = next(a for a in argv if a.startswith("--since="))[len("--since="):]
            seen["since"] = since
            return 0, journal_by_since(since)
        if argv[0] == "systemctl":
            return (0, f"@{int(started)}\n") if started is not None else (1, "")
        return 0, "{}"
    return runner, seen


def test_db_fault_since_is_the_unit_start_when_it_is_newer_than_the_cap(monkeypatch):
    import time
    monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
    # started 10 min ago: only lines after it count; a journal honouring --since returns nothing for the old error
    runner, seen = _since_probe(lambda since: "" if since.startswith("1970-01-12 13:") else LINE, 1_000_000 - 600)
    out = host.agent_db_fault_probe(runner)
    assert seen["since"] == "1970-01-12 13:36:40 UTC" and not out["fault"] and out["agents"] == []


def test_db_fault_error_newer_than_the_unit_start_is_still_reported(monkeypatch):
    import time
    monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
    runner, seen = _since_probe(lambda since: LINE, 1_000_000 - 600)
    out = host.agent_db_fault_probe(runner)
    assert out["fault"] and out["agents"] == ["snoutzone"]


def test_db_fault_without_a_start_timestamp_keeps_the_45_minute_window():
    runner, seen = _since_probe(lambda since: LINE, None)
    out = host.agent_db_fault_probe(runner)
    assert seen["since"] == "-45min" and out["agents"] == ["snoutzone"]


def test_db_fault_start_older_than_the_cap_keeps_the_cap(monkeypatch):
    import time
    monkeypatch.setattr(time, "time", lambda: 1_000_000.0)
    runner, seen = _since_probe(lambda since: LINE, 1_000_000 - 7200)
    host.agent_db_fault_probe(runner)
    assert seen["since"] == "-45min"


# --- db-fault probe_ok: an empty journal match is an answer (2026-10-29) ---------------------------------------------

def _probe_runner(journal_rc, journal_text, health_rc=1, health_text=""):
    def runner(argv, **_kw):
        if argv[0] == "journalctl":
            return journal_rc, journal_text
        if argv[0] == "systemctl":
            return 1, ""
        return health_rc, health_text
    return runner


def test_db_fault_journal_exit_1_empty_is_an_answered_probe():
    out = host.agent_db_fault_probe(_probe_runner(1, ""))
    assert out["probe_ok"] is True and not out["fault"] and out["agents"] == []


def test_db_fault_journal_exit_0_with_a_match_reports_the_fault():
    out = host.agent_db_fault_probe(_probe_runner(0, LINE))
    assert out["probe_ok"] is True and out["fault"] and out["agents"] == ["snoutzone"]


def test_db_fault_journal_exit_2_with_no_health_could_not_tell():
    out = host.agent_db_fault_probe(_probe_runner(2, "journalctl: boom"))
    assert out["probe_ok"] is False and not out["fault"]


def test_db_fault_journal_missing_with_no_health_could_not_tell():
    out = host.agent_db_fault_probe(_probe_runner(127, "journalctl: not found"))
    assert out["probe_ok"] is False


def test_db_fault_journal_exit_1_with_output_is_an_error_not_an_answer():
    out = host.agent_db_fault_probe(_probe_runner(1, "Failed to connect to bus"))
    assert out["probe_ok"] is False


def test_db_fault_health_json_followed_by_stderr_text_still_parses():
    health = '{"ok": true, "deliveryQueues": {"failed": [{"count": 2}]}}\n[warn] something on stderr {not json}\n'
    out = host.agent_db_fault_probe(_probe_runner(2, "", 0, health))
    assert out["probe_ok"] is True and out["dead_letters"] == 2


def test_db_fault_health_json_after_a_leading_warning_still_parses():
    out = host.agent_db_fault_probe(_probe_runner(2, "", 0, 'warn {x\n{"ok": true}\n'))
    assert out["probe_ok"] is True

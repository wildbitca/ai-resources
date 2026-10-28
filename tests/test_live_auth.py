"""ADR-0016 S4 (AC-6, AC-8): live sign-in readers. Method only; email, org ids and key values are never kept."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ai_resources.setup import live_auth as la

FIX = Path(__file__).parent / "fixtures"
REAL_DEFAULT_RUNNER = la.default_runner            # captured at import, before conftest replaces it for every test


def runner_for(text, rc=0):
    calls = []

    def run(argv):
        calls.append(argv)
        return rc, text

    run.calls = calls
    return run


def sub_text():
    return (FIX / "claude_auth" / "status_subscription.json").read_text(encoding="utf-8")


def test_env_api_key_wins_and_the_runner_is_not_asked():
    r = runner_for(sub_text())
    got = la.read_claude(r, env={"ANTHROPIC_API_KEY": "sk-ant-fake"}, settings={})
    assert (got.kind, got.billed_to) == (la.API_KEY, la.B_API) and r.calls == []
    assert "sk-ant-fake" not in repr(got)


def test_api_key_helper_in_settings_is_second():
    got = la.read_claude(runner_for(sub_text()), env={}, settings={"apiKeyHelper": "/home/u/key.sh"})
    assert got.kind == la.API_KEY_HELPER and got.billed_to == la.B_API
    assert "/home/u/key.sh" not in repr(got)


@pytest.mark.parametrize("name,kind", [("CLAUDE_CODE_USE_VERTEX", la.VERTEX), ("CLAUDE_CODE_USE_BEDROCK", la.BEDROCK)])
def test_vertex_and_bedrock_env(name, kind):
    assert la.read_claude(runner_for(""), env={name: "1"}, settings={}).kind == kind


def test_subscription_from_auth_status_and_pii_is_dropped():
    got = la.read_claude(runner_for(sub_text()), env={}, settings={})
    assert (got.kind, got.billed_to) == (la.SUBSCRIPTION, la.B_SUBSCRIPTION)
    blob = repr(got) + got.line() + got.label
    for leak in ("operator@example.invalid", "00000000-0000", "Example Org"):
        assert leak not in blob


def test_method_fields_keeps_method_only():
    kept = la.method_fields(json.loads(sub_text()))
    assert set(kept) == {"loggedIn", "authMethod", "apiProvider", "subscriptionType"}


def test_logged_out_is_none():
    out = (FIX / "claude_auth" / "status_logged_out.json").read_text(encoding="utf-8")
    assert la.read_claude(runner_for(out), env={}, settings={}).kind == la.NONE


def test_unverified_shape_is_unknown_never_api_key():
    out = (FIX / "claude_auth" / "status_unverified_shape.json").read_text(encoding="utf-8")
    got = la.read_claude(runner_for(out), env={}, settings={})
    assert got.kind == la.UNKNOWN and got.billed_to == la.B_UNKNOWN


def test_a_failing_or_garbled_status_is_unknown_and_a_missing_binary_is_none():
    assert la.read_claude(runner_for("not json"), env={}, settings={}).kind == la.UNKNOWN
    assert la.read_claude(runner_for("boom", rc=1), env={}, settings={}).kind == la.UNKNOWN
    assert la.read_claude(runner_for("", rc=127), env={}, settings={}).kind == la.NONE


def test_billing_auth_mode_subscription_is_read():
    out = json.dumps({"loggedIn": True, "billing": {"auth_mode": "subscription"}})
    assert la.read_claude(runner_for(out), env={}, settings={}).kind == la.SUBSCRIPTION


def test_openclaw_reads_status_and_per_agent_profiles_and_ignores_key_material():
    status = (FIX / "openclaw_live" / "models_status_anthropic.json").read_text(encoding="utf-8")
    listing = (FIX / "openclaw_live" / "auth_list_main.json").read_text(encoding="utf-8")

    def run(argv):
        return (0, status) if "status" in argv else (0, listing)

    live = la.read_openclaw(run, ["main", "ai"])
    assert set(live) == {"main", "ai"}
    assert "anthropic" in live["main"] and la.usable_providers(live) >= {"anthropic"}
    assert "FAKEKEY" not in repr(live)


def test_openclaw_with_no_answer_has_no_usable_provider():
    live = la.read_openclaw(runner_for("", rc=127), ["main"])
    assert live == {"main": {}} and la.usable_providers(live) == set()


def test_google_vertex_is_adc_never_an_api_key():
    status = json.dumps({"auth": {"providers": [{"provider": "google-vertex", "effective": {"kind": "env"},
                                                 "profiles": {"count": 0}}]}})
    live = la.read_openclaw(runner_for(status), ["main"])
    assert live["main"]["google-vertex"].kind == la.ADC


def test_a_status_profile_of_unknown_type_is_unknown_billing_never_an_api_key():
    status = json.dumps({"auth": {"providers": [{"provider": "anthropic", "effective": {"kind": "profiles"},
                                                 "profiles": {"count": 1}}]}})
    live = la.read_openclaw(lambda argv: (0, status) if "status" in argv else (1, "no list"), ["main"])
    got = live["main"]["anthropic"]
    assert got.kind == la.UNKNOWN and got.billed_to == la.B_UNKNOWN
    assert la.usable_providers(live) == {"anthropic"}              # still usable: only the billing is unknown


def test_agy_presence_only(tmp_path):
    assert la.read_agy(home=tmp_path).kind == la.NONE
    (tmp_path / "google_accounts.json").write_text('{"active": "someone@example.invalid"}', encoding="utf-8")
    got = la.read_agy(home=tmp_path)
    assert got.kind == la.OAUTH_GOOGLE and "someone" not in repr(got)


def test_read_claude_settings_tolerates_missing_and_bad_files(tmp_path):
    assert la.read_claude_settings(tmp_path / "nope.json") == {}
    (tmp_path / "s.json").write_text("{bad", encoding="utf-8")
    assert la.read_claude_settings(tmp_path / "s.json") == {}


# --- the real default runner (conftest replaces it in every test; this one is called with shutil.which and subprocess.run
# --- stubbed here, so no binary is ever looked up or started) --------------------------------------------------------------

def _stub_process(monkeypatch, *, which="/stub/bin/tool", result=None, raises=None):
    ran = []
    monkeypatch.setattr(la.shutil, "which", lambda name: which)

    def fake_run(argv, **kw):
        ran.append((argv, kw))
        if raises is not None:
            raise raises
        return result
    monkeypatch.setattr(la.subprocess, "run", fake_run)
    return ran


class _Done:
    def __init__(self, rc, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def test_the_real_runner_answers_127_when_the_binary_is_not_on_path(monkeypatch):
    ran = _stub_process(monkeypatch, which=None)
    assert REAL_DEFAULT_RUNNER(["claude", "auth", "status"]) == (127, "claude not found on PATH")
    assert ran == []                                       # nothing was started


def test_the_real_runner_runs_the_resolved_binary_with_a_timeout_in_the_home_directory(monkeypatch):
    ran = _stub_process(monkeypatch, result=_Done(0, '{"ok": true}', "noise"))
    assert REAL_DEFAULT_RUNNER(["openclaw", "models", "status", "--json"]) == (0, '{"ok": true}')
    argv, kw = ran[0]
    assert argv == ["/stub/bin/tool", "models", "status", "--json"]
    assert kw["timeout"] == la.STATUS_TIMEOUT_S and kw["capture_output"] is True and kw["text"] is True
    assert kw["cwd"] == str(Path.home())


def test_the_real_runner_appends_stderr_only_when_the_command_failed(monkeypatch):
    _stub_process(monkeypatch, result=_Done(0, "out", "warning text"))
    assert REAL_DEFAULT_RUNNER(["x"]) == (0, "out")
    _stub_process(monkeypatch, result=_Done(3, "out", "boom"))
    assert REAL_DEFAULT_RUNNER(["x"]) == (3, "outboom")
    _stub_process(monkeypatch, result=_Done(2, None, None))
    assert REAL_DEFAULT_RUNNER(["x"]) == (2, "")


def test_the_real_runner_turns_a_timeout_or_an_os_error_into_a_failed_status(monkeypatch):
    import subprocess
    _stub_process(monkeypatch, raises=subprocess.TimeoutExpired(["x"], 20))
    rc, text = REAL_DEFAULT_RUNNER(["x"])
    assert rc == 1 and "timed out" in text
    _stub_process(monkeypatch, raises=OSError("exec format error"))
    assert REAL_DEFAULT_RUNNER(["x"]) == (1, "exec format error")


# --- robust OpenClaw reader (3.2.8 item 3) ---------------------------------------------------------------------------

LIVE = FIX / "openclaw_live"
LIVE_STATUS = (LIVE / "models_status_live_2026_10_7.json").read_text(encoding="utf-8")
LIVE_LIST = (LIVE / "auth_list_live_main.json").read_text(encoding="utf-8")
MAINTENANCE = (LIVE / "maintenance_rejection.txt").read_text(encoding="utf-8")
BANNER = (LIVE / "banner_stderr.txt").read_text(encoding="utf-8")
CONFIG_PROFILES = {"anthropic:default": {"provider": "anthropic", "mode": "api_key"},
                   "google:default": {"provider": "google", "mode": "api_key"}}


def _scripted(status, listing):
    """A runner answering `models status` with `status` and `models auth list` with `listing`, each a (rc, text) or a
    list of them consumed one per call (the last repeats)."""
    seqs = {"status": list(status) if isinstance(status, list) else [status],
            "list": list(listing) if isinstance(listing, list) else [listing]}
    calls = []

    def run(argv):
        key = "status" if "status" in argv else "list"
        calls.append(key)
        seq = seqs[key]
        return seq.pop(0) if len(seq) > 1 else seq[0]
    run.calls = calls
    return run


def _no_sleep(slept=None):
    return (lambda s: slept.append(s)) if slept is not None else (lambda s: None)


def test_the_real_shaped_output_reads_anthropic_google_and_the_agy_lane_for_every_agent():
    run = _scripted((0, LIVE_STATUS), (0, LIVE_LIST))
    agents = ["main", "ai", "claude", "devops", "elinvo", "security", "snoutzone"]
    live = la.read_openclaw(run, agents, sleep=_no_sleep())
    assert live.readable and not live.reason
    for a in agents:
        assert {"anthropic", "google", "agy-cli-lane"} <= set(live[a])


def test_banner_text_around_the_json_does_not_hide_the_providers():
    run = _scripted((0, BANNER + LIVE_STATUS + "\n" + BANNER), (0, BANNER + LIVE_LIST))
    live = la.read_openclaw(run, ["main"], sleep=_no_sleep())
    assert live.readable and {"anthropic", "google"} <= set(live["main"])


def test_extract_json_prefers_the_object_with_the_expected_keys():
    text = 'warn {"level": "info"} then ' + LIVE_LIST
    assert la.extract_json(text, ("profiles",))["agentId"] == "main"
    assert la.extract_json("no json here", ("profiles",)) is None
    assert la.extract_json('{"a": 1}', ("profiles",)) == {"a": 1}


def test_a_maintenance_rejection_is_unreadable_not_empty_and_is_retried_with_backoff():
    slept: list = []
    run = _scripted((1, MAINTENANCE), (1, MAINTENANCE))
    live = la.read_openclaw(run, ["main"], sleep=_no_sleep(slept))
    assert live.readable is False and "maintenance" in live.reason
    assert slept[:3] == [1, 3, 8]
    assert la.usable_providers(live) == set()


def test_a_maintenance_window_that_ends_is_waited_out():
    slept: list = []
    run = _scripted([(1, MAINTENANCE), (0, LIVE_STATUS)], [(1, MAINTENANCE), (0, LIVE_LIST)])
    live = la.read_openclaw(run, ["main"], sleep=_no_sleep(slept))
    assert live.readable and "anthropic" in live["main"] and slept


def test_exit_zero_with_unparseable_output_is_unreadable_not_empty():
    live = la.read_openclaw(_scripted((0, BANNER), (0, BANNER)), ["main"], sleep=_no_sleep())
    assert live.readable is False and live.reason


def test_a_timeout_is_unreadable():
    live = la.read_openclaw(_scripted((1, "timed out after 60 seconds"), (1, "timed out after 60 seconds")), ["main"],
                            sleep=_no_sleep())
    assert live.readable is False


def test_empty_providers_with_a_parsed_answer_is_readable_and_empty():
    empty = (FIX / "openclaw_models_status" / "empty.json").read_text(encoding="utf-8")
    live = la.read_openclaw(_scripted((0, empty), (0, json.dumps({"agentId": "main", "profiles": []}))), ["main"],
                            sleep=_no_sleep())
    assert live.readable is True and la.usable_providers(live) == set()


def test_config_auth_profiles_corroborate_an_unreadable_cli():
    run = _scripted((1, MAINTENANCE), (1, MAINTENANCE))
    live = la.read_openclaw(run, ["main", "ai"], sleep=_no_sleep(), config_profiles=CONFIG_PROFILES)
    assert live.readable is False
    assert set(live["main"]) == {"anthropic", "google"} and set(live["ai"]) == {"anthropic", "google"}
    assert live.config_providers == {"anthropic", "google"}


def test_config_auth_profiles_fill_a_provider_the_cli_reports_nothing_for():
    empty = (FIX / "openclaw_models_status" / "empty.json").read_text(encoding="utf-8")
    run = _scripted((0, empty), (0, json.dumps({"agentId": "main", "profiles": []})))
    live = la.read_openclaw(run, ["main"], sleep=_no_sleep(), config_profiles=CONFIG_PROFILES)
    assert live.readable and set(live["main"]) == {"anthropic", "google"}


def test_config_profiles_of_an_unknown_mode_do_not_corroborate():
    assert la.config_providers({"x:default": {"provider": "x", "mode": "bogus"}, "y": "nope"}) == set()
    assert la.config_providers(CONFIG_PROFILES) == {"anthropic", "google"}

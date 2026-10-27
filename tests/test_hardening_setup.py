"""R5/R7 through setup: the plugin VERSION rule, the kit hooks (StopFailure quota signal, the narration hook), the
dry-run preview, the sleep guard's wait-loop rules, the live checks, and the simulated end state: after the owner's
`setup` the verify --live findings hold no error."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import openclaw_host as host  # noqa: E402
from ai_resources import openclaw_reconcile as rec  # noqa: E402
from ai_resources import verify  # noqa: E402
from ai_resources.setup import state  # noqa: E402
from ai_resources.setup.cockpits import claude, openclaw  # noqa: E402

AK = str(REPO)


# --- plugin version rule ------------------------------------------------------------------------------------------

def _runtime(monkeypatch, **plugin):
    monkeypatch.setattr(openclaw, "plugin_runtime", lambda: plugin)


def test_a_gateway_on_another_plugin_version_is_stale_by_version(monkeypatch):
    plugin_dir = str(REPO / "openclaw-plugin" / "ai-resources")
    disk = openclaw.plugin_version(AK)
    _runtime(monkeypatch, rootDir=plugin_dir, packageVersion="0.9.0", status="loaded")
    assert disk and openclaw.stale_reason(plugin_dir) == "version"     # no start time, no runner needed


def test_the_same_version_on_the_same_path_is_not_stale_by_version(monkeypatch):
    plugin_dir = str(REPO / "openclaw-plugin" / "ai-resources")
    _runtime(monkeypatch, rootDir=plugin_dir, packageVersion=openclaw.plugin_version(AK), status="loaded")
    monkeypatch.setattr(openclaw.openclaw_host, "gateway_started_at", lambda *a, **k: None)
    assert openclaw.stale_reason(plugin_dir) is None


def test_a_runtime_without_a_version_falls_through_to_the_other_rules(monkeypatch):
    plugin_dir = str(REPO / "openclaw-plugin" / "ai-resources")
    _runtime(monkeypatch, rootDir=plugin_dir)
    monkeypatch.setattr(openclaw.openclaw_host, "gateway_started_at", lambda *a, **k: None)
    assert openclaw.stale_reason(plugin_dir) is None


# --- hooks installed by setup, shown by the dry run ----------------------------------------------------------------

def _scripts(settings: dict, event: str) -> list[str]:
    return sorted(pathlib.Path(h["command"].split('"')[1]).name for e in settings.get("hooks", {}).get(event, [])
                  for h in e["hooks"])


def test_setup_registers_the_quota_signal_on_stopfailure_and_a_second_run_changes_nothing(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "python3 /home/me/own-guard.py"}]}]}}))
    assert rec.hooks_missing(AK, team=False, guard=False, settings_path=settings) \
        and "StopFailure:kit_quota_signal.py" in rec.hooks_missing(AK, team=False, guard=False, settings_path=settings)
    from ai_resources.setup.cockpits import _shared
    assert _shared.merge_kit_hooks(settings, claude._kit_hooks(AK), claude._is_kit_hook_command)
    data = json.loads(settings.read_text())
    assert _scripts(data, "StopFailure") == ["kit_quota_signal.py"]
    assert data["hooks"]["StopFailure"][0]["matcher"] == "rate_limit|billing_error"
    assert "own-guard.py" in json.dumps(data), "the user's own hook was dropped"
    assert rec.hooks_missing(AK, team=False, guard=False, settings_path=settings) == []
    before = settings.read_text()
    assert not _shared.merge_kit_hooks(settings, claude._kit_hooks(AK), claude._is_kit_hook_command)
    assert settings.read_text() == before


def test_the_narration_hook_is_wanted_only_when_host_narration_is_on(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    off = rec.hooks_missing(AK, team=False, guard=True, settings_path=settings)
    on = rec.hooks_missing(AK, team=True, guard=True, settings_path=settings)
    assert not any("openclaw_team_progress" in m for m in off)
    assert any("openclaw_gateway_guard" in m for m in off)
    assert {"PreToolUse:openclaw_team_progress.py", "Stop:openclaw_team_progress.py",
            "UserPromptSubmit:openclaw_team_progress.py", "SubagentStop:openclaw_team_progress.py"} <= set(on)
    from ai_resources.setup.cockpits import _shared
    assert _shared.merge_kit_hooks(settings, claude._kit_hooks(AK), claude._is_kit_hook_command)
    assert claude.install_openclaw_hooks(AK, team=True, guard=True, settings_path=settings)
    assert rec.hooks_missing(AK, team=True, guard=True, settings_path=settings) == []
    assert not claude.install_openclaw_hooks(AK, team=True, guard=True, settings_path=settings)     # idempotent


def test_the_dry_run_preview_names_the_missing_hooks_and_writes_nothing(tmp_path, monkeypatch):
    settings = claude.SETTINGS_PATH
    settings.write_text("{}")
    s = state.SetupState()
    s.cockpits["claude"] = state.CockpitState(installed=True)
    monkeypatch.setattr(rec, "reconcile", lambda *a, **k: rec.Outcome(0, [], False, False, ["restart owed: x"]))
    lines = openclaw.hardening_dry_run_lines(s, AK)
    assert any("StopFailure" in line and "kit_quota_signal.py" in line for line in lines), lines
    assert any("restart owed" in line for line in lines)
    assert settings.read_text() == "{}"


# --- the sleep guard (R7-5) -----------------------------------------------------------------------------------------

HOOK = REPO / "hooks" / "kit_sleep_guard.py"


def _guard(command: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"tool_name": "Bash",
                          "tool_input": {"command": command}}), capture_output=True, text=True, timeout=20)


@pytest.mark.parametrize("command", [
    "until curl -s localhost:8080; do sleep 2; done",
    "while ! test -f /tmp/ready; do sleep 5; done",
    "sleep 20; sleep 20",
    "sleep 15 && build && sleep 20",
])
def test_wait_loops_and_summed_sleeps_are_refused(command):
    r = _guard(command)
    assert r.returncode == 2 and ("run_in_background" in r.stderr or "timeout" in r.stderr), (command, r.stderr)


@pytest.mark.parametrize("command", [
    "for i in 1 2 3; do cmd && break; sleep 2; done",
    "sleep 10; echo ok", "timeout 600 make test", "echo 'while true; do sleep 1; done'",
    "grep -r 'until x; do sleep 1; done' .",
])
def test_bounded_or_unrelated_commands_still_pass(command):
    r = _guard(command)
    assert r.returncode == 0, (command, r.stderr)


def test_the_polling_guidance_names_one_blocking_wait():
    from ai_resources.setup.cockpits import _shared
    text = _shared.OPENCLAW_POLLING_MD
    assert "ONE blocking call" in text and "awaitResults" in text and "kit_sleep_guard" in text
    assert "ONE blocking call" in (REPO / "AGENTS.md").read_text()


# --- the live checks and the simulated end state ---------------------------------------------------------------------

def _setup_state(narration: str = "") -> state.SetupState:
    s = state.SetupState()
    s.cockpits["openclaw"] = state.CockpitState(installed=True, configured=True)
    s.openclaw.host, s.openclaw.host_guard, s.openclaw.host_narration = True, True, narration
    s.openclaw.host_hooks_applied = True
    return s


@pytest.fixture
def live(monkeypatch, tmp_path):
    cred = tmp_path / "offbox-wif-cred.json"
    env = {"OPENCLAW_OFFBOX_BUCKET": "gs://b", "OPENCLAW_OFFBOX_CRED_FILE": str(cred)}
    monkeypatch.setattr(host, "read_host_env", lambda path=None: dict(env))
    box = {"health": 0, "plugin": {"status": "loaded", "packageVersion": openclaw.plugin_version(AK),
                                   "rootDir": str(REPO / "openclaw-plugin" / "ai-resources")},
           "stale": None, "counts": {"engram": 2}, "cred": cred}
    monkeypatch.setattr(openclaw, "_openclaw", lambda args, stdin=None, timeout=120: (box["health"], ""))
    monkeypatch.setattr(openclaw, "plugin_runtime", lambda: box["plugin"])
    monkeypatch.setattr(openclaw, "stale_reason", lambda *a, **k: box["stale"])
    monkeypatch.setattr(rec, "gateway_mcp_children", lambda *a, **k: box["counts"])
    return box


def _levels(findings):
    return {f.level for f in findings}


def test_before_setup_the_live_checks_name_every_defect(live):
    live.update(health=0, stale="path", counts={"engram": 57, "supabase": 159})
    out = openclaw.live_findings(_setup_state(), AK)
    msgs = " | ".join(f.message for f in out if f.level != "ok")
    assert "StopFailure:kit_quota_signal.py" in msgs                 # hooks missing
    assert "off-box credential file" in msgs and "missing" in msgs   # R7-1
    assert "(path rule)" in msgs                                     # R7-3
    assert "216 MCP server children" in msgs                         # R7-4


def test_after_setup_and_the_drained_restart_the_live_checks_hold_no_error(live, tmp_path):
    """Simulated owner run: hooks registered, credential written 0600, one drained restart cleared the stale plugin
    and the overflow; a second pass of each step changes nothing."""
    s = _setup_state()
    from ai_resources.setup.cockpits import _shared
    assert _shared.merge_kit_hooks(claude.SETTINGS_PATH, claude._kit_hooks(AK), claude._is_kit_hook_command)
    assert claude.install_openclaw_hooks(AK, team=False, guard=True)
    live["cred"].write_text("{}")
    live["cred"].chmod(0o600)
    live.update(stale=None, counts={"engram": 2, "supabase": 1})
    out = openclaw.live_findings(s, AK)
    assert "error" not in _levels(out), [f.message for f in out if f.level == "error"]
    assert "warn" not in _levels(out), [f.message for f in out if f.level == "warn"]
    # a second whole run (kit hooks, then the host hooks) leaves the file byte for byte as the first one did
    first = claude.SETTINGS_PATH.read_text()
    _shared.merge_kit_hooks(claude.SETTINGS_PATH, claude._kit_hooks(AK), claude._is_kit_hook_command)
    claude.install_openclaw_hooks(AK, team=False, guard=True)
    assert claude.SETTINGS_PATH.read_text() == first


def test_a_credential_with_loose_permissions_is_an_error(live):
    live["cred"].write_text("{}")
    live["cred"].chmod(0o644)
    msgs = [f.message for f in openclaw.live_findings(_setup_state(), AK) if f.level == "error"]
    assert any("mode 644" in m for m in msgs)


def test_an_unhealthy_gateway_and_an_unloaded_plugin_are_errors(live):
    live.update(health=1)
    live["plugin"] = {"status": "failed"}
    msgs = [f.message for f in openclaw.live_findings(_setup_state(), AK) if f.level == "error"]
    assert any("does not answer" in m for m in msgs) and any("failed" in m for m in msgs)


def test_verify_live_flag_adds_the_live_findings_and_is_off_by_default():
    import argparse
    p = argparse.ArgumentParser()
    sub = p.add_subparsers()
    verify.add_subparser(sub)
    assert p.parse_args(["verify"]).live is False and p.parse_args(["verify", "--live"]).live is True


def test_verify_live_without_openclaw_is_a_quiet_ok():
    out = verify.live_findings(state.SetupState())
    assert [f.level for f in out] == ["ok"]

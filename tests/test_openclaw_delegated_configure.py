"""ADR-0016 D1 (AC-9, AC-11): OpenClaw's sign-in is delegated to `openclaw configure --section model`.

The terminal is handed over only on an explicit "change" and an explicit confirm, never in a non-interactive run, a dry
run or a test without a stub, and no gateway restart or call is ever made."""
from __future__ import annotations

import pathlib
import subprocess

import pytest

from ai_resources.setup import cockpit_auth, live_auth, state, ui
from ai_resources.setup.cockpits import openclaw as oc
from wizard_fakes import Script, fake_openclaw

REPO = pathlib.Path(__file__).resolve().parents[1]
FIX = pathlib.Path(__file__).parent / "fixtures" / "openclaw_live"
ARGV = ["openclaw", "configure", "--section", "model"]


@pytest.fixture
def rig(monkeypatch, tmp_path):
    cfg = tmp_path / "openclaw.json"
    cfg.write_text((FIX / "all_anthropic.json").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(cfg))
    log = fake_openclaw(monkeypatch)
    handed = []
    monkeypatch.setattr(live_auth, "handoff", lambda argv: handed.append(list(argv)) or 0)
    return type("R", (), {"calls": log, "handed": handed})


def ask(monkeypatch, answers, *, dry_run=False, non_interactive=False):
    script = Script(**answers)
    script.install(monkeypatch, non_interactive=non_interactive)
    s = state.SetupState()
    rc = oc.ask_auth(ui, s, None, dry_run)
    return rc, script, s


def test_keep_hands_nothing_over(rig, monkeypatch):
    rc, _script, _s = ask(monkeypatch, {"OpenClaw sign-in": "keep"})
    assert rc == 0 and rig.handed == []


def test_change_asks_a_default_no_confirm_and_a_no_hands_nothing_over(rig, monkeypatch):
    asked = []
    script = Script(**{"OpenClaw sign-in": "change", "Hand this terminal": False})
    script.install(monkeypatch)
    monkeypatch.setattr(ui, "confirm", lambda m, default=True: asked.append((m, default)) or False)
    assert oc.ask_auth(ui, state.SetupState(), None, False) == 0
    assert rig.handed == [] and len(asked) == 1 and asked[0][1] is False
    assert "openclaw configure --section model" in asked[0][0] and "never restarts the gateway" in asked[0][0]


def test_an_explicit_change_and_yes_hands_the_terminal_to_openclaw_once_and_reads_again(rig, monkeypatch):
    rc, script, s = ask(monkeypatch, {"OpenClaw sign-in": "change", "Hand this terminal": True})
    assert rc == 0 and rig.handed == [ARGV]
    reads = [c for c in rig.calls if c[:3] == ["models", "auth", "list"]]
    assert len(reads) >= 2 * 7                      # one reading before and one after the handover, per non-kit agent
    assert any("Applies now" in m for m in script.shown)
    assert any("changed OpenClaw's model sign-in" in a for a in ui.actions_done())
    assert cockpit_auth.providers(s)


def test_no_native_flow_in_a_dry_run_or_a_non_interactive_run(rig, monkeypatch):
    rc, script, _s = ask(monkeypatch, {"OpenClaw sign-in": "change", "Hand this terminal": True}, dry_run=True)
    assert rc == 0 and rig.handed == [] and any("Dry run" in m for m in script.shown)
    rc, _script, _s = ask(monkeypatch, {}, non_interactive=True)
    assert rc == 0 and rig.handed == []


def test_no_gateway_restart_or_call_is_ever_made(rig, monkeypatch):
    ask(monkeypatch, {"OpenClaw sign-in": "change", "Hand this terminal": True})
    for call in rig.calls:
        assert call[0] not in ("gateway", "restart", "agent", "agents", "channels", "config"), call
    assert not any("restart" in " ".join(c) for c in rig.calls)


def test_a_failing_native_flow_is_reported_by_exit_code_only(rig, monkeypatch):
    monkeypatch.setattr(live_auth, "handoff", lambda argv: 3)
    _rc, script, _s = ask(monkeypatch, {"OpenClaw sign-in": "change", "Hand this terminal": True})
    assert any("exited 3" in m for m in script.shown)


def test_the_default_handoff_is_the_inherited_terminal(monkeypatch):
    """The real handover inherits stdin/stdout/stderr: no capture, no input, no pipe (the credential goes keyboard to
    OpenClaw). `subprocess.run` is replaced, so nothing starts."""
    seen = {}
    monkeypatch.setattr(live_auth.shutil, "which", lambda name: f"/fake/bin/{name}")

    def run(argv, **kw):
        seen["argv"], seen["kw"] = list(argv), kw
        return subprocess.CompletedProcess(argv, 0)
    monkeypatch.setattr(live_auth.subprocess, "run", run)
    assert live_auth.native_handoff(ARGV) == 0
    assert seen["argv"] == ["/fake/bin/openclaw", "configure", "--section", "model"]
    assert not {"capture_output", "input", "stdin", "stdout", "stderr", "env"} & set(seen["kw"])


def test_the_words_paste_api_key_appear_nowhere_under_scripts():
    out = subprocess.run(["grep", "-rn", "--include=*.py", "paste-api-key", str(REPO / "scripts")],
                         capture_output=True, text=True)
    assert out.returncode == 1, out.stdout

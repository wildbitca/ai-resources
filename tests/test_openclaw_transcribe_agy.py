"""The transcriber's `agy` mode: Antigravity CLI on the user's own Google account.

Every assertion here comes from a failure seen against a live agy 1.2.5 and a live
OpenClaw 2026.9.4 gateway on 2026-09-17, not from reading its help text.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from ai_resources.voice import openclaw_transcribe as transcriber  # noqa: E402


@pytest.fixture(autouse=True)
def _quiet_logs_and_no_glossary(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICE_LOG", str(tmp_path / "voice.log"))
    monkeypatch.setattr(transcriber, "glossary", lambda: "OpenClaw, kubectl")
    monkeypatch.setenv("AGY_BIN", "/bin/agy")


def _capture(monkeypatch, stdout: str = "hola", returncode: int = 0, stderr: str = ""):
    calls: list[dict] = []

    def fake_run(cmd, **kw):
        calls.append({"cmd": cmd, "kw": kw})
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    monkeypatch.setattr(transcriber, "run", fake_run)
    return calls


def test_the_prompt_is_the_last_argv_element_right_after_dash_p(monkeypatch):
    """`-p` consumes the NEXT argv element: with `-p --model <id> … <prompt>` agy takes
    "--model" as the prompt and exits 2 ('took "--model" as its prompt')."""
    calls = _capture(monkeypatch)
    assert transcriber.agy("/notes/a.ogg", "es") == "hola"
    cmd = calls[0]["cmd"]
    assert cmd[-2] == "-p"
    assert "/notes/a.ogg" in cmd[-1]
    assert "--model" in cmd[:-2]


def test_the_notes_directory_is_added_and_permissions_are_skipped(monkeypatch):
    """The note is staged outside agy's workspace, so `view_file` needs `--add-dir` for
    its directory AND `--dangerously-skip-permissions`; otherwise headless agy denies the
    read, prints nothing and exits 0 — a silent failure on every note."""
    calls = _capture(monkeypatch)
    transcriber.agy("/openclaw/media/inbound/a.ogg", "es")
    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("--add-dir") + 1] == "/openclaw/media/inbound"
    assert "--dangerously-skip-permissions" in cmd


def test_no_json_schema_is_ever_passed_with_audio(monkeypatch):
    """`--json-schema` plus an audio note hung for 124 s and returned nothing."""
    calls = _capture(monkeypatch)
    transcriber.agy("/notes/a.ogg", "es")
    assert "--json-schema" not in calls[0]["cmd"]


def test_the_default_model_is_flash_low_and_is_overridable(monkeypatch):
    """flash-medium leaks its reasoning into the reply, so it is never the default."""
    calls = _capture(monkeypatch)
    transcriber.agy("/notes/a.ogg", "es")
    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("--model") + 1] == "gemini-3.8-flash-low"
    monkeypatch.setenv("VOICE_AGY_MODEL", "gemini-3.1-pro-low")
    calls.clear()
    transcriber.agy("/notes/a.ogg", "es")
    assert calls[0]["cmd"][calls[0]["cmd"].index("--model") + 1] == "gemini-3.1-pro-low"


def test_a_timeout_is_retried_once_and_kills_the_process_group(monkeypatch):
    attempts = {"n": 0}

    def fake_run(cmd, **kw):
        attempts["n"] += 1
        assert kw.get("start_new_session") is True, kw
        assert kw.get("timeout") == 45.0, kw
        if attempts["n"] == 1:
            raise subprocess.TimeoutExpired(cmd, 45.0)
        return subprocess.CompletedProcess(cmd, 0, "recovered", "")

    monkeypatch.setattr(transcriber, "run", fake_run)
    assert transcriber.agy("/notes/a.ogg", "es") == "recovered"
    assert attempts["n"] == 2


def test_two_failures_return_empty_and_the_mode_reports_the_marker(monkeypatch):
    """A wrong Whisper transcript cannot be repaired by a model that never heard the
    audio, so agy mode has no whisper fallback — but the reply is never empty."""
    _capture(monkeypatch, stdout="", returncode=0)
    assert transcriber.agy("/notes/a.ogg", "es") == ""
    text, via = transcriber.transcribe("/notes/a.ogg", "agy", "es")
    assert text == transcriber.FAILURE_MARKER
    assert via == "agy-failed"


def test_a_successful_run_reports_the_agy_engine(monkeypatch):
    _capture(monkeypatch, stdout="  buenas  ")
    assert transcriber.transcribe("/notes/a.ogg", "agy", "es") == ("buenas", "agy")


def test_a_missing_binary_fails_without_running_anything(monkeypatch):
    monkeypatch.delenv("AGY_BIN", raising=False)
    monkeypatch.setattr(transcriber, "find_binary", lambda *_a, **_k: "")
    calls = _capture(monkeypatch)
    assert transcriber.agy("/notes/a.ogg", "es") == ""
    assert calls == []


def test_agy_mode_never_shells_out_to_whisper_or_ffmpeg(monkeypatch):
    calls = _capture(monkeypatch, stdout="hola")
    monkeypatch.setattr(transcriber, "whisper", lambda *_a, **_k: pytest.fail("whisper ran"))
    monkeypatch.setattr(transcriber, "ffmpeg", lambda *_a, **_k: pytest.fail("ffmpeg ran"))
    assert transcriber.transcribe("/notes/a.ogg", "agy", "es")[1] == "agy"
    assert all("whisper" not in part and "ffmpeg" not in part for part in calls[0]["cmd"])


def test_the_cli_accepts_the_agy_mode(monkeypatch, capsys):
    monkeypatch.setattr(transcriber, "transcribe", lambda *_a, **_k: ("desde agy", "agy"))
    assert transcriber.main(["--mode", "agy", "--language", "es", "/notes/a.ogg"]) == 0
    assert capsys.readouterr().out.strip() == "desde agy"

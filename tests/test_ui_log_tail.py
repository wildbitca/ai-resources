"""run_with_spinner, log_tail and mask_secrets (plan S10)."""
from __future__ import annotations

import subprocess

import pytest

from ai_resources.setup import ui

# Integration: drives a real external tool, process or lock; skipped unless --integration (see tests/conftest.py).
pytestmark = pytest.mark.integration

TOKEN = "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


@pytest.fixture
def out(monkeypatch):
    events: list[str] = []
    for name in ("info", "ok", "warn", "error", "detail"):
        monkeypatch.setattr(ui, name, lambda msg, _n=name: events.append(f"{_n}: {msg}"))

    class Spin:
        def __enter__(self):
            events.append("spinner open")

        def __exit__(self, *a):
            events.append("spinner closed")
    monkeypatch.setattr(ui, "spinner", lambda label: Spin())
    return events


def _fake_run(monkeypatch, rc, lines, err=""):
    def run(cmd, **kw):
        assert kw.get("capture_output") and kw.get("text")
        return subprocess.CompletedProcess(cmd, rc, stdout="\n".join(lines), stderr=err)
    monkeypatch.setattr(subprocess, "run", run)


def test_failure_prints_the_command_the_rc_and_exactly_the_last_20_lines_after_the_spinner(out, monkeypatch):
    _fake_run(monkeypatch, 1, [f"line {i}" for i in range(50)])
    r = ui.run_with_spinner("pipx install x", ["pipx", "install", "x"])
    assert r.returncode == 1
    assert out.index("spinner closed") < min(i for i, e in enumerate(out) if e.startswith("error"))
    errors = [e for e in out if e.startswith("error")]
    assert errors == ["error: pipx install x failed (exit 1): pipx install x"]
    shown = [e for e in out if e.startswith("detail: line ")]
    assert shown == [f"detail: line {i}" for i in range(30, 50)]
    assert "detail: Last output:" in out


def test_a_token_looking_string_is_masked_in_the_tail_and_the_command(out, monkeypatch):
    _fake_run(monkeypatch, 2, ["fetching", f"ANTHROPIC_API_KEY={TOKEN}", f"Authorization: Bearer {TOKEN}"])
    ui.run_with_spinner("install", ["curl", "-H", f"x-api-key: {TOKEN}", "https://x"])
    text = "\n".join(out)
    assert TOKEN not in text and "ABCDEFGHIJ" not in text
    assert "ANTHROPIC_API_KEY=****" in text


def test_success_prints_one_ok_line(out, monkeypatch):
    _fake_run(monkeypatch, 0, ["noise"] * 5)
    r = ui.run_with_spinner("pipx install x", ["pipx", "install", "x"])
    assert r.returncode == 0
    assert [e for e in out if not e.startswith("spinner")] == ["ok: pipx install x"]


def test_a_command_that_cannot_start_is_a_failure_not_a_traceback(out, monkeypatch):
    def run(cmd, **kw):
        raise FileNotFoundError("no such file: pipx")
    monkeypatch.setattr(subprocess, "run", run)
    r = ui.run_with_spinner("pipx install x", ["pipx", "install", "x"])
    assert r.returncode == -1
    assert any("exit -1" in e for e in out) and any("no such file" in e for e in out)


def test_a_timeout_is_a_failure(out, monkeypatch):
    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 5)
    monkeypatch.setattr(subprocess, "run", run)
    assert ui.run_with_spinner("slow", ["sleep", "9"], timeout=5).returncode == -1


@pytest.mark.parametrize("raw,must_not_contain", [
    ("OPENROUTER_API_KEY=sk-or-v1-0123456789abcdef", "0123456789abcdef"),
    ("password: hunter2hunter2", "hunter2"),
    ('"ANTHROPIC_AUTH_TOKEN": "abcdefghijklmnop"', "abcdefghijklmnop"),
    ("token ghp_abcdefghijklmnopqrstuvwxyz0123", "ghp_abcdefghij"),
    ("AIzaSyA-abcdefghijklmnopqrstuvwxyz", "AIzaSyA-abcdefg"),
    ("Authorization: Bearer abcdefghijklmnop", "abcdefghijklmnop"),
    ("blob 0123456789abcdef0123456789abcdef0123", "0123456789abcdef0123456789abcdef"),
    ("PASSWORD=$ecret99", "ecret99"),
    ("password: <hunter2pw>", "hunter2pw"),
    ("run --token hunter2", "hunter2"),
    ("fetch https://user:tok3n@host/path", "tok3n"),
    ("DATABASE_URL=postgres://u:pw1234@h/db", "pw1234"),
    ("secret = 'a b c phrase'", "phrase"),
])
def test_mask_secrets(raw, must_not_contain):
    masked = ui.mask_secrets(raw)
    assert must_not_contain not in masked and "****" in masked


@pytest.mark.parametrize("raw", [
    "api_key: os.environ/ANTHROPIC_API_KEY", "model: claude-sonnet-5-5", "/home/user/.config/ai-resources/.env",
    "Installing pipx 1.7.1", "api_key: ${OPENAI_API_KEY}",
    "password: $DB_PASSWORD", "token: <API_TOKEN>",
])
def test_mask_secrets_keeps_references_and_ordinary_text(raw):
    assert ui.mask_secrets(raw) == raw

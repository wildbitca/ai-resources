"""Read-only detection aggregate: Codex and agy usage, pools by file presence, no calls, no writes (AC-16, AC-23)."""
from __future__ import annotations

import socket
import subprocess
from pathlib import Path

import pytest

from ai_resources import model_accounts as ma
from ai_resources.scoring import detect, headroom_sources
from ai_resources.setup import detection

SECRET = "SECRET-CONTENT-MUST-NEVER-BE-READ"


class Det:
    def __init__(self, installed):
        self.installed = installed
        self.name, self.version, self.binary_path = "x", "1", "/bin/x"


def cockpits(*installed):
    ids = ("claude", "gemini", "cursor", "codex", "aider", "copilot", "windsurf", "continue", "opencode",
           "openclaw", "agy")
    return {cid: Det(cid in installed) for cid in ids}


def accounts(**creds):
    return {pid: ma.Account(bool(creds.get(pid)), ["env-file"] if creds.get(pid) else [], "", direct_key=bool(creds.get(pid)))
            for pid in ("anthropic", "google", "vertex", "openai", "ollama", "openrouter", "deepseek", "moonshot")}


def make_deps(tmp_path, installed=("claude", "codex", "agy", "openclaw"), env=None, creds=None, files=()):
    present = {str(tmp_path / f) for f in files}
    return detect.Deps(cockpits=lambda: cockpits(*installed), accounts=lambda: accounts(**(creds or {})),
                       env=lambda: dict(env or {}), file_exists=lambda p: p in present, home=tmp_path)


def by_id(items):
    return {i.id: i for i in items}


def test_codex_is_a_cockpit_with_no_dispatch_and_instructions_only_usage(tmp_path):
    inv = detect.inventory(make_deps(tmp_path))
    codex = by_id(inv.cockpits)["codex"]
    assert codex.installed and codex.usage == "instructions-only" and codex.capability == "no-dispatch"
    assert by_id(inv.engines)["codex"].usage == "instructions-only"


def test_agy_is_direct_only_with_the_faq_reason_and_its_pools_are_reporting_only(tmp_path):
    inv = detect.inventory(make_deps(tmp_path))
    agy = by_id(inv.cockpits)["agy"]
    assert agy.usage == "direct-only" and "ToS" in agy.reason and agy.capability == "report-only"
    anti = by_id(inv.engines)["antigravity"]
    assert anti.usage == "direct-only"
    pools = by_id(inv.pools)
    for pid in ("agy-gemini", "agy-claude-gpt"):
        assert pools[pid].present and pools[pid].usage == "direct-only" and "reporting only" in pools[pid].reason


def test_claude_is_dispatchable_and_an_instructions_only_cockpit_is_reported_as_such(tmp_path):
    inv = detect.inventory(make_deps(tmp_path, installed=("claude", "cursor")))
    c = by_id(inv.cockpits)
    assert c["claude"].usage == "dispatch-ok" and c["cursor"].usage == "instructions-only"
    assert by_id(inv.pools)["claude-subscription"].present


def test_codex_subscription_pool_comes_from_file_presence_only(tmp_path):
    inv = detect.inventory(make_deps(tmp_path, files=(".codex/auth.json",)))
    assert by_id(inv.pools)["codex-subscription"].present
    assert not by_id(detect.inventory(make_deps(tmp_path)).pools)["codex-subscription"].present


def test_the_auth_file_is_never_opened(tmp_path, monkeypatch):
    auth = tmp_path / ".codex" / "auth.json"
    auth.parent.mkdir()
    auth.write_text(SECRET)
    real_open = open

    def guarded(file, *a, **k):
        assert "auth.json" not in str(file), "the credential file was opened"
        return real_open(file, *a, **k)
    monkeypatch.setattr("builtins.open", guarded)
    real_read_text, real_read_bytes = Path.read_text, Path.read_bytes

    def guard(real):
        def read(self, *a, **k):
            assert "auth.json" not in str(self), "the credential file was read"
            return real(self, *a, **k)
        return read
    monkeypatch.setattr(Path, "read_text", guard(real_read_text))
    monkeypatch.setattr(Path, "read_bytes", guard(real_read_bytes))
    deps = make_deps(tmp_path)
    deps.file_exists = lambda p: Path(p).is_file()
    inv = detect.inventory(deps)
    assert by_id(inv.pools)["codex-subscription"].present


def test_openai_api_pool_needs_one_of_the_two_keys(tmp_path):
    assert not by_id(detect.inventory(make_deps(tmp_path)).pools)["openai-api"].present
    for name in ("CODEX_API_KEY", "OPENAI_API_KEY"):
        inv = detect.inventory(make_deps(tmp_path, env={name: "k"}))
        assert by_id(inv.pools)["openai-api"].present, name


def test_vertex_hint_reports_project_and_adc_presence_without_reading_it(tmp_path):
    adc = ".config/gcloud/application_default_credentials.json"
    inv = detect.inventory(make_deps(tmp_path, env={"GOOGLE_CLOUD_PROJECT": "p"}, files=(adc,)))
    assert "GOOGLE_CLOUD_PROJECT set" in by_id(inv.providers)["vertex"].hint
    assert "ADC file present" in by_id(inv.providers)["vertex"].hint


def test_provider_credentials_are_reported_by_name_and_source_only(tmp_path):
    inv = detect.inventory(make_deps(tmp_path, creds={"anthropic": True, "google": True}, env={"GEMINI_API_KEY": SECRET}))
    assert inv.credentialed_providers() == ["anthropic", "google"]
    assert SECRET not in repr(inv)


def test_inventory_does_not_run_readers_or_touch_network_or_write(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("detection called something it must not")
    for name in ("read_claude_sidecar", "read_openrouter_key", "read_vertex_quota", "read_kit_counters",
                 "read_for_pool"):
        monkeypatch.setattr(headroom_sources, name, boom)
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    detect.inventory(make_deps(tmp_path, files=(".codex/auth.json",)))
    assert sorted(p.name for p in tmp_path.rglob("*")) == before


def test_real_cockpit_probes_are_the_allowlisted_version_checks_only(tmp_path, monkeypatch):
    """default_deps wires `detection.detect_all_cockpits`; with the probe seam recorded, only `--version`
    style detector calls happen, and nothing is called with a prompt or a usage command."""
    calls = []
    monkeypatch.setattr(detection, "_run", lambda cmd, timeout=5: calls.append(list(cmd)) or (0, "1.2.3"))
    monkeypatch.setattr(detection, "_which", lambda b: f"/fake/bin/{b}")
    monkeypatch.setattr(detection, "_which_extra", lambda b: f"/fake/bin/{b}")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail(f"subprocess.run {a}"))
    found = detect.default_deps().cockpits()
    assert found["claude"].installed
    assert calls and all(any(flag in c for flag in ("--version", "version", "-V")) for c in calls), calls
    flat = [part for c in calls for part in c]
    assert "-p" not in flat and "/usage" not in flat and "--prompt" not in flat

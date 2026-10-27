"""ADR-0010 S8: doctor's Antigravity lane sub-check. Read-only, no agy, no new process.

The record, when there is one, lives in a throwaway kit root under pytest's tmp_path."""
from __future__ import annotations

import argparse
import subprocess

import pytest

import agy_evidence_fakes as fk
from ai_resources import agy_gate_evidence as ev
from ai_resources import doctor
from ai_resources.setup import lanes_render, state, ui


class _Console:
    def print(self, *a, **k):
        pass

    def rule(self, *a, **k):
        pass


@pytest.fixture(autouse=True)
def _fresh():
    ev.clear_cache()
    yield
    ev.clear_cache()


@pytest.fixture
def lines(monkeypatch):
    out = []
    for name in ("info", "detail", "warn", "error", "ok"):
        monkeypatch.setattr(ui, name, lambda msg, _n=name: out.append((_n, msg)))
    return out


@pytest.fixture
def no_process(monkeypatch):
    """AC-S8d: doctor's own checks start no `agy` and no `openclaw` process. The loader's read-only `git` calls on a
    kit that is a git root are the one process this sub-check may use, and are allowed through."""
    seen = []
    real_run, real_popen = subprocess.run, subprocess.Popen

    def guard(real):
        def wrapped(argv, *a, **k):
            seen.append(argv)
            first = str(argv[0] if isinstance(argv, (list, tuple)) else argv).rsplit("/", 1)[-1]
            assert first not in ("agy", "openclaw"), f"doctor started {first}"
            return real(argv, *a, **k)
        return wrapped
    monkeypatch.setattr(subprocess, "run", guard(real_run))
    monkeypatch.setattr(subprocess, "Popen", guard(real_popen))
    return seen


def _text(lines):
    return "\n".join(m for _n, m in lines)


def test_without_a_record_doctor_says_0_of_8_and_names_the_record_command(lines, no_process, monkeypatch, tmp_path):
    fk.use(monkeypatch, fk.make_root(tmp_path))
    assert doctor._check_antigravity_lane(state.SetupState()) == 0
    text = _text(lines)
    assert "gates 0/8 proven (G6, G7 partial)" in text
    assert "evidence record: absent" in text
    assert "ai-resources lanes gates record" in text
    assert "Claude Code x Google via OpenRouter: unverified" in text
    assert "Google acknowledgement: none stored" in text and "code-reviewer" in text
    assert not [n for n, _m in lines if n in ("warn", "error")]
    assert not any(str(a[0]).endswith(('agy', 'openclaw')) for a in no_process if isinstance(a, list))


def test_an_invalid_record_prints_the_reason_and_still_0_of_8(lines, no_process, monkeypatch, tmp_path):
    root = fk.make_root(tmp_path)
    fk.use(monkeypatch, root)
    doc = fk.valid_doc(root)
    doc["gates"][7]["ack_text_sha256"] = "0" * 64
    fk.write(root, fk.resealed(doc))
    assert doctor._check_antigravity_lane(state.SetupState()) == 1
    text = _text(lines)
    assert "gates 0/8 proven" in text and "evidence record: invalid" in text and "G7" in text
    assert ("warn", "The Antigravity evidence record is ignored: G7.ack_text_sha256 differs from the current Google "
                    "acknowledgement text") in lines
    assert "fix or restore the evidence record" in text


def test_a_valid_committed_record_prints_8_of_8_the_record_sha_and_a_verified_cell(lines, no_process, monkeypatch,
                                                                                   tmp_path):
    root = fk.make_root(tmp_path, git=True)
    fk.use(monkeypatch, root)
    fk.write(root, fk.valid_doc(root))
    fk.run_git(root, "add", ev.RECORD_REL)
    fk.run_git(root, "commit", "-q", "-m", "evidence")
    ev.clear_cache()
    from ai_resources.setup.cockpits import openclaw
    monkeypatch.setattr(openclaw, "backend_registered", lambda b: b == "agy-cli-lane")
    assert doctor._check_antigravity_lane(state.SetupState()) == 0
    head = fk.run_git(root, "rev-parse", "HEAD").strip()
    text = _text(lines)
    assert "on, gates 8/8 proven" in text and head[:8] in text and fk.RUN_ID in text
    assert "Claude Code x Google via OpenRouter: verified" in text
    assert "last touched by commit" in text and "Op Erator" in text
    assert "agy-cli-lane backend: registered" in text


def test_the_acknowledgement_that_misses_balanced_roles_is_named(lines, no_process, monkeypatch, tmp_path):
    fk.use(monkeypatch, fk.make_root(tmp_path))
    s = state.SetupState()
    s.profile.lane_acks = {"google": lanes_render.make_ack("google", ["explore"],
                                                          "2026-10-19T00:00:00+00:00", account="a@example.com")}
    doctor._check_antigravity_lane(s)
    text = _text(lines)
    assert "Balanced also uses: code-reviewer" in text
    assert "extend the Google acknowledgement to: code-reviewer" not in text   # held gates come first
    assert "probe window" in text


def test_the_full_doctor_runs_the_sub_check_and_never_invokes_agy(lines, no_process, monkeypatch, tmp_path):
    from ai_resources.setup.cockpits import openclaw
    fk.use(monkeypatch, fk.make_root(tmp_path))
    monkeypatch.setattr(ui, "require_deps", lambda: None)
    monkeypatch.setattr(ui, "console", lambda: _Console())
    monkeypatch.setattr(ui, "banner", lambda *a, **k: None)
    monkeypatch.setattr(ui, "section", lambda *a, **k: None)
    monkeypatch.setattr(state, "load", lambda: state.SetupState())
    monkeypatch.setattr(openclaw, "plugin_loaded", lambda: False)          # the existing OpenClaw probe (4c), mocked
    doctor.cmd_doctor(argparse.Namespace(skip_smoke=True))
    assert "gates 0/8 proven" in _text(lines)

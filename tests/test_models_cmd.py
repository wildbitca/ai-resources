"""The `ai-resources models` verbs. Fakes only (Env is shared with test_models_update)."""
from __future__ import annotations

import json

import pytest

from ai_resources import cli, models, models_cmd
from ai_resources import model_pins as mp
from ai_resources.setup import ui
from test_models_update import Env


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch)
    monkeypatch.setattr(models_cmd, "get_deps", e.deps)
    monkeypatch.setattr(mp, "overlay_path", lambda: e.overlay)
    return e


def run(*argv):
    return cli.main(["models", *argv])


def test_help_lists_every_verb(capsys):
    with pytest.raises(SystemExit):
        cli.main(["models", "--help"])
    out = capsys.readouterr().out
    for verb in models_cmd.VERBS:
        assert verb in out


def test_check_with_an_auto_candidate_is_11_and_writes_nothing(env, capsys):
    assert run("check") == 11
    assert not env.overlay.exists() and env.patches == [] and env.smokes == []
    assert "sonnet" in capsys.readouterr().out


def test_check_with_only_approvals_pending_is_10(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, priced=False)
    monkeypatch.setattr(models_cmd, "get_deps", e.deps)
    assert run("check") == 10
    assert not e.overlay.exists()


def test_check_refreshes_only_when_asked(env):
    run("check")
    assert not any("refresh" in a for a in env.runner_calls)
    env.runner_calls.clear()
    run("check", "--refresh")
    assert any("refresh" in a for a in env.runner_calls)


def test_approve_persists_and_the_next_plan_applies_it(tmp_path, monkeypatch, capsys):
    e = Env(tmp_path, monkeypatch, priced=False)
    monkeypatch.setattr(models_cmd, "get_deps", e.deps)
    monkeypatch.setattr(mp, "overlay_path", lambda: e.overlay)
    assert run("approve", "haiku", "claude-haiku-5-5") == 0
    assert mp.load_overlay(e.overlay)["approvals"] == {"haiku": "claude-haiku-5-5"}
    r = models.run_update(models.Options(check=True), e.deps())
    assert {p.cls: p.decision for p in r.proposals}["haiku"] == "approved" and r.rc == 11
    assert run("revoke", "haiku") == 0
    assert "haiku" not in mp.load_overlay(e.overlay)["approvals"]


def test_approve_validates_class_and_id(env, capsys):
    assert run("approve", "sonnet", "claude-opus-5") == 2
    assert run("approve", "nope", "claude-opus-5") == 2
    assert not env.overlay.exists()


def test_pin_unpin_and_exclude_persist(env):
    assert run("pin", "sonnet", "claude-sonnet-5") == 0
    ov = mp.load_overlay(env.overlay)
    assert ov["pins"]["sonnet"] == "claude-sonnet-5" and ov["policy"]["classes"]["sonnet"]["mode"] == "frozen"
    assert run("exclude", "claude-opus-5-5") == 0
    assert mp.load_overlay(env.overlay)["policy"]["exclude"] == ["claude-opus-5-5"]
    assert run("unpin", "sonnet") == 0
    ov = mp.load_overlay(env.overlay)
    assert "sonnet" not in ov["pins"] and "sonnet" not in ov["policy"]["classes"]


def test_a_pinned_class_is_not_updated(env):
    run("pin", "sonnet", "claude-sonnet-5")
    assert run("update", "--unattended", "--class", "sonnet") == 0 and env.patches == []


def test_update_unattended_never_prompts(env, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("prompted")
    monkeypatch.setattr(ui, "select", boom)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    assert run("update", "--unattended", "--class", "sonnet") == 0
    assert env.restarts == 1


def test_update_without_a_tty_never_prompts(env, monkeypatch):
    monkeypatch.setattr(ui, "select", lambda *a, **k: (_ for _ in ()).throw(AssertionError("prompted")))
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: False)
    assert run("update", "--class", "sonnet") == 0


def test_update_interactive_offers_the_pending_ones(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, priced=False)
    monkeypatch.setattr(models_cmd, "get_deps", e.deps)
    monkeypatch.setattr(mp, "overlay_path", lambda: e.overlay)
    monkeypatch.setattr(ui, "stdin_is_a_terminal", lambda: True)
    monkeypatch.setattr(ui, "is_non_interactive", lambda: False)
    asked = []
    monkeypatch.setattr(ui, "select", lambda msg, choices, default=None, **k: asked.append(msg) or (
        "Approve and apply" if msg.startswith("haiku") else "Skip"))
    run("update", "--no-restart")
    assert len(asked) == 3 and mp.load_overlay(e.overlay)["approvals"] == {"haiku": "claude-haiku-5-5"}


def test_update_json_output(env, capsys):
    run("update", "--dry-run", "--json", "--class", "sonnet")
    data = json.loads(capsys.readouterr().out)
    assert data["outcome"] == "dry_run" and data["applied"]["sonnet"] == ["claude-sonnet-5", "claude-sonnet-5-5"]


def test_rollback_verb(env):
    run("update", "--unattended", "--class", "sonnet")
    env.restarts = 0
    assert run("rollback") == 0
    assert "sonnet" not in mp.load_overlay(env.overlay)["pins"]
    assert env.doc["agents"]["entries"]["main"]["model"]["primary"] == "anthropic/claude-sonnet-5"
    assert run("rollback") == 4                      # nothing left to undo


def test_status_reports_drift_and_agy(env, capsys):
    mp.save_overlay({"pins": {"sonnet": "claude-sonnet-5-5"}}, env.overlay)
    assert run("status") == 0
    out = capsys.readouterr().out
    assert "DRIFT agents.entries.main.model.primary" in out
    assert "gemini-3.8-flash-low" in out and "pool" in out

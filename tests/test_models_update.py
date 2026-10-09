"""run_update against fakes only: no live unit, no network, no real config. (S7)"""
from __future__ import annotations

import copy
import json
import pathlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from ai_resources import audit, models
from ai_resources import model_pins as mp
from ai_resources import openclaw_host

FIX = pathlib.Path(__file__).parent / "fixtures" / "models"
CATALOG = (FIX / "claude-cli-catalog.json").read_text()
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)


class Env:
    """A fake host: records every effect, lets a test choose where it fails."""

    def __init__(self, tmp_path, monkeypatch, *, priced=True, health=True, busy=0, smoke_ok=True,
                 post_smoke_ok=True, defer_restart=False, rollback_ok=True, lock=True):
        if priced:   # make sonnet 5 -> 5-5 an auto bump
            prices = dict(audit.PRICES)
            prices["claude-sonnet-5-5"] = audit.PRICES["claude-sonnet-5"]
            monkeypatch.setattr(audit, "PRICES", prices)
        self.doc = json.loads((FIX / "openclaw-model-sites.json").read_text())
        self.events, self.patches, self.restarts, self.smokes, self.runner_calls = [], [], 0, [], []
        self.health_ok, self.busy_n, self.smoke_ok, self.post_smoke_ok = health, busy, smoke_ok, post_smoke_ok
        self.defer_restart, self.rollback_ok, self.lock_free = defer_restart, rollback_ok, lock
        self.pid, self.health_after_rollback = 100, True
        self.marker_during_restart = []
        self.marker = openclaw_host.Marker(tmp_path / "watchdog.off")
        self.overlay = tmp_path / "ov.json"
        self.cfg = tmp_path / "openclaw.json"
        self.cfg.write_text(json.dumps(self.doc))
        self.now = NOW

    # fakes -------------------------------------------------------------
    def runner(self, argv, **kw):
        self.runner_calls.append(argv)
        return (0, "ok") if "refresh" in argv else (0, CATALOG)

    def apply_patch(self, patch, *, dry_run=False, replace_paths=None):
        self.patches.append((copy.deepcopy(patch), dry_run))
        if not dry_run and self.rollback_ok is False and self.restarts:      # the inverse patch
            return False, "boom"
        if not dry_run:
            self.doc = models.apply_in_memory(self.doc, patch)
        return True, "ok"

    def smoke(self, model_id):
        self.smokes.append(model_id)
        post = self.restarts > 0
        ok = self.post_smoke_ok if post else self.smoke_ok
        return models.SmokeResult(ok, "ok" if ok else "bad", "OK")

    def restart(self):
        self.marker_during_restart.append(self.marker.exists())
        if self.defer_restart:
            self.defer_restart = False
            raise models.RestartDeferred("2 agent turn(s) in flight")
        self.restarts += 1
        self.pid += 1
        return True

    def health(self):
        if self.restarts >= 2:
            return self.health_after_rollback
        return self.health_ok

    @contextmanager
    def lock(self):
        yield self.lock_free

    def deps(self):
        return models.Deps(
            runner=self.runner, apply_patch=self.apply_patch, read_config=lambda: copy.deepcopy(self.doc),
            config_file=lambda: self.cfg, smoke=self.smoke, busy=lambda: self.busy_n, restart=self.restart,
            health=self.health, main_pid=lambda: str(self.pid), sleep=lambda s: None, monotonic=lambda: 0.0,
            now=lambda: self.now, marker=self.marker, overlay_file=self.overlay,
            backup_root=self.overlay.parent / "bk", lock=self.lock, log=self.events.append,
            health_tries=3, health_interval=0)

    def run(self, **opts):
        return models.run_update(models.Options(**opts), self.deps())

    def state(self):
        return mp.load_overlay(self.overlay)


@pytest.fixture
def env(tmp_path, monkeypatch):
    return Env(tmp_path, monkeypatch)


def real_patches(e):
    return [p for p, dry in e.patches if not dry]


def test_healthy_switch(env):
    r = env.run(classes=["sonnet"])
    assert (r.rc, r.outcome) == (0, "switched")
    assert len(real_patches(env)) == 1 and env.restarts == 1
    assert env.state()["pins"]["anthropic:sonnet"] == "claude-sonnet-5-5"
    assert env.state()["state"]["last_result"] == "switched"
    assert "pending_restart" not in env.state()["state"]
    assert any(e["event"] == "switched" for e in env.events)
    assert not env.marker.exists() and env.marker_during_restart == [True]
    assert env.smokes == ["claude-sonnet-5-5", "claude-sonnet-5-5"]      # pre and post
    assert env.doc["agents"]["entries"]["main"]["model"]["primary"] == "anthropic/claude-sonnet-5-5"


def test_pre_switch_smoke_failure_patches_nothing(env):
    env.smoke_ok = False
    r = env.run(classes=["sonnet"])
    assert r.rc == 4 and r.outcome == "smoke_failed" and env.patches == [] and env.restarts == 0


def test_busy_gateway_defers_without_patching(env):
    env.busy_n = 2
    r = env.run(classes=["sonnet"])
    assert r.rc == 75 and env.patches == []
    assert env.state()["state"]["deferrals"] == 1


def test_deferred_restart_is_resumed_without_discovery_or_patch(env):
    env.defer_restart = True
    assert env.run(classes=["sonnet"]).rc == 75
    st = env.state()["state"]
    assert st["pending_restart"] is True and st["deferrals"] == 1
    assert len(real_patches(env)) == 1
    env.runner_calls.clear()
    env.patches.clear()
    r = env.run(classes=["sonnet"])
    assert r.rc == 0 and env.runner_calls == [] and env.patches == []
    assert "pending_restart" not in env.state()["state"] and env.restarts == 1


def test_resume_skips_the_restart_when_the_gateway_already_restarted(env):
    env.defer_restart = True
    env.run(classes=["sonnet"])
    env.pid += 1                                  # the operator restarted by hand
    assert env.run().rc == 0 and env.restarts == 0


def test_unhealthy_gateway_rolls_back(env):
    env.health_ok = False
    env.health_after_rollback = True
    r = env.run(classes=["sonnet"])
    assert (r.rc, r.outcome) == (2, "rolled_back")
    st = env.state()
    assert "anthropic:sonnet" not in st["pins"] and st["state"]["last_result"] == "rolled_back"
    assert st["state"]["failed"] == {"anthropic:sonnet": "claude-sonnet-5-5"}
    assert env.restarts == 2 and not env.marker.exists()
    assert env.doc["agents"]["entries"]["main"]["model"]["primary"] == "anthropic/claude-sonnet-5"


def test_post_switch_smoke_failure_rolls_back(env):
    env.post_smoke_ok = False
    assert env.run(classes=["sonnet"]).rc == 2


def test_failed_rollback_is_critical(env):
    env.health_ok = False
    env.rollback_ok = False
    r = env.run(classes=["sonnet"])
    assert r.rc == 6 and env.state()["state"]["last_result"] == "rollback_failed" and not env.marker.exists()


def test_unhealthy_after_rollback_is_critical(env):
    env.health_ok = False
    env.health_after_rollback = False
    assert env.run(classes=["sonnet"]).rc == 6


def test_a_rolled_back_model_is_not_retried_unattended(env):
    env.health_ok = False
    env.run(classes=["sonnet"])
    env.now = NOW + timedelta(days=2)
    env.health_ok = True
    r = env.run(classes=["sonnet"])
    assert r.rc == 10 and len(real_patches(env)) == 2        # forward + inverse only


def test_an_approved_model_that_fails_is_not_retried_every_run(env):
    env.run(classes=["sonnet"], check=True)           # record nothing; just prove discovery works
    ov = env.state()
    ov.setdefault("approvals", {})["anthropic:sonnet"] = "claude-sonnet-5-5"
    mp.save_overlay(ov, env.overlay)
    env.health_ok = False
    assert env.run(classes=["sonnet"]).rc == 2
    st = env.state()
    assert st["state"]["failed"] == {"anthropic:sonnet": "claude-sonnet-5-5"}
    assert "anthropic:sonnet" not in (st.get("approvals") or {})
    patches_before = len(real_patches(env))
    env.now = NOW + timedelta(days=2)
    env.health_ok = True
    r = env.run(classes=["sonnet"])
    assert r.rc == 10 and len(real_patches(env)) == patches_before


def test_lock_held_returns_73_with_no_calls(env):
    env.lock_free = False
    assert env.run().rc == 73 and env.runner_calls == [] and env.patches == []


def test_cooldown_skips_discovery(env):
    env.run(classes=["sonnet"])
    env.runner_calls.clear()
    env.now = NOW + timedelta(hours=1)
    r = env.run()
    assert (r.rc, r.outcome) == (0, "cooldown") and env.runner_calls == []
    env.now = NOW + timedelta(hours=7)
    assert env.run().outcome == "pending_approval"      # opus and haiku still wait


def test_declining_everything_keeps_the_cooldown_and_applies_nothing(env):
    from ai_resources import models_interaction as mi
    env.run(classes=["sonnet"])
    env.runner_calls.clear()
    patches = len(env.patches)
    env.now = NOW + timedelta(hours=1)
    declined = {"anthropic:opus": mi.Answer(mi.KIND_NOT_NOW, "claude-opus-5-5"),
                "anthropic:haiku": mi.Answer(mi.KIND_NEVER_MODEL, "claude-haiku-5-5")}
    r = models.run_update(models.Options(), env.deps(), answers=declined)
    assert (r.rc, r.outcome) == (0, "cooldown") and env.runner_calls == [] and len(env.patches) == patches


def test_accepting_one_proposal_still_bypasses_the_cooldown(env):
    from ai_resources import models_interaction as mi
    env.run(classes=["sonnet"])
    env.now = NOW + timedelta(hours=1)
    r = models.run_update(models.Options(), env.deps(),
                          answers={"anthropic:opus": mi.Answer(mi.KIND_NOW, "claude-opus-5-5")})
    assert r.outcome != "cooldown"


def test_check_has_no_side_effects(env):
    before = env.overlay.exists()
    r = env.run(check=True)
    assert r.rc == 11 and env.smokes == [] and env.patches == [] and env.overlay.exists() == before


def test_check_with_only_approvals_pending_is_10(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, priced=False)
    assert e.run(check=True).rc == 10 and not e.overlay.exists()


def test_dry_run_smokes_and_validates_but_never_restarts(env):
    r = env.run(dry_run=True, classes=["sonnet"])
    assert r.rc == 0 and r.outcome == "dry_run"
    assert env.smokes == ["claude-sonnet-5-5"]
    assert env.patches and all(dry for _, dry in env.patches)
    assert env.restarts == 0 and not env.overlay.exists()


def test_needs_approval_runs_record_pending_and_return_10(tmp_path, monkeypatch):
    e = Env(tmp_path, monkeypatch, priced=False)
    r = e.run()
    assert r.rc == 10 and e.patches == [] and e.smokes == []
    pend = e.state()["pending"]
    assert pend["anthropic:sonnet"]["to"] == "claude-sonnet-5-5" and "price unknown" in pend["anthropic:sonnet"]["reason"]


def test_discovery_failure_is_exit_1(env):
    env.runner = lambda argv, **kw: (0, "ok") if "refresh" in argv else (1, "down")
    r = env.run()
    assert r.rc == 1 and env.patches == []


def test_no_restart_flag_leaves_the_gateway(env):
    r = env.run(classes=["sonnet"], no_restart=True)
    assert r.rc == 0 and env.restarts == 0 and len(real_patches(env)) == 1


def test_smoke_requires_the_served_model():
    def runner(argv, **kw):
        return 0, json.dumps({"result": "OK", "is_error": False, "modelUsage": {"claude-haiku-5-5": {}}})
    assert models.smoke("claude-haiku-5-5", runner).ok
    assert not models.smoke("claude-sonnet-5-5", runner).ok
    assert not models.smoke("claude-haiku-5-5", lambda a, **k: (1, "x")).ok
    assert not models.smoke("claude-haiku-5-5", lambda a, **k: (0, "not json")).ok
    assert not models.smoke("claude-haiku-5-5", lambda a, **k: (0, json.dumps({"result": "no", "modelUsage": {"claude-haiku-5-5": {}}}))).ok


def test_smoke_output_is_redacted_and_truncated():
    out = models.redact("x" * 500 + " token=abc123 sk-ant-abcdefghijk")
    assert len(out) <= 200
    assert "sk-ant" not in models.redact("sk-ant-abcdefghijk") and "abc123" not in models.redact("token=abc123")


# --- provider-agnostic runs (S6/S7) -------------------------------------------------------------

import dataclasses

import httpx

from ai_resources import model_accounts as ma
from ai_resources.selection import ProviderSel, Selection, SlotSel


def _selection(*providers, smoke_path="litellm"):
    slots = {"anthropic": ("anthropic:sonnet", "anthropic/claude-sonnet-5"),
             "google": ("google:gemini-flash", "google/gemini-3.7-flash")}
    s = Selection(shape="multi-provider", smoke_path=smoke_path)
    for p in providers:
        s.providers[p] = ProviderSel()
        s.slots[slots[p][0]] = SlotSel(ref=slots[p][1])
    return s


def _provider_env(tmp_path, monkeypatch, *providers, fail=(), **kw):
    e = Env(tmp_path, monkeypatch, priced=False)
    runner_calls = e.runner_calls

    def runner(argv, **k):
        runner_calls.append(argv)
        if "refresh" in argv:
            return 0, "ok"
        provider = argv[argv.index("--provider") + 1]
        if provider in fail:
            return 1, "boom"
        if provider == "google":      # one flash newer than the kit default (gemini-3.8-flash)
            cat = json.loads((FIX / "catalog-google.json").read_text())
            cat["models"].append({"key": "google/gemini-3.9-flash", "name": "Gemini 3.9 Flash", "available": True})
            return 0, json.dumps(cat)
        return 0, (FIX / "claude-cli-catalog.json").read_text()

    e.runner = runner
    gateway_hits = []

    def handler(req):
        gateway_hits.append(json.loads(req.content))
        return httpx.Response(200, json={"model": json.loads(req.content)["model"],
                                          "choices": [{"message": {"content": "OK"}}]})

    deps = dataclasses.replace(
        e.deps(), runner=runner, selection=lambda: _selection(*providers),
        accounts=lambda: {p: ma.Account(True, ["env-file"], direct_key=True) for p in providers},
        http=models.make_http(httpx.MockTransport(handler)), gateway=lambda: ("http://gw.invalid", "k"), **kw)
    e.deps = lambda: deps
    e.gateway_hits = gateway_hits
    return e


def test_every_enabled_provider_failing_is_a_plain_error(tmp_path, monkeypatch):
    e = _provider_env(tmp_path, monkeypatch, "anthropic", "google", fail={"google", "claude-cli"})
    r = e.run()
    assert r.rc == models.EXIT_ERROR == 1 and r.outcome == "error"


def test_one_failing_provider_is_a_row_not_an_error(tmp_path, monkeypatch):
    e = _provider_env(tmp_path, monkeypatch, "anthropic", "google", fail={"google"})
    r = e.run(check=True)
    rows = {p.provider: p.text for p in r.providers}
    assert rows["google"].startswith("discovery failed") and rows["anthropic"].endswith("listed")
    assert r.rc == models.EXIT_APPROVAL_PENDING and r.outcome == "pending_approval"     # anthropic still waits
    assert any(r_["provider"] == "google" for r_ in r.as_dict()["providers"])


def test_an_approved_google_bump_is_smoked_through_the_gateway_and_recorded_without_a_restart(tmp_path, monkeypatch):
    e = _provider_env(tmp_path, monkeypatch, "google")
    mp.save_overlay({"approvals": {"google:gemini-flash": "gemini-3.9-flash"}}, e.overlay)
    r = e.run()
    assert r.rc == 0 and r.outcome == "switched"
    assert e.gateway_hits and e.gateway_hits[0]["model"] == "google/gemini-3.9-flash"
    assert e.patches == [] and e.restarts == 0                        # no verified OpenClaw runtime: nothing to patch
    assert e.state()["pins"]["google:gemini-flash"] == "gemini-3.9-flash"


def test_a_google_bump_without_approval_waits_and_is_recorded_as_pending(tmp_path, monkeypatch):
    e = _provider_env(tmp_path, monkeypatch, "google")
    r = e.run()
    assert r.rc == models.EXIT_APPROVAL_PENDING
    assert e.state()["pending"]["google:gemini-flash"]["to"] == "gemini-3.9-flash"
    assert e.gateway_hits == []                                      # no smoke call before the human answers

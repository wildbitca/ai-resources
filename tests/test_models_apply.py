"""Patch building, backup, apply and rollback. No test touches a live config or the network."""
from __future__ import annotations

import builtins
import copy
import json
import os
import pathlib
import stat
from datetime import datetime, timezone

import pytest

from ai_resources import model_pins as mp
from ai_resources import models

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "models" / "openclaw-model-sites.json"
SONNET = ("anthropic/claude-sonnet-5", "anthropic/claude-sonnet-5-5")
HAIKU = ("anthropic/claude-haiku-4-5", "anthropic/claude-haiku-5-5")


@pytest.fixture
def doc():
    return json.loads(FIXTURE.read_text())


def refs(d):
    return {".".join(map(str, p)): r for p, r in models.collect_refs(d)}


def test_collect_refs_finds_every_site(doc):
    r = refs(doc)
    assert r["agents.defaults.model.primary"] == HAIKU[0]
    assert r["agents.defaults.model.fallbacks.0"] == SONNET[0]
    assert r["agents.defaults.heartbeat.model"] == HAIKU[0]
    assert r["agents.entries.snoutzone.model"] == SONNET[0]
    assert r["agents.entries.elinvo.model.fallbacks.0"] == HAIKU[0]
    assert r["agents.defaults.models.anthropic/claude-sonnet-5"] == SONNET[0]


def test_forward_patch_repoints_only_the_old_ref(doc):
    built = models.build_forward_patch(doc, dict([SONNET]))
    after = models.apply_in_memory(doc, built["patch"])
    assert after["agents"]["entries"]["main"]["model"]["primary"] == SONNET[1]
    assert after["agents"]["entries"]["snoutzone"]["model"] == SONNET[1]
    assert after["agents"]["entries"]["elinvo"]["model"]["primary"] == SONNET[1]
    assert after["agents"]["entries"]["elinvo"]["model"]["fallbacks"] == [HAIKU[0]]
    assert after["agents"]["defaults"]["model"]["fallbacks"] == [SONNET[1], "anthropic/claude-opus-5"]
    # other classes and the claude-kit ref are untouched
    assert after["agents"]["defaults"]["model"]["primary"] == HAIKU[0]
    assert after["agents"]["entries"]["devops"]["model"] == "anthropic/claude-opus-5"
    assert after["agents"]["entries"]["claude"]["model"]["primary"] == "claude-kit/claude-sonnet-5"
    # the allowlist gains the new key, keeps the old one, and copies the entry
    allow = after["agents"]["defaults"]["models"]
    assert allow[SONNET[1]] == allow[SONNET[0]] == {"agentRuntime": {"id": "claude-cli"}}
    assert "channels" not in built["patch"] and "bindings" not in json.dumps(built["patch"])


def test_inverse_restores_the_original(doc):
    built = models.build_forward_patch(doc, dict([SONNET, HAIKU]))
    forward = models.apply_in_memory(doc, built["patch"])
    assert forward != doc
    assert models.apply_in_memory(forward, built["inverse"]) == doc


def test_no_matching_reference_gives_an_empty_patch(doc):
    built = models.build_forward_patch(doc, {"anthropic/claude-fable-5-1": "anthropic/claude-fable-5-2"})
    assert built["patch"] == {} and built["inverse"] == {}


def test_existing_new_allowlist_key_is_not_overwritten_or_deleted(doc):
    doc["agents"]["defaults"]["models"][SONNET[1]] = {"agentRuntime": {"id": "claude-cli"}, "x": 1}
    built = models.build_forward_patch(doc, dict([SONNET]))
    assert SONNET[1] not in built["patch"]["agents"]["defaults"].get("models", {})
    assert models.apply_in_memory(models.apply_in_memory(doc, built["patch"]), built["inverse"]) == doc


class FakePatch:
    def __init__(self, fail_dry=False, fail_real=False):
        self.calls, self.fail_dry, self.fail_real = [], fail_dry, fail_real

    def __call__(self, patch, *, dry_run=False, replace_paths=None):
        self.calls.append((copy.deepcopy(patch), dry_run))
        if dry_run and self.fail_dry:
            return False, "schema error"
        if not dry_run and self.fail_real:
            return False, "write error"
        return True, "ok"


CHANGES = {"sonnet": ("claude-sonnet-5", "claude-sonnet-5-5")}


def test_apply_dry_run_then_real_then_overlay(doc, tmp_path):
    ov_file, patch = tmp_path / "ov.json", FakePatch()
    ok, msg, ov = models.apply_changes(doc, CHANGES, apply_patch=patch, overlay={}, overlay_file=ov_file)
    assert ok and [d for _, d in patch.calls] == [True, False]
    saved = mp.load_overlay(ov_file)
    assert saved["pins"]["anthropic:sonnet"] == "claude-sonnet-5-5"
    assert saved["state"]["last_change"]["previous_pins"] == {"anthropic:sonnet": None}
    assert saved["history"][-1]["action"] == "switch"


def test_rejected_dry_run_sends_nothing_and_keeps_the_overlay(doc, tmp_path):
    ov_file, patch = tmp_path / "ov.json", FakePatch(fail_dry=True)
    ok, msg, ov = models.apply_changes(doc, CHANGES, apply_patch=patch, overlay={}, overlay_file=ov_file)
    assert not ok and [d for _, d in patch.calls] == [True] and not ov_file.exists()


def test_failed_real_patch_keeps_the_overlay(doc, tmp_path):
    ov_file = tmp_path / "ov.json"
    ok, *_ = models.apply_changes(doc, CHANGES, apply_patch=FakePatch(fail_real=True), overlay={}, overlay_file=ov_file)
    assert not ok and not ov_file.exists()


def test_rollback_sends_the_inverse_dry_run_first_and_reverts_pins(doc, tmp_path):
    ov_file = tmp_path / "ov.json"
    _, _, ov = models.apply_changes(doc, CHANGES, apply_patch=FakePatch(), overlay={}, overlay_file=ov_file)
    patch = FakePatch()
    ok, msg, back = models.rollback_last(apply_patch=patch, overlay=ov, overlay_file=ov_file)
    assert ok and [d for _, d in patch.calls] == [True, False]
    assert patch.calls[0][0] == ov["state"]["last_change"]["inverse"]
    assert "anthropic:sonnet" not in mp.load_overlay(ov_file)["pins"]
    assert models.rollback_last(apply_patch=patch, overlay=back)[0] is False


def _spy_config_writes(monkeypatch, cfg):
    """Record every attempt to write, replace or rename the config file, through any common API."""
    writes = []
    real_open, real_os_open = builtins.open, os.open
    real_replace, real_rename = os.replace, os.rename

    def is_cfg(p):
        try:
            return os.fspath(p) == str(cfg)
        except TypeError:
            return False

    def spy_open(file, mode="r", *a, **kw):
        if is_cfg(file) and any(c in mode for c in "wax+"):
            writes.append(("open", mode))
        return real_open(file, mode, *a, **kw)

    def spy_os_open(path, flags, *a, **kw):
        if is_cfg(path) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            writes.append(("os.open", flags))
        return real_os_open(path, flags, *a, **kw)

    def spy_move(real, name):
        def inner(src, dst, *a, **kw):
            if is_cfg(src) or is_cfg(dst):
                writes.append((name, src, dst))
            return real(src, dst, *a, **kw)
        return inner

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    monkeypatch.setattr(os, "replace", spy_move(real_replace, "os.replace"))
    monkeypatch.setattr(os, "rename", spy_move(real_rename, "os.rename"))
    for name in ("write_text", "write_bytes"):
        orig = getattr(pathlib.Path, name)
        monkeypatch.setattr(pathlib.Path, name, lambda self, *a, _o=orig, _n=name, **k:
                            writes.append((_n, self)) if is_cfg(self) else _o(self, *a, **k))
    for name in ("rename", "replace"):
        orig = getattr(pathlib.Path, name)
        monkeypatch.setattr(pathlib.Path, name, lambda self, target, _o=orig, _n=name, **k:
                            writes.append((_n, self, target)) if (is_cfg(self) or is_cfg(target))
                            else _o(self, target, **k))
    return writes


@pytest.mark.parametrize("scenario", ["switch", "rollback"])
def test_nothing_ever_writes_openclaw_json_during_an_update(tmp_path, monkeypatch, scenario):
    import hashlib
    from test_models_update import Env
    env = Env(tmp_path, monkeypatch)
    cfg = env.cfg
    before = hashlib.sha256(cfg.read_bytes()).hexdigest()
    writes = _spy_config_writes(monkeypatch, cfg)
    if scenario == "rollback":
        env.health_ok = False
    r = env.run(classes=["sonnet"])
    assert r.rc == (0 if scenario == "switch" else 2)
    assert env.restarts >= 1 and any(not dry for _, dry in env.patches)      # the real path ran
    assert writes == []
    assert hashlib.sha256(cfg.read_bytes()).hexdigest() == before


def test_backup_keeps_the_newest_ten_with_mode_0600(tmp_path):
    cfg, ov = tmp_path / "openclaw.json", tmp_path / "model-pins.json"
    cfg.write_text("{}")
    ov.write_text("{}")
    root = tmp_path / "bk"
    for i in range(13):
        models.backup(cfg, overlay_file=ov, root=root, now=lambda i=i: datetime(2026, 1, 1, 0, i, tzinfo=timezone.utc))
    dirs = sorted(d.name for d in root.iterdir())
    assert len(dirs) == 10 and dirs[0].startswith("20260101T0003")
    for f in (root / dirs[-1]).iterdir():
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600


def test_lock_is_exclusive(tmp_path):
    path = tmp_path / "x.lock"
    with models.update_lock(path) as first:
        assert first is True
        with models.update_lock(path) as second:
            assert second is False
    with models.update_lock(path) as again:
        assert again is True


def test_failed_attempt_is_not_proposed_again_automatically():
    disc = models.Discovery(best={"sonnet": "claude-sonnet-5-5"})
    ov = {"state": {"failed": {"sonnet": "claude-sonnet-5-5"}}}
    p = models.propose(mp.DEFAULTS, disc, ov)[0]
    assert p.decision == "needs_approval" and "previous attempt failed" in p.reasons[0]

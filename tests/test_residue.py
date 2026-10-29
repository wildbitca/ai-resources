"""Shared caches, kit backups, the worktree-completion guard and the legacy ~/.openclaw dirs (ADR-0021)."""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_resources import doctor, residue, scratch

# Integration: touches or fakes an external tool (openclaw, node, git, installers...); skipped unless --integration (see tests/conftest.py).
pytestmark = pytest.mark.integration

DAY = 86400


@pytest.fixture
def env(tmp_path):
    return {"HOME": str(tmp_path / "home"), "XDG_CACHE_HOME": str(tmp_path / "cache")}


def _old(path: Path, days: float):
    ts = time.time() - days * DAY
    for p in [path, *path.rglob("*")]:
        os.utime(p, (ts, ts), follow_symlinks=False)


def test_default_cap_is_two_gib_and_configurable():
    assert scratch.DEFAULT_CAP_BYTES == 2 * 1024 ** 3
    assert scratch.cap_bytes({}) == 2 * 1024 ** 3
    assert scratch.cap_bytes({"AI_RESOURCES_SCRATCH_CAP_GB": "0.5"}) == 512 * 1024 ** 2
    assert scratch.cap_bytes({"AI_RESOURCES_SCRATCH_CAP_GB": "junk"}) == 2 * 1024 ** 3


def test_every_cache_points_at_the_one_shared_root(env):
    vals = residue.shared_env(env)
    root = Path(env["XDG_CACHE_HOME"]) / "ai-resources" / "shared"
    for key in ("GOCACHE", "GOMODCACHE", "npm_config_cache", "PNPM_STORE_DIR", "PIP_CACHE_DIR", "PUB_CACHE", "GRADLE_USER_HOME"):
        assert Path(vals[key]).parent == root
    run = scratch.new("scan", "x", env=env)
    assert scratch.env_for(run, env)["GOCACHE"] == vals["GOCACHE"]          # a run never gets a cache of its own


def test_two_scans_share_one_go_cache(env, tmp_path):
    full = {**os.environ, **env, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "scripts")}
    out = [subprocess.run(["python3", "-m", "ai_resources.scratch", "scan-env"], env=full, capture_output=True, text=True, timeout=60).stdout
           for _ in range(2)]
    goc = [next(ln for ln in o.splitlines() if ln.startswith("export GOCACHE=")) for o in out]
    assert goc[0] == goc[1] and "/ai-resources/shared/go-build" in goc[0]
    assert not (tmp_path / "home" / ".openclaw").exists()


def test_shared_prune_trims_oldest_files_to_the_cap(env):
    residue.ensure_shared(env)
    for i, age in enumerate((9, 5, 1)):
        f = residue.shared_dir(env) / "go-build" / f"f{i}"
        f.write_bytes(b"x" * 1000)
        os.utime(f, (time.time() - age * DAY,) * 2)
    dry = residue.prune_shared(cap=2500, dry_run=True, env=env)
    assert [Path(p).name for p, _ in dry["removed"]] == ["f0"] and (residue.shared_dir(env) / "go-build" / "f0").exists()
    res = residue.prune_shared(cap=2500, env=env)
    assert res["after"] == 2000 and not (residue.shared_dir(env) / "go-build" / "f0").exists()


def test_backups_expire_after_fourteen_days_by_marker(env):
    now = time.time()
    fresh = residue.backup_dir("repo snap", env, now=now)
    old = residue.backup_dir("old", env, now=now - 15 * DAY)
    unmarked = residue.backups_dir(env) / "mine"
    unmarked.mkdir()
    assert fresh.name.startswith("repo-snap-") and json.loads((fresh / residue.EXPIRES_NAME).read_text())["expires_epoch"] - now == 14 * DAY
    assert residue.prune_backups(env, now, dry_run=True) == [str(old)] and old.exists()
    scratch.prune(env=env, now=now)
    assert not old.exists() and fresh.exists() and unmarked.exists()


def test_prune_age_cap_and_log(env):
    now = time.time()
    keep = scratch.new("a", "young", env=env, prune_first=False, keep_on_fail=True)
    aged = scratch.new("a", "aged", env=env, prune_first=False)
    big = scratch.new("a", "big", env=env, prune_first=False)
    for r, days, pid in ((aged, 8, None), (big, 1, None), (keep, 0.1, os.getpid())):
        m = scratch.read_meta(r)
        m.update(created_epoch=now - days * DAY, owner_pid=pid or 99999999, owner_start=scratch._start_ticks(pid) if pid else None, lease_epoch=0, status="failed", failed_epoch=now)
        scratch._write_meta(r, m)
    (big / "out" / "blob").write_bytes(b"x" * 3000)
    res = scratch.prune(cap_bytes=2000, env=env, now=now)
    reasons = {Path(p).name: why for p, why, _ in res["removed"]}
    assert reasons == {aged.name: "older-than", big.name: "over-cap"} and keep.exists()
    log = (scratch.base_dir(env) / "prune.log").read_text()
    assert aged.name in log and big.name in log and "older-than" in log and "over-cap" in log


def test_keep_on_fail_is_explicit(env):
    plain = scratch.new("a", "p", env=env, prune_first=False)
    flagged = scratch.new("a", "k", env=env, prune_first=False, keep_on_fail=True)
    assert scratch.finish(plain, False, env=env) == "removed" and not plain.exists()
    assert scratch.finish(flagged, False, env=env) == "kept-logs"
    # kept dirs count toward the cap
    assert any(e["path"] == flagged for e in scratch.entries(env))


# --- worktree guard ---------------------------------------------------------------------------------------------

def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), "-c", "user.email=t@t", "-c", "user.name=t", *a], check=True, capture_output=True)


@pytest.fixture
def worktree(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    wt = tmp_path / "home" / ".openclaw" / "worktrees" / "fp" / "task"
    wt.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
    _git(wt, "commit", "-q", "--allow-empty", "-m", "base")
    _git(wt, "remote", "add", "origin", str(bare))
    _git(wt, "push", "-q", "origin", "main")
    return wt


def _fake_openclaw(wt, calls):
    def runner(argv):
        calls.append(argv)
        if argv[2] == "list":
            return SimpleNamespace(returncode=0, stdout=json.dumps({"worktrees": [{"id": "abc", "path": str(wt)}]}), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return runner


def test_worktree_done_refuses_unpushed_commits_and_removes_when_pushed(worktree):
    calls = []
    _git(worktree, "commit", "-q", "--allow-empty", "-m", "work")
    code, msg = residue.worktree_done(str(worktree), runner=_fake_openclaw(worktree, calls))
    assert code == 3 and "not on any remote" in msg and calls == []
    _git(worktree, "push", "-q", "origin", "main")
    code, msg = residue.worktree_done(str(worktree), runner=_fake_openclaw(worktree, calls))
    assert code == 0 and calls[-1] == ["openclaw", "worktrees", "remove", "abc", "--if-lossless"]


def test_worktree_done_refuses_dirty_trees_and_foreign_paths(worktree, tmp_path):
    (worktree / "new.txt").write_text("x")
    assert residue.worktree_done(str(worktree), runner=_fake_openclaw(worktree, []))[0] == 3
    assert residue.worktree_done(str(tmp_path), runner=_fake_openclaw(worktree, []))[0] == 2


# --- ~/.openclaw legacy dirs and the doctor -------------------------------------------------------------------------

@pytest.fixture
def oc(tmp_path, monkeypatch):
    home = tmp_path / "oc"
    for d in ("scratch/old", "scratch/new", "gocache/x", "tmp/cap", "security-scan/clone", "backups/snap", "agents/keep", "workspace/scratch/whisper", "scratch/api.key"):
        (home / d).mkdir(parents=True, exist_ok=True) if not d.endswith(".key") else (home / "scratch").mkdir(exist_ok=True)
        if not d.endswith(".key"):
            (home / d / "f").write_text("x")
    (home / "scratch" / "api.key").write_text("secret")
    (home / "cleanup-backup-20261028").mkdir()
    for p in (home / "scratch" / "old", home / "gocache" / "x", home / "tmp" / "cap", home / "security-scan" / "clone",
              home / "backups" / "snap", home / "cleanup-backup-20261028", home / "scratch" / "api.key", home / "agents" / "keep"):
        _old(p, 10) if p.is_dir() else os.utime(p, (time.time() - 10 * DAY,) * 2)
    monkeypatch.setenv("OPENCLAW_HOME", str(home))
    monkeypatch.setenv("OPENCLAW_SCAN_RESULTS_DIR", str(tmp_path / "scans"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    return home


def test_legacy_openclaw_report_is_dry_run_by_default(oc):
    rows = {Path(r["path"]).name: r for r in residue.openclaw_legacy_report()}
    assert {"old", "new", "x", "cap", "clone", "snap", "cleanup-backup-20261028", "api.key"} <= set(rows)
    assert not any(r["removed"] for r in rows.values()) and (oc / "scratch" / "old").exists()
    assert "keep" not in rows and "whisper" not in rows                       # agents/ and workspace/scratch are never listed


def test_apply_removes_only_old_unprotected_entries(oc):
    rows = {Path(r["path"]).name: r for r in residue.openclaw_legacy_report(apply=True)}
    assert rows["old"]["removed"] and rows["x"]["removed"] and rows["snap"]["removed"] and rows["cleanup-backup-20261028"]["removed"]
    assert (oc / "scratch" / "new").exists()                                  # under 7 days: kept
    assert (oc / "scratch" / "api.key").exists() and rows["api.key"]["protected"]
    assert (oc / "agents" / "keep").exists() and (oc / "workspace" / "scratch" / "whisper").exists()
    assert (oc / "scratch").is_dir()                                          # the dirs themselves stay


def test_doctor_fix_lists_and_only_apply_deletes(oc, tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    assert doctor._check_scratch(SimpleNamespace(fix=True), cache) >= 1
    assert (oc / "scratch" / "old").exists()                                  # --fix alone is a dry run
    doctor._check_scratch(SimpleNamespace(fix=True, apply=True), cache)
    assert not (oc / "scratch" / "old").exists()
    assert not doctor._check_scratch.__doc__ is None
    assert any(k.startswith("~/.openclaw/") for k in residue.sizes())

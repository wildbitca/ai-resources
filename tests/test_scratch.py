"""The one scratch root: create/cleanup, signals, keep-on-fail TTL, pruning by meta + pid, cap, doctor, scan retention."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ai_resources import scratch

DAY = 86400
_real_new = scratch.new
def _new_keep(*a, **k):
    k.setdefault("keep_on_fail", True)
    return _real_new(*a, **k)

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


@pytest.fixture
def env(tmp_path):
    return {"HOME": str(tmp_path / "home"), "XDG_CACHE_HOME": str(tmp_path / "cache")}


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _age(run: Path, *, days: float, owner: int | None = None, status: str | None = None, failed_days_ago: float | None = None):
    """Rewrite meta as a run created `days` ago whose owner is `owner` (default: a dead pid)."""
    meta = scratch.read_meta(run)
    created = time.time() - days * DAY
    pid = owner or _dead_pid()
    meta.update(created_epoch=created, owner_pid=pid, owner_start=scratch._start_ticks(pid), lease_epoch=0)
    if status:
        meta["status"] = status
    if failed_days_ago is not None:
        meta["failed_epoch"] = time.time() - failed_days_ago * DAY
    scratch._write_meta(run, meta)


def test_new_creates_layout_and_meta(env):
    run = _real_new("verifier", "My Task!", env=env)
    assert run.parent == Path(env["XDG_CACHE_HOME"]) / "ai-resources" / "runs"
    assert run.name.startswith("verifier-my-task-") and str(os.getpid()) in run.name
    assert {p.name for p in run.iterdir() if p.is_dir()} == set(scratch.SUBDIRS)
    meta = scratch.read_meta(run)
    assert meta["owner_pid"] == os.getpid() and meta["role"] == "verifier" and meta["keep_on_fail"] is False
    assert meta["created_at"].endswith("Z") and meta["status"] == "running"


def test_context_manager_exports_env_and_removes_on_success(env, monkeypatch):
    monkeypatch.delenv(scratch.ENV_SCRATCH, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", env["XDG_CACHE_HOME"])
    monkeypatch.setenv("TMPDIR", "/orig")
    with scratch.run("tester", "x") as run:
        assert os.environ[scratch.ENV_SCRATCH] == str(run) and os.environ["TMPDIR"] == str(run / "tmp")
        (run / "out" / "f").write_text("x")
    assert not run.exists()
    assert scratch.ENV_SCRATCH not in os.environ and os.environ["TMPDIR"] == "/orig"


def test_context_manager_keeps_only_logs_on_failure(env, monkeypatch):
    monkeypatch.delenv(scratch.ENV_SCRATCH, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", env["XDG_CACHE_HOME"])
    with pytest.raises(RuntimeError):
        with scratch.run("tester", "x", keep_on_fail=True) as run:
            (run / "logs" / "a.log").write_text("boom")
            (run / "clone" / "big").write_text("x" * 100)
            raise RuntimeError("fail")
    assert sorted(p.name for p in run.iterdir()) == [scratch.META_NAME, "logs"]
    assert (run / "logs" / "a.log").read_text() == "boom"
    meta = scratch.read_meta(run)
    assert meta["status"] == "failed" and "failed_epoch" in meta


def test_no_keep_on_fail_removes_everything(env):
    run = _new_keep("r", "s", keep_on_fail=False, env=env)
    assert scratch.finish(run, False, env=env) == "removed" and not run.exists()


def test_rm_refuses_paths_outside_the_runs_root(env, tmp_path):
    _new_keep("r", "s", env=env)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(ValueError):
        scratch.rm(outside, env=env)
    assert outside.is_dir()
    link = scratch.runs_dir(env) / "link"
    link.symlink_to(outside)
    with pytest.raises(ValueError):
        scratch.rm(link, env=env)
    assert outside.is_dir()


def _bash_script(tmp_path, env, body, flags='--keep-on-fail'):
    kit = Path(__file__).resolve().parents[1]
    script = tmp_path / "t.sh"
    script.write_text(f'. "{kit}/scripts/scratch.sh"\nscratch_begin tester trap {flags}\n{body}\n')
    full = {**os.environ, **env, "AGENT_KIT": str(kit)}
    full.pop(scratch.ENV_SCRATCH, None)
    return script, full


def test_bash_success_removes_the_run(tmp_path, env):
    script, full = _bash_script(tmp_path, env, 'echo "$AI_RESOURCES_SCRATCH" > "$OUT"; touch "$AI_RESOURCES_SCRATCH/out/x"')
    full["OUT"] = str(tmp_path / "path.txt")
    assert subprocess.run(["bash", str(script)], env=full, timeout=60).returncode == 0
    assert not Path((tmp_path / "path.txt").read_text().strip()).exists()


def test_bash_failure_keeps_logs_only(tmp_path, env):
    script, full = _bash_script(tmp_path, env, 'echo "$AI_RESOURCES_SCRATCH" > "$OUT"; echo hi > "$AI_RESOURCES_SCRATCH/logs/l"; touch "$AI_RESOURCES_SCRATCH/out/x"; exit 3')
    full["OUT"] = str(tmp_path / "path.txt")
    assert subprocess.run(["bash", str(script)], env=full, timeout=60).returncode == 3
    run = Path((tmp_path / "path.txt").read_text().strip())
    assert (run / "logs" / "l").is_file() and not (run / "out").exists()
    assert scratch.read_meta(run)["status"] == "failed"


def test_bash_sigterm_trap_cleans_up(tmp_path, env):
    script, full = _bash_script(tmp_path, env, 'echo "$AI_RESOURCES_SCRATCH" > "$OUT"; touch "$AI_RESOURCES_SCRATCH/out/x"; echo ready > "$READY"; while :; do sleep 0.2; done')
    full["OUT"], full["READY"] = str(tmp_path / "path.txt"), str(tmp_path / "ready")
    proc = subprocess.Popen(["bash", str(script)], env=full)
    for _ in range(300):
        if (tmp_path / "ready").exists():
            break
        time.sleep(0.1)
    run = Path((tmp_path / "path.txt").read_text().strip())
    assert run.is_dir()
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 143
    assert (run / "logs").is_dir() and not (run / "out").exists()      # SIGTERM is a failure: logs only
    assert scratch.read_meta(run)["status"] == "failed"


def test_python_sigterm_inside_context_marks_failure(tmp_path, env):
    code = (
        "import os, signal, sys, time\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "from ai_resources import scratch\n"
        "with scratch.run('impl', 'sig', keep_on_fail=True) as run:\n"
        "    open(os.environ['READY'], 'w').write(str(run))\n"
        "    time.sleep(60)\n")
    full = {**os.environ, **env, "READY": str(tmp_path / "ready")}
    full.pop(scratch.ENV_SCRATCH, None)
    proc = subprocess.Popen([sys.executable, "-c", code], env=full)
    for _ in range(300):
        if (tmp_path / "ready").exists():
            break
        time.sleep(0.1)
    run = Path((tmp_path / "ready").read_text())
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(timeout=30) == 143
    assert scratch.read_meta(run)["status"] == "failed" and not (run / "tmp").exists()


def test_failed_run_expires_after_three_days(env):
    fresh = _new_keep("a", "fresh", env=env)
    old = _new_keep("a", "old", env=env)
    for run in (fresh, old):
        scratch.finish(run, False, env=env)
    _age(old, days=4, status="failed", failed_days_ago=3.1)
    _age(fresh, days=1, status="failed", failed_days_ago=0.5)
    r = scratch.prune(env=env)
    assert [reason for _, reason, _ in r["removed"]] == ["failed-ttl"]
    assert fresh.is_dir() and not old.exists()


def test_prune_ignores_future_dated_mtimes(env):
    """Created by meta, not mtime: a 2286 mtime must not keep an old run alive, a 1970 one must not kill a young one."""
    old = _new_keep("a", "old", env=env, prune_first=False)
    young = _new_keep("a", "young", env=env, prune_first=False)
    _age(old, days=9)
    _age(young, days=0.2, owner=os.getpid())
    for run, ts in ((old, 10_000_000_000), (young, 1_800_000_000)):
        os.utime(run, (ts, ts))
        for sub in run.iterdir():
            os.utime(sub, (ts, ts))
    far = _new_keep("a", "far", env=env, prune_first=False)
    _age(far, days=9)
    os.utime(far / "logs", (2_000_000_000, 2_000_000_000))      # 2033; the fixture class that broke mtime guards
    r = scratch.prune(env=env)
    removed = {Path(p).name for p, _, _ in r["removed"]}
    assert old.name in removed and far.name in removed and young.name not in removed
    assert young.is_dir()


def test_live_pid_is_never_pruned_even_when_ancient_and_over_cap(env):
    run = _new_keep("a", "live", env=env)
    (run / "out" / "big").write_bytes(b"x" * 4096)
    _age(run, days=30, owner=os.getpid())
    r = scratch.prune(cap_bytes=1, env=env)
    assert run.is_dir() and r["skipped_live"] == 1 and not r["removed"]


def test_a_lease_keeps_a_run_live_without_an_owner_pid(env):
    run = _new_keep("impl", "orch", env=env, prune_first=False, lease_s=3600)
    meta = scratch.read_meta(run)
    assert meta["owner_pid"] == 0 and meta["lease_epoch"] > time.time()
    meta["created_epoch"] = time.time() - 3 * DAY                  # old, owner gone, but the lease holds
    scratch._write_meta(run, meta)
    r = scratch.prune(env=env)
    assert run.is_dir() and r["skipped_live"] == 1 and not r["converted"]
    meta["lease_epoch"] = time.time() - 1                           # lease over: now a crash
    scratch._write_meta(run, meta)
    assert str(run) in scratch.prune(env=env)["converted"]
    assert scratch.read_meta(run)["status"] == "failed"


def test_cli_new_without_a_pid_takes_a_lease(env, tmp_path):
    full = {**os.environ, **env, "PYTHONPATH": str(SCRIPTS)}
    full.pop(scratch.ENV_SCRATCH, None)
    out = subprocess.run([sys.executable, "-m", "ai_resources.scratch", "new", "--role", "x", "--slug", "y"],
                         env=full, capture_output=True, text=True, timeout=60)
    meta = scratch.read_meta(Path(out.stdout.strip()))
    assert meta["owner_pid"] == 0 and meta["lease_epoch"] - meta["created_epoch"] == scratch.DEFAULT_LEASE_S


def test_hostile_meta_cannot_make_a_run_permanent_or_stop_the_prune(env):
    fake_owner = _new_keep("a", "pid1", env=env, prune_first=False)
    bool_owner = _new_keep("a", "bool", env=env, prune_first=False)
    bad_epoch = _new_keep("a", "badnum", env=env, prune_first=False)
    logs_file = _new_keep("a", "logsfile", env=env, prune_first=False)
    for run, patch in ((fake_owner, {"owner_pid": 1, "owner_start": None}), (bool_owner, {"owner_pid": True, "owner_start": 1}),
                       (bad_epoch, {"status": "failed", "failed_epoch": "x", "owner_pid": 0}),
                       (logs_file, {"owner_pid": 0})):
        meta = scratch.read_meta(run)
        meta.update(patch, created_epoch=time.time() - 30 * DAY, lease_epoch=0)
        scratch._write_meta(run, meta)
    import shutil
    shutil.rmtree(logs_file / "logs")
    (logs_file / "logs").write_text("not a directory")
    meta = scratch.read_meta(logs_file)
    meta["created_epoch"] = time.time() - 0.5 * DAY
    scratch._write_meta(logs_file, meta)
    r = scratch.prune(env=env)
    assert not fake_owner.exists() and not bool_owner.exists() and not bad_epoch.exists()   # all reaped, none stuck "live"
    assert logs_file.is_dir() and (logs_file / "logs").is_dir()                              # converted despite the file


def test_a_symlink_logs_dir_is_replaced_not_followed(env, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("x")
    run = _new_keep("a", "link", env=env, prune_first=False)
    (run / "logs").rmdir()
    (run / "logs").symlink_to(victim)
    scratch.finish(run, False, env=env)
    assert (victim / "keep").is_file() and (run / "logs").is_dir() and not (run / "logs").is_symlink()


def test_the_same_lockfile_in_two_worktrees_shares_one_venv(tmp_path):
    a, b = tmp_path / "wt1" / "requirements.txt", tmp_path / "wt2" / "requirements.txt"
    for f in (a, b):
        f.parent.mkdir()
        f.write_text("pyyaml\n")
    assert scratch.venv_key([a]) == scratch.venv_key([b])


def test_done_and_rm_refuse_a_path_outside_the_root_without_a_traceback(env, tmp_path):
    full = {**os.environ, **env, "PYTHONPATH": str(SCRIPTS)}
    out = subprocess.run([sys.executable, "-m", "ai_resources.scratch", "done", str(tmp_path)], env=full, capture_output=True, text=True, timeout=60)
    assert out.returncode == 2 and "Traceback" not in out.stderr


def test_a_planted_module_in_the_cwd_does_not_run_from_scratch_sh(tmp_path):
    kit = Path(__file__).resolve().parents[1]
    (tmp_path / "json.py").write_text("raise SystemExit('PLANTED')\n")
    script = tmp_path / "t.sh"
    script.write_text(f'. "{kit}/scripts/scratch.sh"\n_scratch_py ls\n')
    out = subprocess.run(["bash", str(script)], cwd=tmp_path, env={**os.environ, "AGENT_KIT": str(kit),
                         "AI_RESOURCES_CACHE_DIR": str(tmp_path / "c")}, capture_output=True, text=True, timeout=60)
    assert "PLANTED" not in out.stdout + out.stderr and out.returncode == 0


def test_recycled_pid_is_not_alive(env):
    me = os.getpid()
    ticks = scratch._start_ticks(me)
    assert scratch.pid_alive(me, ticks)
    if ticks is not None:
        assert not scratch.pid_alive(me, ticks + 1)
    assert not scratch.pid_alive(_dead_pid())


def test_crashed_run_is_converted_to_logs_only_then_expires(env):
    run = _new_keep("a", "crash", env=env, prune_first=False)
    (run / "logs" / "l").write_text("x")
    (run / "clone" / "c").write_text("x" * 10)
    _age(run, days=0.5)                                          # owner dead, status still running
    r = scratch.prune(env=env)
    assert str(run) in r["converted"] and run.is_dir()
    assert (run / "logs" / "l").is_file() and not (run / "clone").exists()
    assert scratch.read_meta(run)["status"] == "failed"


def test_young_unknown_owner_gets_a_grace_period(env):
    run = _new_keep("a", "young", env=env, prune_first=False)
    _age(run, days=0)                                            # created now, owner pid already gone
    r = scratch.prune(env=env)
    assert run.is_dir() and not r["converted"]


def test_size_cap_removes_oldest_first(env):
    runs = []
    for i, age in enumerate((3, 2, 1)):
        run = _new_keep("a", f"s{i}", env=env, prune_first=False)
        (run / "out" / "blob").write_bytes(b"x" * 2000)
        scratch.finish(run, False, env=env) if False else None
        _age(run, days=age, status="failed", failed_days_ago=0.1)
        runs.append(run)
    r = scratch.prune(cap_bytes=5400, env=env)
    assert [Path(p).name for p, why, _ in r["removed"] if why == "over-cap"] == [runs[0].name]
    assert not runs[0].exists() and runs[1].is_dir() and runs[2].is_dir()


def test_dry_run_changes_nothing_and_reports(env):
    old = _new_keep("a", "old", env=env, prune_first=False)
    (old / "out" / "b").write_bytes(b"x" * 100)
    _age(old, days=9)
    crashed = _new_keep("a", "crashed", env=env, prune_first=False)
    (crashed / "clone" / "c").write_text("x")
    _age(crashed, days=0.5)
    before = sorted(str(p) for p in scratch.runs_dir(env).rglob("*"))
    r = scratch.prune(dry_run=True, env=env)
    assert r["dry_run"] and [why for _, why, _ in r["removed"]] == ["older-than"] and r["removed"][0][2] >= 100
    assert str(crashed) in r["converted"]
    assert sorted(str(p) for p in scratch.runs_dir(env).rglob("*")) == before


def test_dir_without_meta_or_stamp_is_left_alone(env):
    root = scratch.runs_dir(env)
    root.mkdir(parents=True)
    (root / "mystery").mkdir()
    r = scratch.prune(env=env)
    assert (root / "mystery").is_dir() and r["kept"] >= 1


def test_prune_uses_the_stamp_in_the_name_when_meta_is_missing(env):
    root = scratch.runs_dir(env)
    root.mkdir(parents=True)
    old = root / "verifier-x-20200101T000000-1-abc1"
    old.mkdir()
    scratch.prune(env=env)
    assert not old.exists()


def test_concurrent_prune_is_skipped_by_flock(env):
    import fcntl
    scratch.base_dir(env).mkdir(parents=True)
    with open(scratch.base_dir(env) / ".prune.lock", "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        r = scratch.prune(env=env, quiet_lock=True)
    assert r["locked"]


def test_new_prunes_opportunistically(env):
    old = _new_keep("a", "old", env=env, prune_first=False)
    _age(old, days=9)
    _new_keep("a", "next", env=env)
    assert not old.exists()


def test_venvs_are_keyed_and_pruned_by_last_use(env, tmp_path):
    lock = tmp_path / "req.txt"
    lock.write_text("pyyaml\n")
    k1 = scratch.venv_key([lock])
    lock.write_text("pyyaml\nhttpx\n")
    assert scratch.venv_key([lock]) != k1 and k1.startswith(f"py{sys.version_info.major}.{sys.version_info.minor}-")
    idle, used, unknown = (scratch.venvs_dir(env) / n for n in ("idle", "used", "unknown"))
    for d in (idle, used, unknown):
        d.mkdir(parents=True)
    scratch.touch_venv(idle, now=time.time() - 31 * DAY)
    scratch.touch_venv(used)
    os.utime(used, (1, 1))                                       # an ancient mtime does not matter
    r = scratch.prune(env=env)
    assert r["venvs_removed"] == [str(idle)] and used.is_dir() and unknown.is_dir()


def test_scratch_cli_new_ls_prune(env, tmp_path):
    full = {**os.environ, **env, "PYTHONPATH": str(SCRIPTS)}
    full.pop(scratch.ENV_SCRATCH, None)
    def cli(*a):
        return subprocess.run([sys.executable, "-m", "ai_resources.scratch", *a], env=full, capture_output=True, text=True, timeout=60)
    out = cli("new", "--role", "cr", "--slug", "x", "--owner-pid", str(os.getpid()))
    run = Path(out.stdout.strip())
    assert out.returncode == 0 and run.is_dir()
    assert run.name in cli("ls").stdout
    assert "[dry-run]" in cli("prune", "--dry-run").stdout
    assert cli("done", str(run), "--status", "0").returncode == 0 and not run.exists()
    assert cli("rm", str(tmp_path)).returncode == 2


# --- doctor: legacy top-level names --------------------------------------------------------------------------------

LEGACY = ["ai-resources-pytest-s5", "pt-A1", "pytest-models", "verifier-3", "verif22c", "verify22e", "tmp-verify", "tmp-impl",
          "tmp-kit22b", "tmpab12cd34", "tmp.AbCdEfGhIj", "vtmp", "tmpv", "rv-tmp", "cr", "cr-9", "review", "ai-security-work",
          "aisec-1", "sec-audit-2026-10-01", "secaudit-x", "secscan-2026-10-02", "ci-2.1.1b.out", "ci-A1.log", "full3.log",
          "brew-up.log", "ai-resources-test-venv", "kit-venv", "airvenv", "x_fixed.py", "run.sh"]
REAL = ["go-build", "gomod", "uv", "pnpm", "pip", "claude", "openclaw", "p10k-bitgandtter", "gitstatus", "Unity", "Homebrew",
        "ai-resources", "ms-playwright-go", "node", "checkov"]


def _cache(tmp_path):
    root = tmp_path / "dotcache"
    root.mkdir()
    for n in LEGACY + REAL:
        (root / n).mkdir()
        (root / n / "f").write_text("x")
    return root


def test_legacy_detection_flags_scratch_and_never_real_caches(tmp_path):
    root = _cache(tmp_path)
    names = {p.name for p in scratch.legacy_candidates(root)}
    assert names == set(LEGACY)


def test_fix_removes_only_old_unused_legacy_entries(tmp_path):
    root = _cache(tmp_path)
    old = time.time() - 2 * DAY
    for n in LEGACY + REAL:
        for p in [root / n, root / n / "f"]:
            os.utime(p, (old, old))
    young = root / "pt-A1"
    os.utime(young, None)
    os.utime(young / "f", None)
    future = root / "verifier-3"
    os.utime(future, (10_000_000_000, 10_000_000_000))           # a future mtime reads as "just written": kept
    os.utime(future / "f", (10_000_000_000, 10_000_000_000))
    in_use = root / "cr"
    proc = subprocess.Popen(["sleep", "30"], cwd=in_use)
    try:
        dry = scratch.legacy_report(root, fix=False)
        assert len(dry) == len(LEGACY) and not any(r["removed"] for r in dry) and all((root / n).exists() for n in LEGACY)
        rows = {Path(r["path"]).name: r for r in scratch.legacy_report(root, fix=True)}
    finally:
        proc.kill()
        proc.wait()
    assert rows["cr"]["in_use"] and (root / "cr").exists()
    assert (root / "pt-A1").exists() and (root / "verifier-3").exists()
    gone = set(LEGACY) - {"cr", "pt-A1", "verifier-3", "run.sh"}     # a loose .sh may be the operator's own: report only
    assert all(not (root / n).exists() for n in gone)
    assert all((root / n).exists() for n in REAL) and (root / "run.sh").exists()


# --- security-scan-results retention ---------------------------------------------------------------------------------

def test_scan_retention_keeps_seven_basic_and_two_weekly(tmp_path):
    root = tmp_path / "scans"
    root.mkdir()
    names = [f"2026-10-{d:02d}-basic" for d in range(1, 13)] + ["2026-10-05-weekly", "2026-10-05-weekly.out", "2026-10-13-weekly.out", "2026-10-13-weekly", "notes.txt"]
    for n in names:
        p = root / n
        if n.endswith(".out") or n.endswith(".txt"):
            p.write_text("x")
        else:
            p.mkdir()
            (p / "r.json").write_text("{}")
        ts = 10_000_000_000 - len(n) if n.startswith("2026-10-01") else 1_000_000   # mtimes must not drive the order
        os.utime(p, (ts, ts))
    r = scratch.prune_scan_results(root)
    left = sorted(p.name for p in root.iterdir())
    assert len(r["kept"]) == 9                                   # 7 basic + 2 weekly
    basics = [f"2026-10-{d:02d}-basic" for d in range(6, 13)]
    assert left == sorted(basics + ["2026-10-05-weekly", "2026-10-05-weekly.out", "2026-10-13-weekly", "2026-10-13-weekly.out", "notes.txt"])
    # a third weekly pushes the oldest weekly (and its .out sidecar) out
    (root / "2026-10-20-weekly").mkdir()
    scratch.prune_scan_results(root)
    assert not (root / "2026-10-05-weekly").exists() and not (root / "2026-10-05-weekly.out").exists()
    assert (root / "2026-10-13-weekly").exists() and (root / "2026-10-20-weekly").exists()


def test_scan_retention_dry_run_and_cli(tmp_path):
    root = tmp_path / "scans"
    root.mkdir()
    for d in range(1, 10):
        (root / f"2026-10-{d:02d}-basic").mkdir()
    r = scratch.prune_scan_results(root, keep=7, dry_run=True)
    assert len(r["removed"]) == 2 and len(list(root.iterdir())) == 9
    out = subprocess.run([sys.executable, "-m", "ai_resources.scratch", "prune-scans", "--root", str(root)],
                         env={**os.environ, "PYTHONPATH": str(SCRIPTS)}, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and len(list(root.iterdir())) == 7
    assert not (root / "2026-10-01-basic").exists() and (root / "2026-10-09-basic").exists()


def test_scan_size_helper(tmp_path):
    (tmp_path / "a").write_bytes(b"x" * 10)
    assert scratch.scan_results_bytes(tmp_path) == 10


def test_doctor_check_warns_on_legacy_and_fix_removes_only_safe(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from ai_resources import doctor
    root = _cache(tmp_path)
    monkeypatch.setenv("OPENCLAW_HOME", str(tmp_path / "ochome"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(root))
    monkeypatch.setenv("OPENCLAW_SCAN_RESULTS_DIR", str(tmp_path / "scans"))
    monkeypatch.setenv("OPENCLAW_HOME", str(tmp_path / "ochome"))
    old = time.time() - 2 * DAY
    for n in LEGACY:
        os.utime(root / n, (old, old))
        os.utime(root / n / "f", (old, old))
    os.utime(root / "pt-A1" / "f", None)                         # just written: survives --fix
    assert doctor._check_scratch(SimpleNamespace(fix=False), root) == 1
    assert all((root / n).exists() for n in LEGACY)
    assert doctor._check_scratch(SimpleNamespace(fix=True, apply=True), root) == 1   # pt-A1 is left, so still one issue
    assert (root / "pt-A1").exists() and not (root / "vtmp").exists()
    assert all((root / n).exists() for n in REAL)


def test_doctor_check_warns_when_scan_results_exceed_500_mb(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from ai_resources import doctor
    scans = tmp_path / "scans"
    scans.mkdir()
    (scans / "2026-10-01-basic").mkdir()
    monkeypatch.setattr(scratch, "SCAN_WARN_BYTES", 5)
    (scans / "2026-10-01-basic" / "big").write_bytes(b"x" * 10)
    monkeypatch.setenv("OPENCLAW_SCAN_RESULTS_DIR", str(scans))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "empty"))
    monkeypatch.setenv("OPENCLAW_HOME", str(tmp_path / "ochome"))
    assert doctor._check_scratch(SimpleNamespace(fix=False), tmp_path / "empty") == 1


def test_scan_names_with_impossible_dates_are_left_alone(tmp_path):
    root = tmp_path / "scans"
    root.mkdir()
    for n in ("2026-13-45-basic", "2026-02-30-basic"):
        (root / n).mkdir()
    for d in range(1, 9):
        (root / f"2026-10-{d:02d}-basic").mkdir()
    scratch.prune_scan_results(root, keep=7)
    assert (root / "2026-13-45-basic").exists() and (root / "2026-02-30-basic").exists()
    assert not (root / "2026-10-01-basic").exists()


def test_ls_survives_a_null_status(env, capsys):
    run = _new_keep("a", "n", env=env, prune_first=False)
    meta = scratch.read_meta(run)
    meta["status"] = None
    scratch._write_meta(run, meta)
    os.environ["AI_RESOURCES_CACHE_DIR"] = str(scratch.base_dir(env))
    try:
        assert scratch._cmd_ls(None) == 0
    finally:
        del os.environ["AI_RESOURCES_CACHE_DIR"]
    assert "unknown" in capsys.readouterr().out

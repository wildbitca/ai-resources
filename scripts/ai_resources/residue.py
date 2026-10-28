"""Everything else that agent work leaves behind (ADR-0021): shared tool caches, kit backups, scan helpers, the
worktree-completion guard and the legacy ~/.openclaw scratch directories.

scratch.py owns the per-run scratch root; this module owns what must be shared or retained on purpose.
Stdlib only.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import scratch

SHARED_TOOLS = ("go-build", "go-mod", "npm", "pnpm-store", "pip", "pub", "gradle")
SHARED_CAP_BYTES = 6 * 1024 ** 3                    # AI_RESOURCES_SHARED_CAP_GB overrides
BACKUP_TTL_S = 14 * 86400
LEGACY_MIN_AGE_S = 7 * 86400
EXPIRES_NAME = ".expires.json"
# Never listed, never removed, whatever else matches.
PROTECTED = re.compile(r"(secret|credential|passw|token|\.key$|\.pem$|id_rsa|id_ed25519|\.env$)", re.I)


# --- shared tool caches ------------------------------------------------------------------------------------------

def shared_dir(env=None) -> Path:
    return scratch.base_dir(env) / "shared"


def shared_env(env=None) -> dict[str, str]:
    """Environment that points every package/build cache at ONE place. A task never gets a cache of its own."""
    root = shared_dir(env)
    return {
        "GOCACHE": str(root / "go-build"), "GOMODCACHE": str(root / "go-mod"),
        "npm_config_cache": str(root / "npm"), "PNPM_STORE_DIR": str(root / "pnpm-store"),
        "npm_config_store_dir": str(root / "pnpm-store"), "PIP_CACHE_DIR": str(root / "pip"),
        "PUB_CACHE": str(root / "pub"), "GRADLE_USER_HOME": str(root / "gradle"),
    }


def ensure_shared(env=None) -> dict[str, str]:
    vals = shared_env(env)
    for tool in SHARED_TOOLS:
        (shared_dir(env) / tool).mkdir(parents=True, exist_ok=True)
    return vals


def prune_shared(cap: int | None = None, dry_run: bool = False, env=None) -> dict:
    """Trim the shared caches to `cap` bytes, oldest files first (a cache entry's age is its use: this is the one place
    mtime is the right signal, because nothing here is a run with an owner)."""
    cap = cap or scratch.cap_bytes(env, "AI_RESOURCES_SHARED_CAP_GB", SHARED_CAP_BYTES)
    root = shared_dir(env)
    files = []
    if root.is_dir():
        for dirpath, _dirs, names in os.walk(root, followlinks=False):
            for n in names:
                p = os.path.join(dirpath, n)
                with contextlib.suppress(OSError):
                    st = os.lstat(p)
                    files.append((min(st.st_mtime, time.time()), st.st_size, p))
    total = sum(f[1] for f in files)
    before, removed = total, []
    for _mtime, size, p in sorted(files):
        if total <= cap:
            break
        removed.append((p, size))
        total -= size
        if not dry_run:
            with contextlib.suppress(OSError):
                os.unlink(p)
    return {"before": before, "after": total, "cap": cap, "removed": removed, "dry_run": dry_run}


# --- kit backups -------------------------------------------------------------------------------------------------

def backups_dir(env=None) -> Path:
    return scratch.base_dir(env) / "backups"


def backup_dir(name: str, env=None, now: float | None = None) -> Path:
    """`~/.cache/ai-resources/backups/<name>-<date>` with a 14-day expiry marker. The only place the kit puts a backup."""
    now = time.time() if now is None else now
    safe = scratch._slug(name, "backup")
    target = backups_dir(env) / f"{safe}-{time.strftime('%Y%m%d', time.gmtime(now))}"
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    (target / EXPIRES_NAME).write_text(json.dumps({"created_epoch": now, "expires_epoch": now + BACKUP_TTL_S,
                                                   "expires_at": scratch._iso(now + BACKUP_TTL_S)}))
    return target


def prune_backups(env=None, now: float | None = None, dry_run: bool = False) -> list[str]:
    now = time.time() if now is None else now
    root = backups_dir(env)
    out = []
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if child.is_symlink() or not child.is_dir():
            continue
        try:
            exp = scratch._num(json.loads((child / EXPIRES_NAME).read_text()).get("expires_epoch"))
        except (OSError, ValueError, AttributeError):
            continue                          # no marker: not ours to expire
        if exp is not None and exp <= now:
            out.append(str(child))
            if not dry_run:
                shutil.rmtree(child, ignore_errors=True)
    return out


# --- scans: read existing checkouts, never clone or build under ~/.openclaw ---------------------------------------

def find_checkout(name: str, roots: list[Path] | None = None, depth: int = 3) -> Path | None:
    """An existing checkout named `name` (a directory holding .git) under ~/Development. Scans read it in place."""
    roots = roots or [Path.home() / "Development"]
    for root in roots:
        stack = [(root, 0)]
        while stack:
            cur, d = stack.pop()
            try:
                kids = sorted(c for c in cur.iterdir() if c.is_dir() and not c.is_symlink() and not c.name.startswith("."))
            except OSError:
                continue
            for c in kids:
                if c.name == name and (c / ".git").exists():
                    return c
                if d + 1 < depth:
                    stack.append((c, d + 1))
    return None


# --- worktree completion ------------------------------------------------------------------------------------------

def _git(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=60)


def unpushed_problem(path: Path) -> str:
    """Why this worktree must not be removed ('' when nothing would be lost): dirty files, or commits no remote has."""
    if not (path / ".git").exists():
        return f"{path} is not a git worktree"
    st = _git(path, "status", "--porcelain")
    if st.returncode != 0:
        return f"git status failed: {st.stderr.strip()[:120]}"
    if st.stdout.strip():
        return "uncommitted changes"
    ahead = _git(path, "rev-list", "--count", "HEAD", "--not", "--remotes")
    if ahead.returncode != 0:
        return f"cannot compare with the remotes: {ahead.stderr.strip()[:120]}"
    if int(ahead.stdout.strip() or 0) > 0:
        return f"{ahead.stdout.strip()} commit(s) not on any remote"
    return ""


def worktree_done(path: str, runner=None) -> tuple[int, str]:
    """Remove a managed worktree after its work is pushed or merged. Refuses (exit 3) with unpushed commits or dirty files."""
    runner = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=300))
    p = Path(path).resolve()
    wt_root = (Path.home() / ".openclaw" / "worktrees").resolve()
    if wt_root not in p.parents:
        return 2, f"{p} is not under {wt_root}: only OpenClaw-managed worktrees are removed"
    problem = unpushed_problem(p)
    if problem:
        return 3, f"refusing to remove {p}: {problem}. Push or merge first."
    listed = runner(["openclaw", "worktrees", "list", "--json"])
    try:
        rows = json.loads(listed.stdout)["worktrees"]
    except (ValueError, KeyError, TypeError):
        return 4, "could not read `openclaw worktrees list --json`"
    wid = next((r["id"] for r in rows if Path(r.get("path", "")).resolve() == p), None)
    if not wid:
        return 4, f"{p} is not a managed worktree"
    done = runner(["openclaw", "worktrees", "remove", wid, "--if-lossless"])
    if done.returncode != 0:
        return 5, f"openclaw refused: {(done.stderr or done.stdout).strip()[:200]}"
    return 0, f"removed worktree {wid}"


# --- sizes and legacy ~/.openclaw dirs -----------------------------------------------------------------------------

OPENCLAW_LEGACY = ("scratch", "tmp", "scan", "gocache", "security-scan", "backups")
OPENCLAW_REPORT_ONLY = ("workspace/scratch",)       # whisper-models may duplicate ~/.local/share: the owner decides
OPENCLAW_OLD_GLOBS = ("cleanup-backup-*",)


def openclaw_home() -> Path:
    return Path(os.environ.get("OPENCLAW_HOME") or Path.home() / ".openclaw")


def sizes(env=None) -> dict[str, int]:
    home = openclaw_home()
    out = {"~/.cache/ai-resources": scratch.dir_size(scratch.base_dir(env)) if scratch.base_dir(env).is_dir() else 0}
    for name in (*OPENCLAW_LEGACY, *OPENCLAW_REPORT_ONLY, "security-scan-results"):
        p = home / name
        out[f"~/.openclaw/{name}"] = scratch.dir_size(p) if p.is_dir() else 0
    return out


def _newest(path: Path, now: float) -> float:
    return scratch._newest_epoch(path)


def openclaw_legacy_report(*, apply: bool = False, now: float | None = None, min_age_s: float = LEGACY_MIN_AGE_S) -> list[dict]:
    """Children of the legacy ~/.openclaw dirs (and cleanup-backup-*): listed always, removed only with `apply`, only when
    nothing inside changed for 7 days, no process is inside, and the name is not credential-shaped. The dirs themselves stay."""
    now = time.time() if now is None else now
    home = openclaw_home()
    targets: list[Path] = []
    for name in OPENCLAW_LEGACY:
        d = home / name
        if d.is_dir() and not d.is_symlink():
            targets += sorted(c for c in d.iterdir())
    for pat in OPENCLAW_OLD_GLOBS:
        targets += sorted(home.glob(pat))
    rows = []
    for p in targets:
        protected = bool(PROTECTED.search(p.name))
        age = now - _newest(p, now)
        busy = scratch._in_use(p) if p.is_dir() and not p.is_symlink() else False
        row = {"path": str(p), "bytes": scratch.dir_size(p) if p.is_dir() and not p.is_symlink() else p.lstat().st_size,
               "age_s": age, "in_use": busy, "protected": protected, "removed": False}
        if apply and not protected and not busy and age >= min_age_s:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    p.unlink()
            row["removed"] = not (p.exists() or p.is_symlink())
        rows.append(row)
    return rows


# --- CLI ---------------------------------------------------------------------------------------------------------

def add_cli(s) -> None:
    sd = s.add_parser("shared-env", help="Print export lines that point GOCACHE, GOMODCACHE, npm, pnpm, pip, pub and gradle at the shared caches")
    sd.set_defaults(func=lambda a: (print("\n".join(f"export {k}={scratch._q(v)}" for k, v in ensure_shared().items())), 0)[1])
    bk = s.add_parser("backup-dir", help="Create ~/.cache/ai-resources/backups/<name>-<date> with a 14-day expiry marker; prints the path")
    bk.add_argument("name")
    bk.set_defaults(func=lambda a: (print(backup_dir(a.name)), 0)[1])
    sc = s.add_parser("scan-env", help="Start a scan run: a scratch run plus the shared caches, as export lines (eval them)")
    sc.add_argument("--name", default="scan")
    sc.add_argument("--keep-on-fail", action="store_true")
    sc.set_defaults(func=_cmd_scan_env)
    co = s.add_parser("scan-checkout", help="Print the existing checkout of a repo under ~/Development (scans read it in place); exit 1 if none")
    co.add_argument("name")
    co.set_defaults(func=lambda a: _print_checkout(a.name))
    wt = s.add_parser("worktree-done", help="Remove a managed worktree once pushed or merged; refuses with unpushed commits or dirty files")
    wt.add_argument("path")
    wt.set_defaults(func=_cmd_worktree_done)
    dr = s.add_parser("dir", help="Print the runs root (the one place scratch lives)")
    dr.set_defaults(func=lambda a: (print(scratch.runs_dir()), 0)[1])


def _cmd_scan_env(a) -> int:
    run = scratch.new("scan", a.name, keep_on_fail=a.keep_on_fail, owner_pid=os.getppid())
    ensure_shared()
    print("\n".join(f"export {k}={scratch._q(v)}" for k, v in scratch.env_for(run).items()))
    return 0


def _print_checkout(name: str) -> int:
    found = find_checkout(name)
    if not found:
        print(f"no checkout named {name} under ~/Development", file=sys.stderr)
        return 1
    print(found)
    return 0


def _cmd_worktree_done(a) -> int:
    code, msg = worktree_done(a.path)
    print(msg, file=sys.stderr if code else sys.stdout)
    return code

"""The one scratch root of the kit (ADR-0020).

Every run that needs scratch space (a verifier's clone, a pytest basetemp, a review checkout, a log) gets its own
directory under ``~/.cache/ai-resources/runs/<role>-<slug>-<UTCstamp>-<pid>-<rand>/`` with the subdirectories
``tmp/ pytest/ logs/ out/ clone/``. Nothing is written directly under ``~/.cache`` or ``/tmp``.

    success  -> the whole directory is removed
    failure  -> only ``logs/`` stays, for 3 days (``keep_on_fail`` off removes everything)
    crash    -> the owner pid is gone and the run never finished: pruning treats it as a failure

Pruning decides by ``.scratch-meta.json`` (``created_at``, owner pid + start ticks), never by mtime: fixtures with
future-dated mtimes (2027, 2286) already broke mtime guards. Live owners are skipped, ``flock`` serialises pruners,
and a global cap (5 GiB, oldest first) bounds what is left.

Shared virtualenvs live in ``~/.cache/ai-resources/venvs/<python>-<lockhash>/`` and are pruned by last use (30 days).

This module is stdlib-only on purpose: it is imported by tests/conftest.py before anything else and run with
``python3 -m ai_resources.scratch`` from shell scripts.
"""
from __future__ import annotations

import argparse
import atexit
import contextlib
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterator

ENV_SCRATCH = "AI_RESOURCES_SCRATCH"
ENV_BASE = "AI_RESOURCES_CACHE_DIR"      # overrides the whole ~/.cache/ai-resources root (tests, odd hosts)
META_NAME = ".scratch-meta.json"
SUBDIRS = ("tmp", "pytest", "logs", "out", "clone")
FAILED_TTL_S = 3 * 86400
DEFAULT_OLDER_THAN_S = 7 * 86400
VENV_MAX_IDLE_S = 30 * 86400
DEFAULT_CAP_BYTES = 5 * 1024 ** 3
DEFAULT_LEASE_S = 6 * 3600
CRASH_GRACE_S = 120                      # a run younger than this whose pid is unknown is not judged
SYSTEM_TMP = ("/tmp", "/var/tmp")
RULE = "Never write directly under ~/.cache or /tmp; use $AI_RESOURCES_SCRATCH (tmp/ pytest/ logs/ out/ clone/)."

_SLUG = re.compile(r"[^a-z0-9]+")
_NAME_STAMP = re.compile(r"-(\d{8}T\d{6})Z?-\d+-[0-9a-f]+$")


# --- locations ---------------------------------------------------------------------------------------------------

def base_dir(env=None) -> Path:
    env = os.environ if env is None else env
    if env.get(ENV_BASE):
        return Path(env[ENV_BASE])
    cache = env.get("XDG_CACHE_HOME") or str(Path(env.get("HOME") or Path.home()) / ".cache")
    return Path(cache) / "ai-resources"


def runs_dir(env=None) -> Path:
    return base_dir(env) / "runs"


def venvs_dir(env=None) -> Path:
    return base_dir(env) / "venvs"


def _slug(text: str, default: str) -> str:
    out = _SLUG.sub("-", (text or "").lower()).strip("-")[:40]
    return out or default


def _stamp(now: float) -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime(now))


def _iso(now: float) -> str:
    return _dt.datetime.fromtimestamp(now, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- process liveness --------------------------------------------------------------------------------------------

def _start_ticks(pid: int) -> int | None:
    """Field 22 of /proc/<pid>/stat: protects against a recycled pid. None when unreadable (not Linux, gone)."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
        return int(data.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def pid_alive(pid: int, start_ticks: int | None = None) -> bool:
    """The owner is alive when its pid exists AND its start time matches the one recorded: a recycled pid, or a
    hostile meta naming pid 1, is not the owner. Without /proc (not Linux) the pid check stands alone."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return False
    if _start_ticks(os.getpid()) is not None and not isinstance(start_ticks, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    except OSError:
        return False
    if start_ticks is not None:
        now = _start_ticks(pid)
        if now is not None and now != start_ticks:
            return False
    return True


# --- meta --------------------------------------------------------------------------------------------------------

def read_meta(run: Path) -> dict | None:
    try:
        data = json.loads((run / META_NAME).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_meta(run: Path, meta: dict) -> None:
    tmp = run / (META_NAME + ".tmp")
    tmp.write_text(json.dumps(meta, indent=2, sort_keys=True))
    os.replace(tmp, run / META_NAME)


def _num(value) -> float | None:
    """A finite number from meta, else None: a corrupt or hostile field must never raise inside a prune."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if value == value and abs(value) != float("inf") else None


def _created_at(run: Path, meta: dict | None) -> float | None:
    """When the run began: meta first, then the UTC stamp in its name. Never the mtime."""
    if meta and _num(meta.get("created_epoch")) is not None:
        return _num(meta["created_epoch"])
    m = _NAME_STAMP.search(run.name)
    if m:
        try:
            return _dt.datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").replace(tzinfo=_dt.timezone.utc).timestamp()
        except ValueError:
            return None
    return None


# --- create / finish / remove ------------------------------------------------------------------------------------

def _safe_run(path: Path | str, env=None) -> Path:
    """A path that is a direct child of the runs root; anything else raises (rm must never reach elsewhere)."""
    p = Path(path)
    root = runs_dir(env)
    if p.is_symlink() or p.parent.resolve() != root.resolve() or p.name.startswith("."):
        raise ValueError(f"{p} is not a scratch run under {root}")
    return p


def new(role: str, slug: str = "", *, keep_on_fail: bool = True, env=None, now: float | None = None,
        prune_first: bool = True, owner_pid: int | None = None, lease_s: float | None = None) -> Path:
    """Create a run directory with its subdirectories and meta. Prunes opportunistically first."""
    now = time.time() if now is None else now
    root = runs_dir(env)
    root.mkdir(parents=True, exist_ok=True)
    if prune_first:
        with contextlib.suppress(Exception):
            prune(env=env, now=now, quiet_lock=True)
    # A run held by a short-lived process (the orchestrator CLI, an agent's per-call shell) is owned by a lease, not a
    # pid: it is live until `lease_epoch`, whatever happens to the process that created it.
    owner = owner_pid if owner_pid else (0 if lease_s is not None else os.getpid())
    name = f"{_slug(role, 'run')}-{_slug(slug, 'job')}-{_stamp(now)}-{os.getpid()}-{secrets.token_hex(2)}"
    run = root / name
    run.mkdir(mode=0o700)
    for sub in SUBDIRS:
        (run / sub).mkdir(mode=0o700)
    _write_meta(run, {
        "owner_pid": owner, "owner_start": _start_ticks(owner),
        "created_at": _iso(now), "created_epoch": now, "role": role, "slug": slug,
        "keep_on_fail": bool(keep_on_fail), "status": "running",
        **({"lease_epoch": now + lease_s} if lease_s is not None else {}),
    })
    return run


def env_for(run: Path) -> dict[str, str]:
    return {ENV_SCRATCH: str(run), "TMPDIR": str(run / "tmp")}


def finish(run: Path | str, ok: bool, *, env=None, now: float | None = None) -> str:
    """Success removes the run; failure keeps logs/ only (TTL 3 days). Returns 'removed' or 'kept-logs'."""
    now = time.time() if now is None else now
    run = _safe_run(run, env)
    if not run.is_dir():
        return "removed"
    meta = read_meta(run) or {}
    if ok or not meta.get("keep_on_fail", True):
        shutil.rmtree(run, ignore_errors=True)
        return "removed"
    for child in run.iterdir():
        if child.name in ("logs", META_NAME):
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                child.unlink()
    logs = run / "logs"
    if logs.is_symlink() or (logs.exists() and not logs.is_dir()):
        with contextlib.suppress(OSError):
            logs.unlink()
    logs.mkdir(exist_ok=True)
    meta.update(status="failed", failed_at=_iso(now), failed_epoch=now)
    _write_meta(run, meta)
    return "kept-logs"


def rm(run: Path | str, *, env=None) -> bool:
    run = _safe_run(run, env)
    if not run.exists():
        return False
    shutil.rmtree(run, ignore_errors=True)
    return True


@contextlib.contextmanager
def run(role: str, slug: str = "", *, keep_on_fail: bool = True, env=None) -> Iterator[Path]:
    """Create a run, export AI_RESOURCES_SCRATCH + TMPDIR, clean up on exit.

    An exception (or SIGTERM / SIGINT, turned into SystemExit while inside) marks the run failed."""
    target = os.environ if env is None else env
    run_dir = new(role, slug, keep_on_fail=keep_on_fail, env=env)
    saved = {k: target.get(k) for k in (ENV_SCRATCH, "TMPDIR")}
    target.update(env_for(run_dir))
    prev_tmp = tempfile.tempdir
    tempfile.tempdir = None
    handlers = {}
    if _main_thread():
        def _die(signum, _frame):
            raise SystemExit(128 + signum)
        for sig in (signal.SIGTERM, signal.SIGINT):
            handlers[sig] = signal.signal(sig, _die)
    ok = False
    try:
        yield run_dir
        ok = True
    finally:
        for sig, h in handlers.items():
            signal.signal(sig, h)
        for k, v in saved.items():
            if v is None:
                target.pop(k, None)
            else:
                target[k] = v
        tempfile.tempdir = prev_tmp
        finish(run_dir, ok, env=env)


def _main_thread() -> bool:
    import threading
    return threading.current_thread() is threading.main_thread()


# --- process-wide scratch for library code -----------------------------------------------------------------------

_process_run: Path | None = None


def current(env=None) -> Path | None:
    value = (os.environ if env is None else env).get(ENV_SCRATCH)
    return Path(value) if value else None


def tmp_root(role: str = "proc") -> Path:
    """`$AI_RESOURCES_SCRATCH/tmp` when a run exported one, else a run owned by this process (removed at exit)."""
    global _process_run
    cur = current()
    if cur is not None:
        t = cur / "tmp"
        t.mkdir(parents=True, exist_ok=True)
        return t
    if _process_run is None or not _process_run.is_dir():
        _process_run = new(role, Path(sys.argv[0] or "py").stem)
        atexit.register(lambda p=_process_run: finish(p, True) if p.is_dir() else None)
    return _process_run / "tmp"


def new_tmpdir(prefix: str = "tmp-") -> str:
    return tempfile.mkdtemp(prefix=prefix, dir=tmp_root())


@contextlib.contextmanager
def tempdir(prefix: str = "tmp-") -> Iterator[str]:
    d = new_tmpdir(prefix)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def atomic_tmp(prefix: str = ".", suffix: str = ".tmp", *, dir: Path | str) -> tuple[int, str]:
    """A temp file *beside* its destination, for write-then-rename. Same directory by design: rename is atomic only
    within a filesystem, and the file is gone (renamed) before this returns control to the caller."""
    return tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=str(dir))


def make_blank_file(directory: Path | str, prefix: str = "", suffix: str = "") -> str:
    """A closed empty file in `directory` (a path in the caller's own tree, not scratch)."""
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=str(directory))
    os.close(fd)
    return name


# --- listing -----------------------------------------------------------------------------------------------------

def dir_size(path: Path) -> int:
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for f in files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, f)).st_size
    return total


def entries(env=None, now: float | None = None) -> list[dict]:
    root = runs_dir(env)
    out = []
    if not root.is_dir():
        return out
    for child in sorted(root.iterdir()):
        if child.is_symlink() or not child.is_dir() or child.name.startswith("."):
            continue
        meta = read_meta(child)
        out.append({
            "path": child, "meta": meta, "created": _created_at(child, meta),
            "status": str((meta or {}).get("status") or "unknown"),
            "alive": bool(meta) and (pid_alive(meta.get("owner_pid"), meta.get("owner_start"))
                                     or (_num(meta.get("lease_epoch")) or 0) > (time.time() if now is None else now)),
            "size": dir_size(child),
        })
    return out


# --- prune -------------------------------------------------------------------------------------------------------

def parse_duration(text: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*", text or "")
    if not m:
        raise ValueError(f"bad duration {text!r} (use 90m, 12h, 7d, 2w)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]


@contextlib.contextmanager
def _lock(env=None, blocking: bool = True) -> Iterator[bool]:
    base = base_dir(env)
    base.mkdir(parents=True, exist_ok=True)
    fh = open(base / ".prune.lock", "a+")
    try:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        fh.close()


def prune(*, older_than: float = DEFAULT_OLDER_THAN_S, cap_bytes: int = DEFAULT_CAP_BYTES, dry_run: bool = False,
          env=None, now: float | None = None, quiet_lock: bool = False) -> dict:
    """Remove what is expired. Returns {'removed': [(path, reason, bytes)], 'kept': n, 'skipped_live': n, ...}."""
    now = time.time() if now is None else now
    result = {"removed": [], "kept": 0, "skipped_live": 0, "converted": [], "locked": False,
              "venvs_removed": [], "dry_run": dry_run}
    if dry_run and not base_dir(env).is_dir():
        return result                            # a dry run creates nothing, not even the lock file
    with _lock(env, blocking=not quiet_lock) as got:
        if not got:
            result["locked"] = True
            return result
        doomed: dict[Path, str] = {}
        kept = []
        every = entries(env, now)
        for e in every:
            path, meta, created = e["path"], e["meta"], e["created"]
            try:
                if e["alive"]:
                    result["skipped_live"] += 1
                    continue
                if created is None:
                    result["kept"] += 1          # no meta and no stamp in the name: not ours to judge
                    continue
                status = (meta or {}).get("status", "running")
                if now - created >= older_than:
                    doomed[path] = "older-than"
                elif status == "failed":
                    failed = _num((meta or {}).get("failed_epoch"))
                    if now - (failed if failed is not None else created) >= FAILED_TTL_S:
                        doomed[path] = "failed-ttl"
                    else:
                        kept.append(e)
                elif now - created < CRASH_GRACE_S or not meta:
                    kept.append(e)               # a run with no readable meta is kept until `older_than`
                elif meta.get("keep_on_fail", True):
                    # the owner (and its lease) is gone without finishing: a crash is a failure; keep logs for the TTL
                    result["converted"].append(str(path))
                    if not dry_run:
                        finish(path, False, env=env, now=now)
                        e["size"] = dir_size(path)
                    kept.append(e)
                else:
                    doomed[path] = "crashed"
            except OSError:
                result["kept"] += 1              # one unreadable run never stops the prune of the others
        # global cap: oldest first; live runs count toward the total but are never removed
        total = sum(e["size"] for e in every if e["path"] not in doomed)
        for e in sorted(kept, key=lambda x: x["created"] or 0):
            if total <= cap_bytes:
                break
            doomed[e["path"]] = "over-cap"
            total -= e["size"]
        sizes = {e["path"]: e["size"] for e in every}
        for path, reason in doomed.items():
            if not dry_run:
                shutil.rmtree(path, ignore_errors=True)
            result["removed"].append((str(path), reason, sizes.get(path, 0)))
        result["kept"] += sum(1 for e in kept if e["path"] not in doomed)
        result["venvs_removed"] = _prune_venvs(env, now, dry_run)
    return result


def _prune_venvs(env, now: float, dry_run: bool) -> list[str]:
    root = venvs_dir(env)
    removed = []
    if not root.is_dir():
        return removed
    with open(root / ".venv.lock", "a+") as lockfh:       # the lock ensure_venv holds: no prune between build and use
        fcntl.flock(lockfh, fcntl.LOCK_EX)
        return _prune_venvs_locked(root, now, dry_run)


def _prune_venvs_locked(root: Path, now: float, dry_run: bool) -> list[str]:
    removed = []
    for child in sorted(root.iterdir()):
        if child.is_symlink() or not child.is_dir() or child.name.startswith("."):
            continue
        try:
            last = float(json.loads((child / ".venv-meta.json").read_text()).get("last_used_epoch"))
        except (OSError, ValueError, TypeError):
            continue                          # no record of use: left for a human, never guessed from mtime
        if now - last >= VENV_MAX_IDLE_S:
            removed.append(str(child))
            if not dry_run:
                shutil.rmtree(child, ignore_errors=True)
    return removed


# --- shared virtualenvs ------------------------------------------------------------------------------------------

def venv_key(lockfiles: list[Path | str], python: str | None = None) -> str:
    h = hashlib.sha256()
    for f in sorted(str(x) for x in lockfiles):
        h.update(Path(f).name.encode() + b"\0" + Path(f).read_bytes() + b"\0")     # content, not location: worktrees share
    ver = f"py{sys.version_info.major}.{sys.version_info.minor}" if python is None else _py_version(python)
    return f"{ver}-{h.hexdigest()[:12]}"


def _py_version(python: str) -> str:
    out = subprocess.run([python, "-c", "import sys;print('py%d.%d'%sys.version_info[:2])"],
                         capture_output=True, text=True, timeout=30, check=True).stdout.strip()
    return out


def venv_path(lockfiles: list[Path | str], python: str | None = None, env=None) -> Path:
    return venvs_dir(env) / venv_key(lockfiles, python)


def touch_venv(path: Path, now: float | None = None) -> None:
    now = time.time() if now is None else now
    (path / ".venv-meta.json").write_text(json.dumps({"last_used_epoch": now, "last_used": _iso(now)}))


def _build_venv(target: Path, lockfiles, python: str | None, install: bool) -> None:
    py = python or sys.executable
    uv = shutil.which("uv")
    made = subprocess.run([py, "-m", "venv", str(target)], capture_output=True, text=True, timeout=300)
    if made.returncode != 0:
        shutil.rmtree(target, ignore_errors=True)
        if not uv:                                   # distro pythons often lack ensurepip
            raise subprocess.CalledProcessError(made.returncode, made.args, made.stdout, made.stderr)
        subprocess.run([uv, "venv", "-q", "--python", py, str(target)], check=True, timeout=300)
        pip = [uv, "pip", "install", "-q", "--python", str(target / "bin" / "python")]
    else:
        pip = [str(target / "bin" / "pip"), "install", "-q"]
    if install:
        for f in lockfiles:
            subprocess.run([*pip, "-r", str(f)], check=True, timeout=1200)


def ensure_venv(lockfiles: list[Path | str], python: str | None = None, env=None, install: bool = True) -> Path:
    """The shared venv for this python + lockfile hash; created (under flock) on first use."""
    target = venv_path(lockfiles, python, env)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target.parent / ".venv.lock", "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        if not (target / ".venv-meta.json").exists():     # written only after a complete build
            shutil.rmtree(target, ignore_errors=True)
            try:
                _build_venv(target, lockfiles, python, install)
            except (subprocess.SubprocessError, OSError):
                shutil.rmtree(target, ignore_errors=True)    # never leave a half-built venv under a valid key
                raise
        if install:                                           # a --no-install venv is not "complete": the next install builds it
            touch_venv(target)
    return target


# --- legacy top-level names under ~/.cache (doctor) --------------------------------------------------------------

# Real tool caches: never flagged, never touched.
ALLOWLIST = frozenset({
    "go-build", "gomod", "go", "uv", "pnpm", "pip", "pipx", "pkg", "claude", "claude-cli-nodejs", "openclaw",
    "p10k-bitgandtter", "gitstatus", "Unity", "Homebrew", "ai-resources", "node", "node-gyp", "node-compile-cache",
    "ms-playwright", "ms-playwright-go", "google-chrome", "google-chrome-headless", "mesa_shader_cache", "nvidia",
    "fontconfig", "gstreamer-1.0", "glycin", "dconf", "gh", "helm", "checkov", "semgrep_version", "snyk", "nuclei",
    "golangci-lint", "goimports", "kubebuilder-envtest", "JNA", "Microsoft", "__pycache__", "antigravity",
    "code-server", "deviceid", "gem", "locks", "maestro", "python-entrypoints", "sessions", "org.graalvm.polyglot",
    "pre-commit", "yarn", "npm", "ms-python", "typescript", "mypy_cache", ".mypy_cache", "ruff", "pytest_cache",
})
ALLOW_PREFIXES = ("p10k-", "gitstatus", "Homebrew", "go-build", "motd.")

LEGACY_PATTERNS = tuple(re.compile(p) for p in (
    r"^ai-resources-pytest($|[-_.])", r"^ai-resources-pytest-runs$", r"^pt-[A-Za-z0-9]+$", r"^pytest-models$",
    r"^verifier-", r"^verif\d*[a-z]?$", r"^verify\d*[a-z]?$", r"^tmp-verify", r"^tmp-impl", r"^tmp-kit", r"^tmpv$",
    r"^tmp[A-Za-z0-9_]{8}$", r"^tmp\.[A-Za-z0-9]{10}$", r"^vtmp$", r"^rv-tmp$", r"^cr$", r"^cr-", r"^review$",
    r"^ai-security-work$", r"^aisec-", r"^sec-audit-", r"^secaudit-", r"^secscan-",
    r"^ci-[\w.]+\.(out|log)$", r"^ci-[A-Z]\d*\.log$", r"^full[\w.-]*\.log$", r"^brew-[\w.-]*\.log$",
    r"^ai-resources-.*-venv$", r"^kit-venv$", r"^airvenv$", r".*_fixed\.py$", r"^[\w.-]+\.sh$",
))
# Reported by doctor but never removed by --fix: a loose shell script may be the operator's own (a one-time GCP
# bootstrap script sat among them on 2026-10-28), unlike the scratch directories above.
REPORT_ONLY = (re.compile(r"^[\w.-]+\.sh$"),)


def legacy_candidates(cache_root: Path) -> list[Path]:
    out = []
    if not cache_root.is_dir():
        return out
    for child in sorted(cache_root.iterdir()):
        name = child.name
        if name in ALLOWLIST or name.startswith(ALLOW_PREFIXES):
            continue
        if any(p.search(name) for p in LEGACY_PATTERNS):
            out.append(child)
    return out


def _in_use(path: Path) -> bool:
    """True when any process has its cwd or an open file inside `path` (reads /proc; unreadable pids are skipped)."""
    prefix = str(path.resolve())
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            targets = [os.readlink(proc / "cwd")]
            targets += [os.readlink(fd) for fd in (proc / "fd").iterdir()]
        except OSError:
            continue
        for t in targets:
            if t == prefix or t.startswith(prefix + os.sep):
                return True
    return False


def _newest_epoch(path: Path) -> float:
    """Youngest mtime inside path. Used only to avoid deleting what was just written; future dates read as 'now'."""
    now = time.time()
    newest = 0.0
    try:
        newest = min(os.lstat(path).st_mtime, now)
        if path.is_symlink():
            return newest                      # a link is judged by itself, never by what it points at
        for root, dirs, files in os.walk(path):
            for n in dirs + files:
                with contextlib.suppress(OSError):
                    newest = max(newest, min(os.lstat(os.path.join(root, n)).st_mtime, now))
    except OSError:
        pass
    return newest


def legacy_report(cache_root: Path, *, fix: bool = False, min_age_s: float = 86400, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    rows = []
    for p in legacy_candidates(cache_root):
        size = dir_size(p) if p.is_dir() and not p.is_symlink() else (p.lstat().st_size if p.exists() or p.is_symlink() else 0)
        age = now - _newest_epoch(p)
        busy = _in_use(p) if p.is_dir() else False
        keep = any(rx.search(p.name) for rx in REPORT_ONLY)
        row = {"path": str(p), "bytes": size, "age_s": age, "in_use": busy, "removed": False, "report_only": keep}
        if fix and age >= min_age_s and not busy and not keep:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    p.unlink()
            row["removed"] = not (p.exists() or p.is_symlink())
        rows.append(row)
    return rows


# --- security-scan-results retention -----------------------------------------------------------------------------

SCAN_KEEP = 7
SCAN_WARN_BYTES = 500 * 1024 ** 2
_SCAN_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T_ -]?(\d{2})[:-]?(\d{2})(?:[:-]?(\d{2}))?)?(?:-(.+?))?(\.out)?$")


def scan_results_dir(env=None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("OPENCLAW_SCAN_RESULTS_DIR") or Path(env.get("HOME") or Path.home()) / ".openclaw" / "security-scan-results")


def _scan_key(name: str):
    """(run name, sort key) from the timestamp in the dir name; sidecar `.out` files belong to their run."""
    m = _SCAN_NAME.match(name)
    if not m:
        return None
    date, hh, mm, ss, kind, out = m.groups()
    try:
        _dt.datetime.strptime(f"{date} {hh or '00'}:{mm or '00'}:{ss or '00'}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None                            # 2026-13-45-basic is not a dated run: left alone, never sorted as one
    run = name[:-4] if out else name
    return run, (date, hh or "00", mm or "00", ss or "00", kind or "")


def prune_scan_results(root: Path | None = None, keep: int = SCAN_KEEP, dry_run: bool = False, env=None) -> dict:
    """Keep the newest `keep` runs (ordered by the timestamp in the name, not by mtime); remove the rest and their
    `.out` sidecars. Entries whose name carries no timestamp are left alone."""
    root = root or scan_results_dir(env)
    groups: dict[str, tuple] = {}
    members: dict[str, list[Path]] = {}
    if root.is_dir():
        for child in root.iterdir():
            k = _scan_key(child.name)
            if k is None or child.is_symlink():
                continue
            run_name, order = k
            groups[run_name] = order
            members.setdefault(run_name, []).append(child)
    ordered = sorted(groups, key=lambda r: groups[r], reverse=True)
    drop = ordered[keep:]
    removed = []
    for r in drop:
        for p in members[r]:
            size = dir_size(p) if p.is_dir() else p.lstat().st_size
            if not dry_run:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    with contextlib.suppress(OSError):
                        p.unlink()
            removed.append((str(p), size))
    return {"kept": ordered[:keep], "removed": removed, "dry_run": dry_run, "root": str(root)}


def scan_results_bytes(root: Path | None = None, env=None) -> int:
    root = root or scan_results_dir(env)
    return dir_size(root) if root.is_dir() else 0


# --- CLI ---------------------------------------------------------------------------------------------------------

def _fmt(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return str(n)


def _cmd_new(a) -> int:
    # A pid bounds the run only when the caller names a long-lived one (a script passes its own $$). Otherwise a lease:
    # an agent's per-call shell or this very CLI exits long before the work does.
    lease = parse_duration(a.lease) if a.lease else (None if a.owner_pid else DEFAULT_LEASE_S)
    run_dir = new(a.role, a.slug, keep_on_fail=not a.no_keep_on_fail, owner_pid=a.owner_pid or None,
                  lease_s=lease)
    if a.shell:
        # eval-able: the caller's trap runs `scratch done`.
        print(f"export {ENV_SCRATCH}={_q(str(run_dir))}\nexport TMPDIR={_q(str(run_dir / 'tmp'))}")
    else:
        print(run_dir)
    return 0


def _q(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _cmd_done(a) -> int:
    try:
        print(finish(a.path, a.status == 0))
    except ValueError as exc:
        print(f"scratch done: {exc}", file=sys.stderr)
        return 2
    return 0


def _cmd_rm(a) -> int:
    try:
        return 0 if rm(a.path) else 1
    except ValueError as exc:
        print(f"scratch rm: {exc}", file=sys.stderr)
        return 2


def _cmd_ls(a) -> int:
    rows = entries()
    if not rows:
        print("no scratch runs")
        return 0
    now = time.time()
    for e in rows:
        age = "?" if e["created"] is None else f"{(now - e['created']) / 3600:.1f}h"
        print(f"{e['path'].name}  {e['status']:8}  {'live' if e['alive'] else 'dead':4}  {age:>7}  {_fmt(e['size'])}")
    print(f"total {_fmt(sum(e['size'] for e in rows))} in {len(rows)} run(s) under {runs_dir()}")
    return 0


def _cmd_prune(a) -> int:
    r = prune(older_than=parse_duration(a.older_than), cap_bytes=int(a.cap_gib * 1024 ** 3), dry_run=a.dry_run)
    verb = "would remove" if a.dry_run else "removed"
    if r["locked"]:
        print("another prune holds the lock; nothing done")
        return 0
    for path, reason, size in r["removed"]:
        print(f"{verb} {path}  ({reason}, {_fmt(size)})")
    for v in r["venvs_removed"]:
        print(f"{verb} {v}  (venv idle 30d+)")
    for c in r["converted"]:
        print(f"{'would convert' if a.dry_run else 'converted'} crashed run to logs-only: {c}")
    print(f"{verb} {len(r['removed'])} run(s); kept {r['kept']}, live {r['skipped_live']}"
          + (" [dry-run]" if a.dry_run else ""))
    return 0


def _cmd_prune_scans(a) -> int:
    r = prune_scan_results(Path(a.root) if a.root else None, keep=a.keep, dry_run=a.dry_run)
    verb = "would remove" if a.dry_run else "removed"
    for path, size in r["removed"]:
        print(f"{verb} {path}  ({_fmt(size)})")
    print(f"kept {len(r['kept'])} run(s) in {r['root']}" + (" [dry-run]" if a.dry_run else ""))
    return 0


def _cmd_venv(a) -> int:
    print(ensure_venv(a.lock, python=a.python, install=not a.no_install))
    return 0


def add_subparser(sub) -> None:
    p = sub.add_parser("scratch", help="The single scratch root: create, remove, list and prune run directories")
    s = p.add_subparsers(dest="scratch_command", metavar="ACTION")
    n = s.add_parser("new", help="Create a run dir; prints its path (or export lines with --shell)")
    n.add_argument("--role", required=True)
    n.add_argument("--slug", default="")
    n.add_argument("--no-keep-on-fail", action="store_true", help="a failure removes the whole run, logs too")
    n.add_argument("--shell", action="store_true", help="print export lines for eval")
    n.add_argument("--owner-pid", type=int, default=0, help="a long-lived pid (your script's $$) whose life bounds the run")
    n.add_argument("--lease", default="", help="keep the run live this long without an owner pid (default 6h when no --owner-pid)")
    n.set_defaults(func=_cmd_new)
    d = s.add_parser("done", help="Finish a run: status 0 removes it, anything else keeps logs/ for 3 days")
    d.add_argument("path")
    d.add_argument("--status", type=int, default=0)
    d.set_defaults(func=_cmd_done)
    r = s.add_parser("rm", help="Remove one run dir (must sit directly under the runs root)")
    r.add_argument("path")
    r.set_defaults(func=_cmd_rm)
    s.add_parser("ls", help="List runs with status, liveness, age and size").set_defaults(func=_cmd_ls)
    pr = s.add_parser("prune", help="Remove expired runs and idle venvs")
    pr.add_argument("--older-than", default="7d", help="age after which a dead run goes (default 7d)")
    pr.add_argument("--cap-gib", type=float, default=DEFAULT_CAP_BYTES / 1024 ** 3, help="global size cap (default 5)")
    pr.add_argument("--dry-run", action="store_true")
    pr.set_defaults(func=_cmd_prune)
    ps = s.add_parser("prune-scans", help="Keep the newest 7 security-scan-results runs")
    ps.add_argument("--root", default="")
    ps.add_argument("--keep", type=int, default=SCAN_KEEP)
    ps.add_argument("--dry-run", action="store_true")
    ps.set_defaults(func=_cmd_prune_scans)
    v = s.add_parser("venv", help="Print (creating on first use) the shared venv for a python + lockfile hash")
    v.add_argument("--lock", action="append", required=True, help="lockfile or requirements file (repeatable)")
    v.add_argument("--python", default=None)
    v.add_argument("--no-install", action="store_true")
    v.set_defaults(func=_cmd_venv)
    p.set_defaults(func=lambda a: (p.print_help(), 1)[1])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ai-resources-scratch")
    sub = ap.add_subparsers()
    add_subparser(sub)
    args = ap.parse_args(["scratch", *(sys.argv[1:] if argv is None else argv)])
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())

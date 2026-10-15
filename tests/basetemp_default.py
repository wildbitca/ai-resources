"""Default pytest basetemp outside /tmp.

A bare `pytest tests/` writes its temp tree (hundreds of MiB per run) to /tmp/pytest-of-<user>, a quota'd
tmpfs that already broke OpenClaw once. When nothing chose a location, the run gets its own directory under
`$XDG_CACHE_HOME/ai-resources-pytest-runs` (or `~/.cache/ai-resources-pytest-runs`), and stale run directories are pruned.
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

PRUNE_AFTER_SECONDS = 6 * 3600
_SYSTEM_TMP = ("/tmp", "/var/tmp")


def cache_root(env=None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CACHE_HOME") or str(Path(env.get("HOME") or Path.home()) / ".cache")
    return Path(base) / "ai-resources-pytest-runs"


def _tmpdir_is_explicit(env) -> bool:
    value = env.get("TMPDIR")
    if not value:
        return False
    resolved = os.path.realpath(value)
    return not any(resolved == p or resolved.startswith(p + os.sep) for p in _SYSTEM_TMP)


def run_dir(root: Path, pid: int | None = None, now: float | None = None) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(now if now is not None else time.time()))
    return root / f"run-{stamp}-{pid if pid is not None else os.getpid()}-{os.urandom(3).hex()}"


def prune(root: Path, max_age: float = PRUNE_AFTER_SECONDS, now: float | None = None) -> list[Path]:
    """Remove `run-*` directories directly under root whose mtime is older than max_age."""
    cutoff = (now if now is not None else time.time()) - max_age
    removed = []
    if not root.is_dir():
        return removed
    for child in root.iterdir():
        if not child.name.startswith("run-") or child.is_symlink() or not child.is_dir():
            continue
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed.append(child)
        except OSError:
            continue
    return removed


def apply_default(config, env=None) -> Path | None:
    """Set config.option.basetemp when nobody chose one. Returns the directory, or None when left alone."""
    env = os.environ if env is None else env
    if getattr(config.option, "basetemp", None):
        return None
    if env.get("PYTEST_DEBUG_TEMPROOT") or _tmpdir_is_explicit(env):
        return None
    root = cache_root(env)
    try:
        root.mkdir(parents=True, exist_ok=True)
        prune(root)
    except OSError:
        return None
    target = run_dir(root)
    config.option.basetemp = str(target)
    return target

"""Default pytest basetemp: inside the kit's one scratch root, never /tmp (ADR-0020).

A bare `pytest tests/` writes its temp tree (hundreds of MiB per run) to /tmp/pytest-of-<user>, a quota'd tmpfs that
already broke OpenClaw once. When nothing chose a location, the run gets a scratch run (`pytest-t-<stamp>-<pid>-<rand>`
under `~/.cache/ai-resources/runs`), its `pytest/` subdirectory is the basetemp, `AI_RESOURCES_SCRATCH` and `TMPDIR`
point into it for the process and its children, and the whole run is removed when the session ends green. A red or
interrupted session keeps only `logs/` for 3 days. Stale runs are pruned by `ai-resources scratch prune` (meta + pid
liveness, never mtime).

A caller that already owns a run (a role agent with `$AI_RESOURCES_SCRATCH`) gets `$AI_RESOURCES_SCRATCH/pytest` and
leaves the cleanup to its owner. An explicit TMPDIR outside /tmp or PYTEST_DEBUG_TEMPROOT is honoured as before.
"""
from __future__ import annotations

import atexit
import os
import signal
import sys
from pathlib import Path

_SYSTEM_TMP = ("/tmp", "/var/tmp")
_OK_EXIT = (0, 5)                      # 5: no tests collected, not a failure


def _under_system_tmp(value: str) -> bool:
    resolved = os.path.realpath(value)
    return any(resolved == p or resolved.startswith(p + os.sep) for p in _SYSTEM_TMP)


def _tmpdir_is_explicit(env) -> bool:
    value = env.get("TMPDIR")
    return bool(value) and not _under_system_tmp(value)


def _scratch():
    from ai_resources import scratch
    return scratch


def apply_default(config, env=None) -> Path | None:
    """Set config.option.basetemp when nobody chose one. Returns the directory, or None when left alone."""
    own_env = env is None
    env = os.environ if own_env else env
    if getattr(config.option, "basetemp", None):
        return None
    scratch = _scratch()
    inherited = env.get(scratch.ENV_SCRATCH)
    if inherited and Path(inherited).is_dir() and not _under_system_tmp(inherited):
        target = Path(inherited) / "pytest"
        target.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(target)
        return target
    if env.get("PYTEST_DEBUG_TEMPROOT") or _tmpdir_is_explicit(env):
        return None
    if _under_system_tmp(str(scratch.base_dir(env))):
        return None
    try:
        run = scratch.new("pytest", "t", env=env)
    except OSError:
        return None
    config.option.basetemp = str(run / "pytest")
    config._ai_scratch_run = run
    config._ai_scratch_env = env
    if own_env:
        os.environ.update(scratch.env_for(run))
        import tempfile
        tempfile.tempdir = None
        if _main_thread():
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    atexit.register(finish_default, config, False)
    return run / "pytest"


def _main_thread() -> bool:
    import threading
    return threading.current_thread() is threading.main_thread()


def finish_default(config, ok: bool | None = None) -> str | None:
    """Close the run this process created: green removes it, anything else keeps logs/ (3-day TTL). Idempotent."""
    run = getattr(config, "_ai_scratch_run", None)
    if run is None:
        return None
    env = getattr(config, "_ai_scratch_env", None)
    status = getattr(config, "_ai_exit", None)
    good = bool(ok) or (status in _OK_EXIT)
    config._ai_scratch_run = None
    scratch = _scratch()
    if not good and Path(run).is_dir():
        try:
            (Path(run) / "logs" / "pytest-exit.txt").write_text(f"pytest exit status: {status}\n")
        except OSError:
            pass
    try:
        return scratch.finish(run, good, env=env)
    except (ValueError, OSError):
        return None

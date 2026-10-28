"""Retry a config patch that OpenClaw refused because its state database is in offline maintenance (3.2.8, item 2).

`openclaw config patch` reads and writes the state database. While no gateway holds it (stopped, restarting, a drained
window of an earlier run, OpenClaw's own maintenance) the CLI answers "OpenClaw state at ... is undergoing offline
maintenance; retry when it finishes." Setup used to report that as an error and still say "Setup complete". Now:

* a refusal with that text, or a `watchdog.off` window held by someone else, is retried with a short backoff
  (2, 4, 8, 15, 30, 30 ... s; no single sleep above 30 s) until a total budget is spent (default 180 s;
  `AI_RESOURCES_PATCH_WAIT_S` or `setup --patch-wait SECONDS`);
* a patch still refused is kept in `exhausted()`; `settle()` moves it into the setup state (`config_patch_pending`),
  lists it among the skipped changes with the one-line command that finishes it, and returns `EXIT_PATCH_PENDING`
  so the run is INCOMPLETE and exits non-zero;
* `ai-resources openclaw apply-pending` and the next setup run apply the pending patches (`apply_pending_patches`).

Nothing here restarts or stops anything. The drained window of the kit itself (`retry=False`) is never waited on:
the window is ours and the gateway is down on purpose.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

from . import ui

DEFAULT_WAIT_S = 180.0
MAX_WAIT_S = 900.0             # a typo (--patch-wait 99999) must not hold a setup run for hours
STALE_PENDING_DAYS = 7
REPLAY_FLAGS = ("slack_cost", "retired")   # the only writer flags a recorded patch may carry back (never allow_restart)
BACKOFF_S = (2, 4, 8, 15, 30)
EXIT_PATCH_PENDING = 75        # EX_TEMPFAIL: the patch is recorded, not applied; re-run or apply-pending finishes it
PENDING_COMMAND = "ai-resources openclaw apply-pending"
MAINTENANCE_TEXT = ("undergoing offline maintenance", "retry when it finishes")

_WAIT_OVERRIDE: float | None = None
_EXHAUSTED: list[dict] = []
_RUN_STARTED: float | None = None      # the run-wide deadline starts at set_wait_budget or at the first send

# Seams for tests (a fake clock).
_sleep: Callable[[float], None] = time.sleep
_clock: Callable[[], float] = time.monotonic


def _marker_exists() -> bool:
    from .. import openclaw_host as host
    try:
        return Path(host.WATCHDOG_OFF).exists()
    except OSError:
        return False


def is_maintenance(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in MAINTENANCE_TEXT)


def set_wait_budget(seconds: float | None) -> None:
    """Set the run-wide wait budget (None = default/env) and start its clock. Negative values are refused."""
    global _WAIT_OVERRIDE, _RUN_STARTED
    if seconds is not None and float(seconds) < 0:
        raise ValueError("the patch wait budget cannot be negative")
    _WAIT_OVERRIDE = None if seconds is None else min(float(seconds), MAX_WAIT_S)
    _RUN_STARTED = _clock()


def wait_budget() -> float:
    if _WAIT_OVERRIDE is not None:
        return _WAIT_OVERRIDE
    raw = os.environ.get("AI_RESOURCES_PATCH_WAIT_S", "").strip()
    if raw:
        try:
            value = float(raw)
            if value >= 0:
                return min(value, MAX_WAIT_S)
        except ValueError:
            pass
    return DEFAULT_WAIT_S


def exhausted() -> list[dict]:
    return list(_EXHAUSTED)


def clear_exhausted() -> None:
    _EXHAUSTED.clear()


def _same(e: dict, patch: dict, replace_paths: list[str]) -> bool:
    return e["patch"] == patch and e["replace_paths"] == replace_paths


def _record(patch: dict, replace_paths: list[str], flags: dict[str, Any]) -> None:
    if not any(_same(e, patch, replace_paths) for e in _EXHAUSTED):
        _EXHAUSTED.append({"patch": patch, "replace_paths": replace_paths, "flags": dict(flags)})


def _backoff(attempt: int) -> float:
    return float(BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)])


def send(do: Callable[[], tuple[int, str]], *, patch: dict, replace_paths: list[str] | None = None,
         dry_run: bool = False, retry: bool = True, flags: dict[str, Any] | None = None) -> tuple[int, str]:
    """Run `do()` (one `openclaw config patch` call) and retry it while OpenClaw is in offline maintenance.

    `retry=False` is the kit's own drained window: one attempt, nothing recorded. The wait budget is ONE deadline for
    the whole run (every send draws on it; once spent a send makes one attempt and records). A dry run refused for the
    whole budget is recorded too: the real write behind it never happens, so the patch would be lost silently; it is
    de-duplicated against the real write of the same patch."""
    global _RUN_STARTED
    if not retry:
        return do()
    budget = wait_budget()
    if _RUN_STARTED is None:
        _RUN_STARTED = _clock()
    started = _RUN_STARTED
    attempt = 0
    paths = list(replace_paths or [])

    def wait(why: str) -> bool:
        left = budget - (_clock() - started)
        if left <= 0:
            return False
        step = min(_backoff(attempt), left)
        ui.detail(f"{why}; retrying in {step:.0f}s ({left:.0f}s of the {budget:.0f}s budget left)")
        _sleep(step)
        return True

    # A maintenance window somebody else holds (watchdog.off): do not even try until it closes or the budget is spent.
    while _marker_exists():
        if not wait("a maintenance window is open (watchdog.off)"):
            break
        attempt += 1
    while True:
        rc, out = do()
        if rc == 0:
            if not dry_run:
                _EXHAUSTED[:] = [e for e in _EXHAUSTED if not _same(e, patch, paths)]
            return rc, out
        if not is_maintenance(out):
            return rc, out
        if not wait("OpenClaw state is in offline maintenance"):
            _record(patch, paths, flags or {})
            return rc, out
        attempt += 1


def finish_command() -> str:
    return f"{PENDING_COMMAND} --yes  (or re-run `ai-resources setup`)"


def record_pending(o) -> int:
    """Move the patches the budget could not apply into the setup state. Returns how many are now pending."""
    from datetime import datetime, timezone
    known = [e.get("patch") for e in o.config_patch_pending]
    for e in _EXHAUSTED:
        if e["patch"] in known:
            continue
        o.config_patch_pending.append({**e, "source": "setup", "at": datetime.now(timezone.utc).isoformat()})
        known.append(e["patch"])
    return len(o.config_patch_pending)


def settle(o) -> int:
    """End of a setup run: 0 when every patch went through, else record the rest and return EXIT_PATCH_PENDING."""
    if not _EXHAUSTED:
        return 0
    n = len(_EXHAUSTED)
    record_pending(o)
    clear_exhausted()
    ui.record_skipped(f"{n} OpenClaw config patch(es)",
                      "OpenClaw's state stayed in offline maintenance for the whole wait budget",
                      f"{PENDING_COMMAND}")
    ui.error(f"setup is INCOMPLETE: {n} config patch(es) were refused (OpenClaw state in offline maintenance) and are "
             f"recorded as pending. Finish with: {finish_command()}")
    return EXIT_PATCH_PENDING


def _replay_flags(flags: object) -> dict[str, bool] | None:
    """The writer flags a recorded patch may be replayed with: only `slack_cost` / `retired`, and only real booleans.
    None when the entry sets both (two guards at once is never legitimate). The state file is operator-editable, so
    `allow_restart` / `reload_mode` or anything else in it is dropped, never forwarded."""
    flags = flags if isinstance(flags, dict) else {}
    kept = {k: True for k in REPLAY_FLAGS if flags.get(k) is True}
    return None if len(kept) > 1 else kept


def _age_days(at: object) -> float | None:
    from datetime import datetime, timezone
    try:
        when = datetime.fromisoformat(str(at))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 86400


def apply_pending_patches(o, apply_patch: Callable[..., tuple[bool, str]]) -> bool:
    """Apply the recorded patches (each through the retrying writer). True when none is left pending.

    The entries come from the state file, so they are validated on replay: allowlisted boolean flags only, an entry with
    both guards is refused, a patch that would now restart the gateway stays pending (never forced), and a very old
    entry is replayed only with a warning."""
    from ..openclaw_reload_rules import RestartRequired
    left: list[dict] = []
    for e in list(o.config_patch_pending):
        flags = _replay_flags(e.get("flags"))
        if flags is None or not isinstance(e.get("patch"), dict):
            ui.error("a pending config patch sets both guards (slack_cost and retired) or is malformed; left pending")
            left.append(e)
            continue
        age = _age_days(e.get("at"))
        if age is not None and age > STALE_PENDING_DAYS:
            ui.warn(f"a pending config patch is older than {STALE_PENDING_DAYS} days ({age:.0f} days old); "
                    "it is validated against the live config as usual before it is applied")
        try:
            ok, out = apply_patch(e["patch"], replace_paths=e.get("replace_paths") or None, **flags)
        except RestartRequired as ex:
            ui.warn(f"a pending config patch would restart the gateway ({ex}); left pending for "
                    "`ai-resources openclaw apply-pending` from outside the gateway")
            left.append(e)
            continue
        if ok:
            ui.ok("applied a pending OpenClaw config patch")
        else:
            ui.error(f"pending config patch still refused: {ui.safe_excerpt(out, 300, tail=True)}")
            left.append(e)
    o.config_patch_pending = left
    return not left

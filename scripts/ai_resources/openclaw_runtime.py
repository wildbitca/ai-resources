"""Is the gateway running on a runtime that no longer exists on disk? (pitfall T57)

Read-only. `brew upgrade node` deletes the old Cellar directory while the gateway keeps running from it; the process
then reports `/proc/<pid>/exe` as `<path> (deleted)` (or a path that is gone). Nothing here restarts, stops or signals
anything: it only names the drained path (`ai-resources openclaw doctor`) for the operator.
"""
from __future__ import annotations

import os
from typing import Callable

Runner = Callable[..., "tuple[int, str]"]

PROC_ROOT = "/proc"   # module-level so a test can point every caller at a fake tree
DELETED_SUFFIX = " (deleted)"
REMEDY = ("a drained restart is needed: run `ai-resources openclaw doctor` (it pauses the watchdog, drains the cgroup, "
          "repairs and restarts); never a bare `systemctl --user restart` (pitfall T57)")


def _main_pid(runner: Runner, unit: str, env: dict) -> str:
    rc, out = runner(["systemctl", "--user", "show", unit, "-p", "MainPID", "--value"], env=env)
    pid = out.strip() if rc == 0 else ""
    return pid if pid.isdigit() and int(pid) > 0 else ""


def gateway_exe_state(runner: Runner, unit: str, env: dict, *, proc_root: "str | None" = None) -> dict:
    """{state, pid, exe}: `state` is one of `ok`, `deleted`, `missing`, `no-pid` (unit not running) or `unknown`
    (the link could not be read, for example a permission error: never reported as a fault)."""
    pid = _main_pid(runner, unit, env)
    if not pid:
        return {"state": "no-pid", "pid": "", "exe": ""}
    try:
        exe = os.readlink(os.path.join(proc_root or PROC_ROOT, pid, "exe"))
    except OSError:
        return {"state": "unknown", "pid": pid, "exe": ""}
    if exe.endswith(DELETED_SUFFIX):
        return {"state": "deleted", "pid": pid, "exe": exe[:-len(DELETED_SUFFIX)]}
    if not os.path.exists(exe):
        return {"state": "missing", "pid": pid, "exe": exe}
    return {"state": "ok", "pid": pid, "exe": exe}


def runtime_findings(runner: Runner, unit: str, env: dict, *, proc_root: "str | None" = None) -> list[tuple[str, str, str]]:
    """(level, message, remedy) for `status` and `verify`: one warning when the gateway runs on a deleted binary, else []."""
    try:
        res = gateway_exe_state(runner, unit, env, proc_root=proc_root)
    except Exception:  # noqa: BLE001 - a broken probe must never take status or verify down
        return []
    if res["state"] not in ("deleted", "missing"):
        return []
    what = "was deleted" if res["state"] == "deleted" else "no longer exists"
    return [("warn", f"the gateway (pid {res['pid']}) runs on a runtime that {what}: {res['exe']}", REMEDY)]

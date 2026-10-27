"""Write-scope guard for concurrent runs (3.2.0 C1): a claim lock before the work, a verification after it.

No worktrees: every run writes in the checkout. What keeps two teams (or a team and an agy child) apart is
* `claim`: a run records the repo-relative paths it will write; a claim that overlaps another LIVE run's claim on the same
  workspace is refused (`roles_policy.scopes_overlap`), so nothing is overwritten and nothing waits on a lock;
* `violations`: the paths a finished step changed outside its claim, for logging (the scope contract is advisory
  verification, never a reason to distrust a backend).
The claims file is per workspace, updated under `flock` with an atomic replace. A claim is live while the caller says the run is
active (the engine knows which runs are not finished) or, for a one-shot caller, while its holder process lives (pid plus
start time, which defeats pid reuse). So an ad-hoc run id (`agy run --run team-a`) keeps its claim for as long as its
process runs, and a crashed holder never keeps it.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Callable, Iterable, Iterator

from . import roles_policy

ROOT = "."                              # the whole checkout: a run that may write anywhere


def claims_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "ai-resources" / "scopes"


def _overlap(a: str, b: str) -> bool:
    return ROOT in (a, b) or roles_policy.scopes_overlap(a, b)


@contextlib.contextmanager
def _locked(workspace: str, root: Path | None) -> Iterator[Path]:
    d = root or claims_dir()
    d.mkdir(parents=True, exist_ok=True)
    stem = hashlib.sha256(os.path.realpath(workspace).encode()).hexdigest()[:24]
    with open(d / f"{stem}.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield d / f"{stem}.json"


def _start_time(pid: int) -> str:
    """The process start time (`/proc/<pid>/stat` field 22) as text; '' when it cannot be read (not Linux, or gone)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return ""
    return stat[stat.rfind(")") + 2:].split()[19] if ")" in stat else ""


def _holder_alive(entry: dict) -> bool:
    pid = entry.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False                        # an old-format claim names no holder: only the caller's `live` keeps it
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:                         # EPERM: the process exists
        pass
    recorded = entry.get("start") or ""
    return not recorded or recorded == _start_time(pid)


def _read(path: Path) -> dict[str, dict]:
    """run id -> {"scopes": [...], "pid": int, "start": str}; the first format (a bare list of scopes) still reads."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict):
        return {}
    out: dict[str, dict] = {}
    for k, v in doc.items():
        if isinstance(v, list):
            v = {"scopes": v}
        if isinstance(v, dict) and isinstance(v.get("scopes"), list):
            out[str(k)] = {**v, "scopes": [str(x) for x in v["scopes"]]}
    return out


def _write(path: Path, claims: dict[str, dict]) -> None:
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(claims, sort_keys=True))
    os.replace(tmp, path)


def claim(workspace: str, run_id: str, scopes: Iterable[str], live: Callable[[str], bool],
          root: Path | None = None, holder: bool = False) -> list[str]:
    """Record `scopes` for `run_id`; return the conflicts (`<run>: <path>`), empty when the claim was taken.

    A claim is dropped first when `live(run_id)` reports the run finished and, for a claim made with `holder=True`, its
    process is gone too. `holder=True` is for a one-shot caller the engine does not know (`agy run`): the claim then
    records the calling pid and start time. An engine run records none: only `live` keeps its claim, so it ends with the
    run even when the process that claimed it lives on. A run claiming again replaces its own claim."""
    want = [s.strip("/") or ROOT for s in scopes]
    with _locked(workspace, root) as path:
        claims = {r: e for r, e in _read(path).items() if r == run_id or _holder_alive(e) or live(r)}
        conflicts = [f"{r}: {held}" for r, e in claims.items() if r != run_id
                     for held in e["scopes"] if any(_overlap(held, w) for w in want)]
        if not conflicts:
            claims[run_id] = {"scopes": want, **({"pid": os.getpid(), "start": _start_time(os.getpid())} if holder else {})}
        _write(path, claims)
        return conflicts


def held(workspace: str, run_id: str, root: Path | None = None) -> list[str]:
    """The scopes `run_id` currently claims on `workspace` (empty when it holds none)."""
    with _locked(workspace, root) as path:
        return _read(path).get(run_id, {}).get("scopes", [])


def release(workspace: str, run_id: str, root: Path | None = None) -> None:
    """Drop `run_id`'s claim: for a one-shot caller (`agy run`) that took it for a single step."""
    with _locked(workspace, root) as path:
        claims = _read(path)
        if claims.pop(run_id, None) is not None:
            _write(path, claims)


def violations(changed: Iterable[str], scopes: Iterable[str], run_id: str = "") -> list[str]:
    """The `changed` repo-relative paths outside the claim; the run's own `.agent-output/<run>/` is always allowed."""
    scopes = list(scopes)
    if ROOT in scopes:
        return []
    own = f".agent-output/{run_id}/" if run_id else ""
    return [p for p in changed if not roles_policy.path_in_scope(p, scopes) and not (own and p.startswith(own))]


def changed_paths(cwd: str) -> set[str] | None:
    """Repo-relative paths git reports as modified or untracked; None outside a git checkout."""
    try:
        p = subprocess.run(["git", "-C", cwd, "status", "--porcelain", "-uall", "--no-renames"], capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return {line[3:].strip().strip('"') for line in p.stdout.splitlines() if len(line) > 3} if p.returncode == 0 else None

"""Write-scope guard for concurrent runs (3.2.0 C1): a claim lock before the work, a verification after it.

No worktrees: every run writes in the checkout. What keeps two teams (or a team and an agy child) apart is
* `claim`: a run records the repo-relative paths it will write; a claim that overlaps another LIVE run's claim on the same
  workspace is refused (`roles_policy.scopes_overlap`), so nothing is overwritten and nothing waits on a lock;
* `violations`: the paths a finished step changed outside its claim, for logging (the scope contract is advisory
  verification, never a reason to distrust a backend).
The claims file is per workspace, updated under `flock` with an atomic replace. Liveness comes from the caller (the
engine knows which runs are not finished), so a crashed run never keeps its claim.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
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


def _read(path: Path) -> dict[str, list[str]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): [str(s) for s in v] for k, v in doc.items() if isinstance(v, list)} if isinstance(doc, dict) else {}


def claim(workspace: str, run_id: str, scopes: Iterable[str], live: Callable[[str], bool],
          root: Path | None = None) -> list[str]:
    """Record `scopes` for `run_id`; return the conflicts (`<run>: <path>`), empty when the claim was taken.

    Claims of runs that `live(run_id)` reports finished are dropped first. A run claiming again replaces its own claim."""
    want = [s.strip("/") or ROOT for s in scopes]
    with _locked(workspace, root) as path:
        claims = {r: s for r, s in _read(path).items() if r == run_id or live(r)}
        conflicts = [f"{r}: {held}" for r, held_scopes in claims.items() if r != run_id
                     for held in held_scopes if any(_overlap(held, w) for w in want)]
        if not conflicts:
            claims[run_id] = want
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(claims, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
        return conflicts


def violations(changed: Iterable[str], scopes: Iterable[str], run_id: str = "") -> list[str]:
    """The `changed` repo-relative paths outside the claim; the run's own `.agent-output/<run>/` is always allowed."""
    scopes = list(scopes)
    if ROOT in scopes:
        return []
    own = f".agent-output/{run_id}/" if run_id else ""
    return [p for p in changed if not roles_policy.path_in_scope(p, scopes) and not (own and p.startswith(own))]

"""Run state on disk: `<state home>/ai-resources/runs/<run id>/{state.json,handoff.md}` (ADR-0015 D3).

Atomic writes (temp file in the same directory, fsync, `os.replace`, mode 0600), one lock per run (`flock`), a run id that
cannot escape the runs directory, and a state home that never lives under a volatile root. Nothing here holds a secret: a
run records ids, statuses, counters, paths and short reasons, never a prompt body or a model reply.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

try:                                   # POSIX only; the kit targets Linux and macOS
    import fcntl
except ImportError:                    # pragma: no cover
    fcntl = None                       # type: ignore[assignment]

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
VOLATILE_ROOTS = ("/tmp", "/dev/shm", "/var/tmp")
MAX_EVENTS = 200
STATE_FILE = "state.json"
HANDOFF_FILE = "handoff.md"


class StateError(RuntimeError):
    """The run store cannot be used (bad id, unsafe location, corrupt file)."""


def runs_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    norm = os.path.realpath(base)
    for volatile in VOLATILE_ROOTS:
        if norm == volatile or norm.startswith(volatile + "/"):
            raise StateError(f"the runs directory never lives under {volatile} (tmpfs, emptied on reboot): {norm}")
    return Path(base) / "ai-resources" / "runs"


def new_run_id(label: str = "run", now: float | None = None) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now if now is not None else time.time()))
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:24] or "run"
    return f"{stamp}-{slug}-{secrets.token_hex(4)}"            # 32 bits: two teams started in one second never share an id


def check_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id) or ".." in run_id:
        raise StateError(f"invalid run id {run_id!r}")
    return run_id


class RunStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else runs_root()

    # -- paths --------------------------------------------------------------------------------------------------
    def dir(self, run_id: str) -> Path:
        return self.root / check_id(run_id)

    def state_path(self, run_id: str) -> Path:
        return self.dir(run_id) / STATE_FILE

    def handoff_path(self, run_id: str) -> Path:
        return self.dir(run_id) / HANDOFF_FILE

    # -- reads --------------------------------------------------------------------------------------------------
    def exists(self, run_id: str) -> bool:
        return self.state_path(run_id).is_file()

    def ids(self) -> list[str]:
        if not self.root.is_dir():
            return []
        return sorted(p.name for p in self.root.iterdir() if RUN_ID_RE.match(p.name) and (p / STATE_FILE).is_file())

    def load(self, run_id: str) -> dict[str, Any]:
        path = self.state_path(run_id)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise StateError(f"unknown run {run_id!r}") from None
        except (OSError, ValueError) as e:
            raise StateError(f"run {run_id!r} has an unreadable state file ({type(e).__name__})") from e
        if not isinstance(doc, dict) or doc.get("id") != run_id:
            raise StateError(f"run {run_id!r} has a state file that does not belong to it")
        return doc

    def all(self) -> list[dict[str, Any]]:
        out = []
        for rid in self.ids():
            try:
                out.append(self.load(rid))
            except StateError:
                continue
        return out

    # -- writes -------------------------------------------------------------------------------------------------
    def save(self, run: dict[str, Any]) -> None:
        run_id = check_id(str(run.get("id")))
        run["events"] = list(run.get("events") or [])[-MAX_EVENTS:]
        self._atomic(self.dir(run_id) / STATE_FILE, json.dumps(run, indent=1, sort_keys=True) + "\n")

    def write_handoff(self, run_id: str, text: str) -> None:
        self._atomic(self.dir(run_id) / HANDOFF_FILE, text)

    def _atomic(self, target: Path, text: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    @contextlib.contextmanager
    def locked(self, run_id: str) -> Iterator[None]:
        """Hold the run's lock for the block (blocking `flock`: the callers hold it for milliseconds)."""
        d = self.dir(run_id)
        d.mkdir(parents=True, exist_ok=True)
        fh = open(d / ".lock", "a+")
        try:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_EX)
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(fh, fcntl.LOCK_UN)
            finally:
                fh.close()

    @contextlib.contextmanager
    def global_lock(self) -> Iterator[None]:
        """The lock that serialises team admission (how many runs are active) across processes."""
        self.root.mkdir(parents=True, exist_ok=True)
        fh = open(self.root / ".admission.lock", "a+")
        try:
            if fcntl is not None:
                fcntl.flock(fh, fcntl.LOCK_EX)
            yield
        finally:
            try:
                if fcntl is not None:
                    fcntl.flock(fh, fcntl.LOCK_UN)
            finally:
                fh.close()

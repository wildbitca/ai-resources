"""ai-resources lanes — prepare | verify | integrate | land | count (ADR-0007 section 6 and 7).

The kit commands that make a lane step safe. Hooks do not fire for OpenClaw children, so these commands
are the control point on every lane (claude-cli and agy alike): they snapshot the run tree, give each
lane writer its own per-attempt worktree, check the step's write contract after the child ran, and are
the only code that merges a scope branch (`integrate`) or moves the run tree (`land`).

Verdict integrity: a hash-chained, append-only run ledger under `$XDG_STATE_HOME/ai-resources/lanes/<run>/`.
Every command prints `{nonce, snapshot_digest, ledger_head}`; the orchestrator passes the values back with
`--expect <nonce>:<digest>:<ledger_head>`. Authority comes from what the orchestrator witnessed: an entry
after the passed head carries none, and `verify` fails when the ledger gained entries during the child's
run from steps that were not declared `--in-flight`.

No destructive git command is ever issued against the run tree (no reset, no clean, no forced checkout);
a failed attempt is discarded by removing its own worktree through `openclaw worktrees remove`.

Exit codes: 0 ok / PASS, 1 refused / FAIL, 2 usage or environment error, 3 BLOCKED (merge conflict).
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from . import roles_policy as rp
from .scoring import routing

Runner = Callable[[list, "str | None"], "tuple[int, str, str]"]

GENESIS = "0" * 64
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")             # step ids: no '.', so a report name is unambiguous
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
REPORT_SUFFIXES = ("md", "txt", "json", "yaml", "yml", "log", "lane-verify.json")
UNTRACKED_XY = ("??", "!!")
MAX_ATTEMPTS = 3
RETRY_DELAY_S = 5.0
GIT = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
KIT_IDENT = ["-c", "user.name=ai-resources lanes", "-c", "user.email=lanes@ai-resources.invalid"]
HANDOFF_FILES = ("handoff.md", ".agent-output/handoff.md")
HUMAN_COPY_SUFFIX = ".lane-verify.json"      # the kit's own human copy of a verify result: no authority
EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_BLOCKED = 0, 1, 2, 3
VOLATILE_ROOTS = ("/tmp", "/dev/shm", "/var/tmp")
VERDICT_RE = re.compile(r"^[ \t>*_-]*\**Verdict\**[ \t]*:[ \t]*\**[ \t]*(PASS|FAIL|REQUIRES_CHANGES)\b", re.M)


class LaneCmdError(Exception):
    """A usage or environment error (exit 2)."""


class Refused(Exception):
    """The command refuses (exit 1); `reasons` are printed."""

    def __init__(self, reasons: Iterable[str], *, blocked: bool = False):
        self.reasons = list(reasons)
        self.blocked = blocked
        super().__init__("; ".join(self.reasons))


# ---------------------------------------------------------------------------------------------
# process runner


def default_runner(argv: list, cwd: str | None = None) -> tuple[int, str, str]:
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


class Ctx:
    def __init__(self, runner: Runner, repo: Path, run: str, state_root: Path):
        self.runner = runner
        self.repo = repo
        self.run = run
        self.run_dir = state_root / run
        self.ledger_path = self.run_dir / "ledger.jsonl"
        self.records_dir = self.run_dir / "records"

    def git(self, args: list, cwd: Path | str | None = None, *, ident: bool = False) -> str:
        argv = GIT + (KIT_IDENT if ident else []) + [str(a) for a in args]
        rc, out, err = self.runner(argv, str(cwd or self.repo))
        if rc != 0:
            raise LaneCmdError(f"git {' '.join(str(a) for a in args[:3])} failed: {err.strip() or out.strip()}")
        return out

    def git_rc(self, args: list, cwd: Path | str | None = None, *, ident: bool = False) -> tuple[int, str, str]:
        argv = GIT + (KIT_IDENT if ident else []) + [str(a) for a in args]
        return self.runner(argv, str(cwd or self.repo))


def state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    norm = os.path.realpath(base)               # a symlink into a volatile root is refused too
    for volatile in VOLATILE_ROOTS:
        if norm == volatile or norm.startswith(volatile + "/"):
            raise LaneCmdError(f"the lanes state directory never lives under {volatile} "
                               f"(tmpfs, emptied on reboot): {norm}")
    return Path(base) / "ai-resources" / "lanes"


# ---------------------------------------------------------------------------------------------
# ledger


def _sha(data: bytes | str) -> str:
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _entry_hash(entry: dict) -> str:
    return _sha(_canon({k: v for k, v in entry.items() if k != "hash"}))


def _read_nofollow(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


def _open_lock(path: Path):
    return os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), "a+")


def _refuse_links(*dirs: Path) -> None:
    """The run directory and `records/` are never followed through a symbolic link."""
    for d in dirs:
        try:
            st = os.lstat(d)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
            raise LaneCmdError(f"state path {d} is a link or not a directory: refused")


class Ledger:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx

    def exists(self) -> bool:
        _refuse_links(self.ctx.run_dir, self.ctx.records_dir)
        try:
            st = os.lstat(self.ctx.ledger_path)
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(st.st_mode):
            raise LaneCmdError(f"ledger {self.ctx.ledger_path} is a link or not a regular file: refused")
        return True

    def entries(self) -> list[dict]:
        """All entries, with the chain verified. Raises Refused when it is broken."""
        if not self.exists():
            return []
        out: list[dict] = []
        prev = GENESIS
        for n, line in enumerate(_read_nofollow(self.ctx.ledger_path).decode("utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                raise Refused([f"ledger entry {n} is not valid JSON: chain broken"])
            if entry.get("seq") != len(out) or entry.get("prev") != prev or entry.get("hash") != _entry_hash(entry):
                raise Refused([f"ledger chain broken at entry {n}"])
            prev = entry["hash"]
            out.append(entry)
        return out

    def head(self, entries: list[dict] | None = None) -> str:
        entries = self.entries() if entries is None else entries
        return entries[-1]["hash"] if entries else GENESIS

    def append(self, kind: str, step: str, *, role: str = "", iteration: int = 0, attempt: int = 0,
               record: dict | None = None, data: dict | None = None) -> dict:
        _refuse_links(self.ctx.run_dir)
        self.ctx.run_dir.mkdir(parents=True, exist_ok=True)
        _refuse_links(self.ctx.run_dir)
        self.ctx.records_dir.mkdir(exist_ok=True)
        _refuse_links(self.ctx.run_dir, self.ctx.records_dir)
        with _open_lock(self.ctx.run_dir / "ledger.lock") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            entries = self.entries()
            # the ledger keeps only the hash of the nonce: a child that reads it cannot present the secret
            secret = secrets.token_hex(16)
            entry: dict[str, Any] = {
                "seq": len(entries), "prev": self.head(entries), "kind": kind, "step": step, "role": role,
                "iteration": iteration, "attempt": attempt, "nonce_sha256": _sha(secret), "record": None,
                "record_sha256": None, "data": data or {},
            }
            if record is not None:
                name = f"{kind}-{step}-i{iteration}-a{attempt}-{entry['nonce_sha256'][:12]}.json"
                path = self.ctx.records_dir / name
                body = json.dumps(record, sort_keys=True, indent=1)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)   # write-once
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(body)
                entry["record"] = name
                entry["record_sha256"] = _sha(body)
            entry["hash"] = _entry_hash(entry)
            fd = os.open(self.ctx.ledger_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o644)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
            entry["nonce"] = secret                           # returned to the caller only, never persisted
            return entry

    def record_ok(self, entry: dict) -> bool:
        if not entry.get("record"):
            return False
        path = self.ctx.records_dir / entry["record"]
        try:
            return _sha(_read_nofollow(path)) == entry.get("record_sha256")
        except OSError:
            return False

    def load_record(self, entry: dict) -> dict:
        """The entry's record, read once and checked against the ledger's `record_sha256` (fail closed)."""
        name = entry.get("record")
        want = entry.get("record_sha256")
        if not name or not want:
            raise Refused([f"record of ledger entry {entry.get('seq')} has no sha256 in the ledger: refused"])
        try:
            raw = _read_nofollow(self.ctx.records_dir / name)
        except OSError:
            raise Refused([f"record {name} does not match its ledger hash"])
        if _sha(raw) != want:
            raise Refused([f"record {name} does not match its ledger hash"])
        return json.loads(raw.decode("utf-8"))

    def require_record(self, entry: dict) -> None:
        self.load_record(entry)


def parse_expect(value: str) -> tuple[str, str, str]:
    parts = (value or "").split(":")
    if len(parts) != 3 or not all(parts):
        raise LaneCmdError("--expect must be <nonce>:<snapshot_digest>:<ledger_head>")
    return parts[0], parts[1], parts[2]


def witnessed(entries: list[dict], head: str) -> list[dict]:
    """Entries the orchestrator witnessed (at or before `head`), minus tainted ones.

    Raises Refused when `head` is not part of the chain (a rewritten ledger or a forged head).
    """
    idx = next((i for i, e in enumerate(entries) if e["hash"] == head), None)
    if idx is None:
        if head == GENESIS and not entries:
            return []
        raise Refused(["ledger_head is not an ancestor of the current chain: ledger was rewritten"])
    upto = entries[: idx + 1]
    tainted = {s for e in upto if e["kind"] == "verify" for s in (e["data"].get("tainted_seqs") or ())}
    return [e for e in upto if e["seq"] not in tainted]


# ---------------------------------------------------------------------------------------------
# git observations


def _hash_path(path: Path) -> str:
    try:
        if path.is_symlink():
            return "link:" + os.readlink(path)
        if path.is_file():
            return _sha(path.read_bytes())
    except OSError:
        pass
    return "absent"


def _hash_tree(path: Path) -> str:
    if not path.exists():
        return "absent"
    if path.is_file() or path.is_symlink():
        return _hash_path(path)
    h = hashlib.sha256()
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for d in dirs:                      # a symlinked directory is not walked: its target is the content
            p = Path(root) / d
            if p.is_symlink():
                h.update(str(p.relative_to(path)).encode())
                h.update(_hash_path(p).encode())
        for f in sorted(files):
            p = Path(root) / f
            h.update(str(p.relative_to(path)).encode())
            h.update(_hash_path(p).encode())
    return h.hexdigest()


def _stat_tree(path: Path) -> str:
    """A directory of binaries (`~/.local/bin/`): name, size, mtime and link target, never the content."""
    if not path.exists() and not path.is_symlink():
        return "absent"
    h = hashlib.sha256()
    for root, dirs, files in os.walk(path):
        dirs.sort()
        for f in sorted(files + [d for d in dirs if (Path(root) / d).is_symlink()]):
            p = Path(root) / f
            try:
                st = p.lstat()
                target = os.readlink(p) if p.is_symlink() else ""
            except OSError:
                continue
            h.update(f"{p.relative_to(path)}\0{st.st_size}\0{st.st_mtime_ns}\0{target}\n".encode())
    return h.hexdigest()


def dirty_map(ctx: Ctx, tree: Path) -> dict[str, str]:
    """path -> `<XY>:<content sha>` for every modified, staged or untracked path, plus ignored forbidden-root files."""
    out = ctx.git(["status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=tree)
    fields = out.split("\0")
    result: dict[str, str] = {}
    i = 0
    while i < len(fields):
        f = fields[i]
        i += 1
        if len(f) < 4:
            continue
        xy, path = f[:2], f[3:]
        if "R" in xy or "C" in xy:
            i += 1                                        # the original path
        result[path] = f"{xy}:{_hash_path(tree / path)}"
    # ignored files are invisible to `git status`; the ones under a forbidden root are not noise
    ign = ctx.git(["ls-files", "-z", "--others", "--ignored", "--exclude-standard"], cwd=tree)
    for path in ign.split("\0"):
        if path and path not in result and _forbidden_root(path) is not None:
            result[path] = f"!!:{_hash_path(tree / path)}"
    return result


def tracked_digest(ctx: Ctx, tree: Path) -> str:
    """The index and tracked content, excluding the handoff file and `.agent-output/`."""
    spec = ["--", ".", ":(exclude)handoff.md", ":(exclude).agent-output"]
    idx = ctx.git(["ls-files", "-s", "-z"] + spec, cwd=tree)
    diff = ctx.git(["diff", "HEAD", "--binary", "--no-ext-diff"] + spec, cwd=tree)
    return _sha(idx + "\0" + diff)


def refs_map(ctx: Ctx, tree: Path) -> dict[str, str]:
    out = ctx.git(["for-each-ref", "--format=%(refname) %(objectname)"], cwd=tree)
    return dict(line.split(" ", 1) for line in out.splitlines() if " " in line)


def _common_dir(ctx: Ctx, tree: Path) -> Path:
    out = ctx.git(["rev-parse", "--git-common-dir"], cwd=tree).strip()
    p = Path(out)
    return p if p.is_absolute() else (tree / p).resolve()


def _tripwires() -> dict[str, str]:
    out = {}
    for p in rp.TRIPWIRE_PATHS:
        path = Path(os.path.expanduser(p))
        if not p.startswith("~") and not path.is_absolute():
            from . import repo_root
            path = Path(repo_root()) / p          # a kit file (the evidence record), not a host file
        out[p] = _hash_tree(path)
    for p in rp.TRIPWIRE_STAT_PATHS:
        out[p] = _stat_tree(Path(os.path.expanduser(p)))
    if rp.OPENCLAW_CRON_STORE_PATH:
        out[rp.OPENCLAW_CRON_STORE_PATH] = _hash_tree(Path(os.path.expanduser(rp.OPENCLAW_CRON_STORE_PATH)))
    return out


# Command snapshots (symmetric, every lane): `crontab -l` and the user timer list, taken at prepare and at
# verify with a fixed environment and a 5 s timeout. A change fails verify; "unavailable" at both ends does not.
SNAPSHOT_RUNNER: Callable[[list], tuple[int, str, str]] | None = None
UNAVAILABLE = "unavailable"
SNAPSHOT_TIMEOUT_S = 5
SNAPSHOT_COMMANDS = {"crontab": ["crontab", "-l"],
                     "user-timers": ["systemctl", "--user", "list-timers", "--all", "--no-legend"]}
_UNIT_RE = re.compile(r"\b[\w@:.\\-]+\.(?:timer|service)\b")


def _snapshot_exec(argv: list) -> tuple[int, str, str]:
    if SNAPSHOT_RUNNER is not None:
        return SNAPSHOT_RUNNER(argv)
    env = {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "HOME": os.environ.get("HOME", "")}
    for key in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    try:
        p = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=SNAPSHOT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return 127, "", ""
    return p.returncode, p.stdout, p.stderr


def _command_snapshots() -> dict[str, str]:
    out: dict[str, str] = {}
    rc, text, err = _snapshot_exec(SNAPSHOT_COMMANDS["crontab"])
    if rc == 0:
        out["crontab"] = _sha(text)
    elif rc == 1 and "no crontab" in (err or text).lower():
        out["crontab"] = _sha("")                       # no crontab: empty
    else:
        out["crontab"] = UNAVAILABLE
    rc, text, _ = _snapshot_exec(SNAPSHOT_COMMANDS["user-timers"])
    if rc == 0:
        # unit names only, sorted: the next-run column moves whenever an operator timer fires, which would fail
        # every in-flight step; a new or removed unit is what a lane child can cause
        out["user-timers"] = _sha("\n".join(sorted(set(_UNIT_RE.findall(text)))))
    else:
        out["user-timers"] = UNAVAILABLE
    return out


def agent_outputs(tree: Path) -> dict[str, str]:
    root = tree / ".agent-output"
    out: dict[str, str] = {}
    if root.is_dir():
        for p in sorted(root.rglob("*")):
            if (p.is_file() or p.is_symlink()):
                out[str(p.relative_to(tree))] = _hash_path(p)
    return out


def snapshot(ctx: Ctx, tree: Path) -> dict:
    common = _common_dir(ctx, tree)
    hooks_cfg = ctx.git_rc(["config", "--get", "core.hooksPath"], cwd=tree)[1].strip()
    return {
        "head": ctx.git(["rev-parse", "HEAD"], cwd=tree).strip(),
        "tracked": tracked_digest(ctx, tree),
        "refs": refs_map(ctx, tree),
        "config": _hash_path(common / "config"),
        "hooks": _hash_tree(common / "hooks"),
        "info": _hash_tree(common / "info"),
        "hooks_path": hooks_cfg,
        "gitmodules": _hash_path(tree / ".gitmodules"),
        "gitattributes": _hash_path(tree / ".gitattributes"),
        "dirty": dirty_map(ctx, tree),
        "agent_outputs": agent_outputs(tree),
        "handoff": {h: _hash_path(tree / h) for h in HANDOFF_FILES},
        "tripwires": _tripwires(),
        "commands": _command_snapshots(),
    }


# ---------------------------------------------------------------------------------------------
# path rules


def _report_re(role: str, step: str) -> re.Pattern:
    """The step's own report: `<step>.<known suffix>` or anything below `<step>/`; step ids carry no '.'."""
    suffixes = "|".join(re.escape(x) for x in REPORT_SUFFIXES)
    return re.compile(rf"^\.agent-output/{re.escape(role)}/{re.escape(step)}(?:\.(?:{suffixes})|/.+)$")


def tolerated(path: str) -> bool:
    parts = path.split("/")
    for t in rp.TOLERANCE_LIST:
        if t.endswith("/"):
            if t[:-1] in parts:
                return True
        elif path == t:
            return True
    return False


def _forbidden_root(path: str) -> str | None:
    """The forbidden root `path` sits in, at any depth: `pkg/.claude/` is as forbidden as `.claude/`."""
    parts = path.split("/")
    for f in rp.LANE_WRITE_FORBIDDEN:
        if f.rstrip("/") in parts:
            return f
    return None


def forbidden_path(path: str, own_report: re.Pattern) -> str | None:
    if path in HANDOFF_FILES:
        return "the handoff file"
    if own_report.match(path):
        return None
    if path.startswith(".agent-output/") or path == ".agent-output":
        return "another step's .agent-output file"
    root = _forbidden_root(path)
    return f"forbidden path {root}" if root else None


def _in_rev(ctx: Ctx, tree: Path, rev: str, path: str) -> bool:
    return ctx.git_rc(["cat-file", "-e", f"{rev}:{path}"], cwd=tree)[0] == 0


def _tolerated_untracked(ctx: Ctx, tree: Path, rev: str, path: str, xy: str) -> bool:
    """The tolerance list covers only untracked or ignored bootstrap/cache paths absent from the tracked tree."""
    return tolerated(path) and xy in UNTRACKED_XY and not _in_rev(ctx, tree, rev, path)


def _scope_paths_for(contract: str, role: str, step: str, scope: list[str], paths: dict[str, str],
                     *, own_report: re.Pattern) -> list[str]:
    """Reasons a set of changed paths violates the contract. `paths` maps path -> mode info or ''."""
    reasons: list[str] = []
    for path in sorted(paths):
        if own_report.match(path):
            continue
        bad = forbidden_path(path, own_report)
        if not bad and paths[path] == "untracked" and tolerated(path):
            continue                                      # tolerance covers only uncommitted noise
        if bad:
            reasons.append(f"{path}: {bad}")
            continue
        if contract == rp.REPORT:
            reasons.append(f"{path}: a report step may write only .agent-output/{role}/{step}*")
        elif contract == rp.SCOPE and not rp.path_in_scope(path, scope):
            reasons.append(f"{path}: outside the write scope {scope}")
    return reasons


def _check_modes(ctx: Ctx, tree: Path, base: str, tip: str, contract: str) -> list[str]:
    """New symlinks, submodules and exec-bit changes are rejected for scope and report contracts."""
    if contract == rp.INTEGRATING:
        return []
    out = ctx.git(["diff", "--raw", "-z", "--no-renames", f"{base}..{tip}"], cwd=tree)
    reasons = []
    fields = out.split("\0")
    i = 0
    while i + 1 < len(fields):
        meta, path = fields[i], fields[i + 1]
        i += 2
        m = re.match(r":(\d+) (\d+) \S+ \S+ ([A-Z])", meta)
        if not m:
            continue
        old, new = m.group(1), m.group(2)
        if new == "120000" or old == "120000":
            reasons.append(f"{path}: symlink change")
        elif new == "160000" or old == "160000":
            reasons.append(f"{path}: submodule change")
        elif new == "100755" and old != "100755":
            reasons.append(f"{path}: exec-bit change")
        elif old == "100755" and new not in ("100755", "000000"):
            reasons.append(f"{path}: exec-bit change")
    return reasons


def _check_messages(ctx: Ctx, tree: Path, base: str, tip: str) -> list[str]:
    out = ctx.git(["log", "--format=%H%x1f%B%x1e", f"{base}..{tip}"], cwd=tree)
    pats = [re.compile(p, re.I | re.M) for p in rp.ATTRIBUTION_PATTERNS]
    reasons = []
    for chunk in out.split("\x1e"):
        if "\x1f" not in chunk:
            continue
        sha, msg = chunk.strip().split("\x1f", 1)
        if any(p.search(msg) for p in pats):
            reasons.append(f"commit {sha[:12]}: carries an assistant attribution trailer or line")
    return reasons


# ---------------------------------------------------------------------------------------------
# helpers on ledger content


def _latest_prepare(auth: list[dict], step: str, iteration: int | None = None, attempt: int | None = None):
    cands = [e for e in auth if e["kind"] == "prepare" and e["step"] == step
             and (iteration is None or e["iteration"] == iteration)
             and (attempt is None or e["attempt"] == attempt)]
    if not cands:
        return None
    return max(cands, key=lambda e: (e["iteration"], e["attempt"], e["seq"]))


def _resolve(ledger: Ledger, entries: list[dict], step: str, expect: tuple[str, str, str], *,
             iteration: int | None = None, attempt: int | None = None) -> tuple[list[dict], dict]:
    """The witnessed entries and the step's prepare entry, bound to the orchestrator's `--expect`."""
    nonce, digest, head = expect
    auth = witnessed(entries, head)
    prep = _latest_prepare(auth, step, iteration, attempt)
    if prep is None:
        raise Refused(["no prepare entry for this step at or before the witnessed ledger_head: "
                       "run `lanes prepare` first"])
    if prep["nonce_sha256"] != _sha(nonce):
        raise Refused(["--expect nonce does not match the step's prepare entry"])
    ledger.require_record(prep)
    if prep["record_sha256"] != digest:
        raise Refused(["--expect snapshot_digest does not match the snapshot"])
    return auth, prep


def _roles_of(entries: list[dict], step: str) -> set[str]:
    return {e["role"] for e in entries if e["step"] == step and e["role"]}


def _verifier_pass(ledger: Ledger, auth: list[dict], subject_step: str, tip: str) -> dict | None:
    # the latest Verifier verify entry about this subject commit governs: a later FAIL revokes an earlier PASS
    latest = next((e for e in reversed(auth)
                   if e["kind"] == "verify" and e["role"] == "verifier"
                   and e["data"].get("subject_step") == subject_step and e["data"].get("subject_sha") == tip), None)
    for e in ([latest] if latest else []):
        if e["data"].get("result") != "PASS" or e["data"].get("verdict") != "PASS":
            continue
        if _roles_of(auth, e["step"]) != {"verifier"}:
            continue
        vfam = {p["data"].get("family") for p in auth if p["kind"] == "prepare" and p["step"] == e["step"]}
        sfam = {p["data"].get("family") for p in auth if p["kind"] == "prepare" and p["step"] == subject_step}
        if not vfam or not sfam or None in vfam or None in sfam or vfam & sfam:
            continue
        rec = ledger.load_record(e)                  # raises Refused when the record was rewritten
        if rec.get("subject_sha") == tip and rec.get("verdict") == "PASS":
            return e
    return None


def _latest_verify(auth: list[dict], step: str, iteration: int | None = None,
                   attempt: int | None = None) -> dict | None:
    """The latest verify entry of a step: it alone governs, a later FAIL revokes an earlier PASS."""
    return next((e for e in reversed(auth)
                 if e["kind"] == "verify" and e["step"] == step
                 and (iteration is None or e["iteration"] == iteration)
                 and (attempt is None or e["attempt"] == attempt)), None)


def _scope_pass(ledger: Ledger, auth: list[dict], step: str, tip: str) -> dict | None:
    e = _latest_verify(auth, step)
    if e is not None and e["data"].get("result") == "PASS" and e["data"].get("tip") == tip:
        ledger.require_record(e)
        return e
    return None


def _out(entry: dict | None, ledger: Ledger, extra: dict) -> dict:
    res = dict(extra)
    if entry is not None:
        if "nonce" in entry:
            res["nonce"] = entry["nonce"]
        rec = entry.get("record_sha256")
        res["snapshot_digest"] = rec or ""
        res["ledger_head"] = entry["hash"]
    else:
        res["ledger_head"] = ledger.head() if ledger.exists() else GENESIS
    return res


# ---------------------------------------------------------------------------------------------
# worktrees


def _wt_name(role: str, step: str, run: str, iteration: int, attempt: int) -> str:
    raw = f"kit-lane-{role}-{step}-{run}-i{iteration}-a{attempt}"
    return re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")


def create_worktree(ctx: Ctx, name: str, base: str) -> dict:
    argv = ["openclaw", "worktrees", "create", str(ctx.repo), "--name", name, "--base-ref", base, "--json"]
    last = ""
    for attempt in range(3):
        rc, out, err = ctx.runner(argv, None)
        if rc == 0:
            try:
                doc = json.loads(out)
            except ValueError:
                raise LaneCmdError("`openclaw worktrees create --json` did not print JSON")
            inner = doc.get("worktree") if isinstance(doc.get("worktree"), dict) else doc
            path = inner.get("path")
            branch = inner.get("branch") or f"openclaw/{name}"
            wid = inner.get("id") or inner.get("worktreeId") or name
            if not path:
                raise LaneCmdError("`openclaw worktrees create` printed no path")
            return {"id": str(wid), "path": str(path), "branch": str(branch), "name": name}
        last = (err or out).strip()
        if attempt < 2:
            time.sleep(RETRY_DELAY_S)          # V10: the first create after a gateway start may need a retry
    raise LaneCmdError(f"`openclaw worktrees create` failed: {last}")


def remove_worktree(ctx: Ctx, wid: str) -> None:
    rc, _, err = ctx.runner(["openclaw", "worktrees", "remove", wid], None)
    if rc != 0:
        raise LaneCmdError(f"`openclaw worktrees remove {wid}` failed: {err.strip()}")


def _removed_ids(entries: list[dict]) -> set[str]:
    return {e["data"].get("worktree_id") for e in entries if e["kind"] == "worktree-removed"}


def _remove_recorded(ctx: Ctx, ledger: Ledger, entries: list[dict], step: str, *, keep_seq: int | None = None) -> list[str]:
    done = _removed_ids(entries)
    removed = []
    for e in entries:
        if e["kind"] != "prepare" or e["step"] != step or e["seq"] == keep_seq:
            continue
        wid = e["data"].get("worktree_id")
        if wid and wid not in done:
            remove_worktree(ctx, wid)
            ledger.append("worktree-removed", step, role=e["role"], iteration=e["iteration"],
                          attempt=e["attempt"], data={"worktree_id": wid})
            done.add(wid)
            removed.append(wid)
    return removed


# ---------------------------------------------------------------------------------------------
# commands


def _check_id(value: str, what: str) -> str:
    if not (RUN_ID_RE if what == "run id" else ID_RE).match(value or ""):
        raise LaneCmdError(f"invalid {what} {value!r}")
    return value


def _tree(ctx: Ctx) -> Path:
    top = ctx.git(["rev-parse", "--show-toplevel"], cwd=ctx.repo).strip()
    return Path(top)


def _normalise_lane(lane: str) -> str:
    return f"openclaw-agent:{lane}" if lane.startswith("kit-lane-") else lane


def _admit_lane(doc: dict, lane: str, role: str = "") -> None:
    """Re-apply the hold and the admission when binding: the digest is integrity, not authentication, so a
    table whose digest was recomputed after adding an agent is still refused here."""
    from .setup import lanes_render
    from .setup.cockpits import openclaw_lanes
    if lane == "claude-code":
        return
    aid = lane.split(":", 1)[1] if lane.startswith("openclaw-agent:") else ""
    agent = (doc.get("agents") or {}).get(aid)
    if not aid.startswith("kit-lane-") or not isinstance(agent, dict):
        raise Refused([f"lane {lane} is not an admitted kit lane agent of the table"])
    backend = agent.get("backend")
    writable = backend in openclaw_lanes.writable_backends()
    if lanes_render.backend_held(backend or ""):
        writable = False
    if not writable:
        raise Refused([f"lane {lane}: agent is not admitted, backend {backend} is not writable in this version"])
    if agent.get("stale"):
        raise Refused([f"lane {lane}: agent is stale (model drift); re-run `ai-resources setup --lanes`"])
    if backend != "claude-cli":
        # An agy lane opens only with the flag, a Google acknowledgement listing the role and every phase 3 gate.
        # The table's digest is integrity, not authentication: a forged table that lists the agent is refused here.
        from . import agy_lane_gates
        google = ((doc.get("acks") or {}).get("google") or {})
        missing = []
        if not (doc.get("gates") or {}).get("antigravity"):
            missing.append("--lanes-antigravity is off in the table")
        if role not in (google.get("roles") or ()):
            missing.append(f"the Google acknowledgement does not list {role or 'the role'}")
        why = agy_lane_gates.refusal()
        if why:
            missing.append(why)
        if missing:
            raise Refused([f"lane {lane}: Antigravity lane closed: " + "; ".join(missing)])


def _check_model(role: str, lane: str, declared: str, entry: dict) -> None:
    """The declared --model must be the model the bound lane resolved to: same family and, when both are
    concrete refs, the same ref (vendor prefix ignored). An alias matches by family."""
    if "model" not in entry:
        raise Refused([f"lanes.yaml lacks the model of {lane}: re-run `ai-resources setup --lanes`"])
    resolved_model = entry.get("model")
    if not resolved_model:
        return                                   # the role has no model in by_role: nothing to compare with
    fam_d, fam_r = rp.model_family(declared), rp.model_family(str(resolved_model))
    strip = lambda r: re.sub(r"^(?:anthropic|claude-cli|claude-kit)/", "", str(r))      # noqa: E731
    concrete = lambda r: rp.model_family(r) is not None and strip(r) not in ("haiku", "sonnet", "opus")  # noqa: E731
    bad = fam_d is None or fam_d != fam_r
    if not bad and concrete(declared) and concrete(resolved_model) and strip(declared) != strip(resolved_model):
        bad = True
    if bad:
        raise Refused([f"--model {declared!r} is not the model of lane {lane} for role {role} "
                       f"(resolved: {resolved_model})"])


def _pool_of_lane(doc: dict, lane: str) -> str:
    """The quota pool a lane draws on (claude-code draws the claude subscription)."""
    if lane == "claude-code" or not lane.startswith("openclaw-agent:"):
        return "claude-subscription"
    agent = (doc.get("agents") or {}).get(lane.split(":", 1)[1]) or {}
    return str(agent.get("pool") or "")


def _agy_evidence(doc: dict, lane: str) -> dict:
    """The phase 3 gate statuses recorded in the ledger when the lane is an agy lane (not proven is a fact)."""
    agent = (doc.get("agents") or {}).get(lane.split(":", 1)[1] if lane.startswith("openclaw-agent:") else "") or {}
    if agent.get("backend") in (None, "claude-cli"):
        return {}
    from . import agy_lane_gates
    return {"agy_gates": agy_lane_gates.status_table()}


def _lane_binding(role: str, a: argparse.Namespace) -> dict | None:
    """Bind a prepare to the lane `lanes.yaml` resolved for the role (ADR-0007 §1, B1).

    None when no `lanes.yaml` exists (lanes are not enabled: `--lane` is optional). When it exists, `--lane`
    is mandatory and must be the resolved lane or one of its recorded fallbacks; the choice and its reason
    are written to the ledger. The file's digest must match its content (an edited table is refused).
    """
    from .setup import lanes_render, state as setup_state
    path = setup_state.lanes_path()
    if not path.is_file():
        if a.lane:
            _normalise_lane(a.lane)
        return None
    try:
        import yaml
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, ImportError, ValueError) as exc:
        raise LaneCmdError(f"cannot read {path}: {exc}")
    except Exception as exc:                                 # yaml.YAMLError
        raise LaneCmdError(f"cannot parse {path}: {exc}")
    if not isinstance(doc, dict) or doc.get("digest") != lanes_render.doc_digest(doc):
        raise Refused([f"{path} does not match its digest: re-run `ai-resources setup --lanes`"])
    resolved = doc.get("resolved")
    if not isinstance(resolved, dict) or resolved.get("error"):
        raise Refused([f"{path} carries no usable resolved table"
                       f"{': ' + str(resolved.get('error')) if isinstance(resolved, dict) else ''}"])
    if not a.lane:
        raise Refused([f"{path} exists: --lane <claude-code|openclaw-agent:<id>> is required "
                       f"(the lane `resolved:` names for role {role})"])
    lane = _normalise_lane(a.lane)
    blocked = (resolved.get("blocked") or {}).get(role)
    if blocked:
        raise Refused([f"role {role} is blocked: {blocked}"])
    _admit_lane(doc, lane, role)
    entry = (resolved.get("roles") or {}).get(role)
    if not entry:                                            # a role the table does not name runs on claude-code
        entry = {"lane": "claude-code", "reason": "role not in the table: claude-code"}
    fallbacks = list(entry.get("fallbacks") or [])
    table_sha = _sha(path.read_bytes())
    pool_of = lambda ref: _pool_of_lane(doc, ref)       # noqa: E731
    base = {"digest": doc["digest"], "table_sha256": table_sha, "resolved_pool": pool_of(str(entry.get("lane")))}
    if lane == entry.get("lane"):
        _check_model(role, lane, a.model, entry)
        return {"lane": lane, "source": "resolved", "reason": str(entry.get("reason", "")),
                "pool": pool_of(lane), **base, **_agy_evidence(doc, lane)}
    for fb in fallbacks:
        if fb.get("lane") == lane:
            _check_model(role, lane, a.model, fb)
            return {"lane": lane, "source": "fallback", "reason": str(fb.get("reason", "")),
                    "pool": pool_of(lane), **base, **_agy_evidence(doc, lane)}
    raise Refused([f"lane {lane} is neither the resolved lane ({entry.get('lane')}) nor a recorded fallback "
                   f"for role {role}"])


def _check_breaker(role: str, binding: dict, tripped: dict, run: str) -> None:
    """The quota circuit breaker (OQ-6): a lane on a tripped pool is refused for the rest of the run. A writer
    moves to the next admitted candidate (the recorded fallback, reason "pool exhausted"); a checker fails with
    the reason and never waits or moves. Symmetric on every pool."""
    pool, resolved_pool = binding.get("pool"), binding.get("resolved_pool")
    # `claude-code` is the orchestrator's own session, not a spawned child: the breaker never refuses it
    if pool in tripped and binding.get("lane") != "claude-code":
        e = tripped[pool]
        raise Refused([f"pool {pool} is exhausted for run {run} (breaker entry {e['seq']}: "
                       f"{e['data'].get('reason')}): lane {binding['lane']} is refused"
                       + ("; a checker fails with the reason and never waits" if role in rp.CHECKER_ROLES else
                          "; use the next admitted candidate")])
    if resolved_pool in tripped and binding.get("source") == "fallback":
        e = tripped[resolved_pool]
        if role in rp.CHECKER_ROLES:
            raise Refused([f"pool {resolved_pool} is exhausted for run {run} (breaker entry {e['seq']}): the "
                           f"checker {role} fails with the reason and never waits or moves"])
        binding["reason"] = f"pool exhausted: {resolved_pool}; " + str(binding.get("reason", ""))


def cmd_prepare(ctx: Ctx, a: argparse.Namespace) -> tuple[int, dict]:
    step, role, contract = _check_id(a.step, "step id"), a.role, a.contract
    if not rp.lane_eligible(role):
        raise Refused([f"role {role!r} is not lane-eligible: no write contract declared"])
    if contract not in rp.contracts_for(role):
        raise Refused([f"role {role} cannot hold the {contract} contract (allowed: "
                       f"{', '.join(rp.contracts_for(role))})"])
    scope = list(a.scope or [])
    if contract == rp.SCOPE:
        if not scope:
            raise Refused(["the scope contract needs --scope"])
        bad = [f"{p}: {', '.join(rp.scope_problems(p))}" for p in scope if rp.scope_problems(p)]
        if bad:
            raise Refused([f"invalid write scope: {b}" for b in bad])
    elif scope:
        raise Refused([f"--scope is only valid with the scope contract, not {contract}"])
    if a.attempt < 1:
        raise LaneCmdError("--attempt starts at 1")
    if a.attempt > MAX_ATTEMPTS:
        raise Refused([f"attempt {a.attempt} exceeds the limit of {MAX_ATTEMPTS} attempts (ADR-0005 section 6)"])
    binding = _lane_binding(role, a)
    family = rp.model_family(a.model)
    ledger = Ledger(ctx)
    entries = ledger.entries()
    # Authority (C5, under C2): with --expect-head only entries at or before the head the orchestrator witnessed
    # count; without it every entry is honoured (a forged breaker can only narrow the candidate set).
    authority = witnessed(entries, a.expect_head) if getattr(a, "expect_head", "") else entries
    tripped = {e["data"].get("pool"): e for e in authority if e["kind"] == "breaker"}
    pinned = next((e["data"].get("sha256") for e in authority if e["kind"] == "table"), None)
    if binding is not None:
        if pinned and pinned != binding["table_sha256"]:
            raise Refused([f"lanes.yaml changed during run {ctx.run}: re-run setup and start a new run"])
        _check_breaker(role, binding, tripped, ctx.run)
    if a.pool and a.pool in tripped:
        raise Refused([f"pool {a.pool} is exhausted for run {ctx.run} (breaker entry "
                       f"{tripped[a.pool]['seq']}): a lane on it is refused"])

    prev_iters = [e["iteration"] for e in entries if e["kind"] == "prepare" and e["step"] == step]
    last_iter = max(prev_iters) if prev_iters else 0
    iteration = last_iter + 1 if (a.reenter or last_iter == 0) else last_iter
    if any(e["kind"] == "prepare" and e["step"] == step and e["iteration"] == iteration
           and e["attempt"] == a.attempt for e in entries):
        raise Refused([f"step {step} attempt {a.attempt} already prepared in iteration {iteration}"])
    if contract == rp.INTEGRATING:
        others = sorted({e["role"] for e in entries if e["kind"] == "prepare"
                         and e["data"].get("contract") == rp.INTEGRATING and e["role"] != role})
        if others:
            raise Refused([f"this run already has an integrating writer ({', '.join(others)}): "
                           f"one integrating writer per run, not {role}"])
    other_roles = _roles_of(entries, step) - {role}
    if other_roles:
        raise Refused([f"step {step} was already prepared as role {sorted(other_roles)}"])

    # checker independence at dispatch (R7-5, C1): families, fail closed
    if role in rp.CHECKER_ROLES:
        if family is None:
            raise Refused([f"checker {role}: model {a.model!r} has an unknown family; independence "
                           f"cannot be shown (fail closed)"])
        for e in entries:
            if e["kind"] != "prepare" or e["role"] in rp.REPORT_ROLES:
                continue
            wref, wfam = e["data"].get("model"), e["data"].get("family")
            if wfam is None or wfam == family:
                raise Refused([f"checker {role} model {a.model!r} (family {family}) equals the family of writer "
                               f"step {e['step']} ({e['role']}, model {wref!r}, family {wfam})"])

    elif role not in rp.REPORT_ROLES:
        for e in entries:
            if e["kind"] != "prepare" or e["role"] not in rp.CHECKER_ROLES:
                continue
            cfam = e["data"].get("family")
            if family is None or cfam is None or cfam == family:
                raise Refused([f"writer {role} model {a.model!r} (family {family}) is not independent of "
                               f"checker step {e['step']} ({e['role']}, family {cfam}): fail closed"])

    tree = _tree(ctx)
    if contract in (rp.SCOPE, rp.INTEGRATING):
        porcelain = ctx.git(["status", "--porcelain=v1", "-z", "--untracked-files=no", "--", ".",
                             ":(exclude)handoff.md", ":(exclude).agent-output"], cwd=tree)
        if porcelain.strip("\0"):
            raise Refused(["the run tree has uncommitted tracked changes: commit a local checkpoint "
                           "before a scope or integrating step (OQ-2)"])

    pool_note = None
    if a.pool:
        used = sum(1 for e in entries if e["kind"] == "count" and e["data"].get("pool") == a.pool
                   and not e["data"].get("checker"))
        if role not in rp.CHECKER_ROLES and a.cap is not None and used >= a.cap:
            raise Refused([f"cap reached on pool {a.pool} ({used}/{a.cap})"])
        pool_note = {"pool": a.pool, "count": used + (0 if role in rp.CHECKER_ROLES else 1)}

    if a.reenter and last_iter:
        _remove_recorded(ctx, ledger, ledger.entries(), step)

    snap = snapshot(ctx, tree)
    data: dict[str, Any] = {"model": a.model, "family": family, "contract": contract, "scope": scope,
                            "base": snap["head"], "repo": str(tree)}
    if binding is not None:
        data["lane"] = binding
    wt = None
    if contract in (rp.SCOPE, rp.INTEGRATING):
        wt = create_worktree(ctx, _wt_name(role, step, ctx.run, iteration, a.attempt), snap["head"])
        data.update(worktree_id=wt["id"], worktree_path=wt["path"], branch=wt["branch"])
        snap["refs"] = refs_map(ctx, tree)           # include the new branch: it is the step's own
    entry = ledger.append("prepare", step, role=role, iteration=iteration, attempt=a.attempt,
                          record={"snapshot": snap, "step": step, "role": role, "iteration": iteration,
                                  "attempt": a.attempt}, data=data)
    if binding is not None and not pinned:
        # the run-pinned table hash (5.7): the first prepare of a run records it; later prepares refuse a change
        ledger.append("table", step, role=role, iteration=iteration, attempt=a.attempt,
                      data={"sha256": binding["table_sha256"], "digest": binding["digest"]})
    if pool_note is not None:
        ledger.append("count", step, role=role, iteration=iteration, attempt=a.attempt,
                      data={"pool": a.pool, "checker": role in rp.CHECKER_ROLES})
    final_head = ledger.head()
    out = {"result": "PREPARED", "step": step, "role": role, "contract": contract, "iteration": iteration,
           "attempt": a.attempt, "model": a.model, "family": family, "base": snap["head"],
           "worktree": wt["path"] if wt else None, "worktree_id": wt["id"] if wt else None,
           "branch": wt["branch"] if wt else None, "nonce": entry["nonce"],
           "snapshot_digest": entry["record_sha256"], "ledger_head": final_head, "pool": pool_note}
    return EXIT_OK, out


def _in_flight(a: argparse.Namespace) -> set[str]:
    raw = a.in_flight or []
    return {s for item in raw for s in re.split(r"[,\s]+", item) if s}


def _forbidden_run_tree_changes(ctx: Ctx, tree: Path, before: dict, after: dict, *, allowed_refs: set[str],
                                step: str, role: str, contract: str, own_report: re.Pattern,
                              peer_reports: list[re.Pattern] | None = None) -> list[str]:
    peers = peer_reports or []

    def own(p: str) -> bool:
        return bool(own_report.match(p)) or any(r.match(p) for r in peers)

    reasons: list[str] = []
    if after["head"] != before["head"]:
        reasons.append(f"the run tree HEAD moved ({before['head'][:12]} -> {after['head'][:12]})")
    if after["tracked"] != before["tracked"]:
        reasons.append("the run tree's index or tracked content changed")
    for ref in sorted(set(before["refs"]) | set(after["refs"])):
        if before["refs"].get(ref) != after["refs"].get(ref) and ref not in allowed_refs:
            reasons.append(f"ref {ref} changed")
    for key, what in (("config", "git config"), ("hooks", "git hooks"), ("info", "git info"),
                      ("hooks_path", "core.hooksPath"), ("gitmodules", ".gitmodules"),
                      ("gitattributes", ".gitattributes")):
        if before[key] != after[key]:
            reasons.append(f"{what} changed")
    for h in HANDOFF_FILES:
        if before["handoff"].get(h) != after["handoff"].get(h):
            reasons.append(f"the handoff file {h} changed")
    keys = set(before["agent_outputs"]) | set(after["agent_outputs"])
    for p in sorted(keys):
        if before["agent_outputs"].get(p) != after["agent_outputs"].get(p) and not own(p) \
                and p not in HANDOFF_FILES:
            reasons.append(f"{p}: another step's .agent-output file changed")
    for p in sorted(before["tripwires"]):
        if before["tripwires"][p] != after["tripwires"].get(p):
            reasons.append(f"tripwire file {p} changed")
    for key in sorted(before.get("commands") or {}):
        b, a_ = (before.get("commands") or {}).get(key), (after.get("commands") or {}).get(key)
        if b == UNAVAILABLE and a_ == UNAVAILABLE:
            continue
        if b != a_:
            what = " ".join(SNAPSHOT_COMMANDS.get(key, [key]))
            if UNAVAILABLE in (a_, b):
                reasons.append(f"tripwire command `{what}` became unavailable or reappeared during the step "
                               f"(fail closed)")
            else:
                reasons.append(f"tripwire command `{what}` output changed")
    # changed dirty paths in the run tree
    changed = {p for p in set(before["dirty"]) | set(after["dirty"])
               if before["dirty"].get(p) != after["dirty"].get(p)}
    for p in sorted(changed):
        xy = (after["dirty"].get(p) or before["dirty"].get(p) or ":").split(":", 1)[0]
        if own(p) or _tolerated_untracked(ctx, tree, before["head"], p, xy):
            continue
        if contract == rp.REPORT:
            reasons.append(f"{p}: a report step may write only .agent-output/{role}/{step}*")
        else:
            reasons.append(f"{p}: the run tree changed during a worktree step")
    return reasons


def _sealed_reports(ledger: Ledger, entries: list[dict], step: str, since_seq: int) -> dict | None:
    """The report hashes the step's latest verify (after `since_seq`) sealed, or None when it has none."""
    e = next((x for x in reversed(entries)
              if x["kind"] == "verify" and x["step"] == step and x["seq"] > since_seq), None)
    if e is None:
        return None
    return ledger.load_record(e).get("reports")


def _report_integrity(ledger: Ledger, entries: list[dict], step: str, prep: dict, before: dict, after: dict,
                      own_report: re.Pattern, peers: list[dict]) -> list[str]:
    """Reports are write-once: a verified report never changes, and a step never changes a peer's report.

    A peer's report may be created while it runs; it may not change against this step's prepare snapshot
    nor against the hashes the peer's own verify sealed.
    """
    reasons: list[str] = []
    ao_b, ao_a = before["agent_outputs"], after["agent_outputs"]
    paths = {p for p in set(ao_b) | set(ao_a) if not p.endswith(HUMAN_COPY_SUFFIX)}
    sealed = _sealed_reports(ledger, entries, step, prep["seq"])
    if sealed is not None:
        for p in sorted(x for x in paths if own_report.match(x)):
            if sealed.get(p) != ao_a.get(p):
                reasons.append(f"{p}: the report changed after this step's verify sealed it")
    for peer in peers:
        rx = _report_re(peer["role"], peer["step"])
        sealed = _sealed_reports(ledger, entries, peer["step"], peer["seq"])
        for p in sorted(x for x in paths if rx.match(x)):
            if sealed is not None:
                if sealed.get(p) != ao_a.get(p):
                    reasons.append(f"{p}: the report of step {peer['step']} changed after its verify sealed it")
            elif p in ao_b and ao_b[p] != ao_a.get(p):
                reasons.append(f"{p}: the report of step {peer['step']} changed since this step's prepare")
    return reasons


def _state_dir_reasons(ledger: Ledger, entries: list[dict]) -> list[str]:
    referenced = {e["record"] for e in entries if e.get("record")}
    reasons = []
    d = ledger.ctx.records_dir
    if d.is_dir():
        for p in sorted(d.iterdir()):
            if p.name not in referenced:
                reasons.append(f"state directory: unreferenced file {p.name}")
    return reasons


def _write_human_copy(tree: Path, role: str, step: str, text: str) -> None:
    """Write the no-authority human copy, never through a symlink (best effort)."""
    try:
        cur = tree
        for part in (".agent-output", role):
            cur = cur / part
            if cur.is_symlink():
                return
            if not cur.exists():
                cur.mkdir()
            elif not cur.is_dir():
                return
        target = cur / f"{step}{HUMAN_COPY_SUFFIX}"
        if target.is_symlink():
            return
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            if os.fstat(fh.fileno()).st_nlink != 1:       # a hardlink would write through to another file
                return
            fh.truncate(0)
            fh.write(text)
    except OSError:
        pass


def cmd_verify(ctx: Ctx, a: argparse.Namespace) -> tuple[int, dict]:
    step = _check_id(a.step, "step id")
    expect = parse_expect(a.expect)
    ledger = Ledger(ctx)
    entries = ledger.entries()
    auth, prep = _resolve(ledger, entries, step, expect, iteration=a.iteration, attempt=a.attempt)
    role, contract = prep["role"], prep["data"]["contract"]
    iteration = prep["iteration"]
    in_flight = _in_flight(a)
    own_report = _report_re(role, step)
    reasons: list[str] = []
    tainted: list[int] = []

    # C2: nothing may have been appended during the child's run by a step that is not declared in flight
    head_idx = next(i for i, e in enumerate(entries) if e["hash"] == expect[2])
    for e in entries[prep["seq"] + 1:]:
        if e["step"] != step and e["step"] not in in_flight:
            reasons.append(f"ledger written during the child's run: entry {e['seq']} ({e['kind']} {e['step']})")
            tainted.append(e["seq"])
    for e in entries[head_idx + 1:]:
        if e["step"] not in in_flight and e["seq"] not in tainted:
            reasons.append(f"ledger written during the child's run: entry {e['seq']} ({e['kind']} {e['step']})")
            tainted.append(e["seq"])
    reasons += _state_dir_reasons(ledger, entries)

    spawn_raw: bytes | None = None
    if getattr(a, "spawn_error", ""):
        sp = Path(a.spawn_error).resolve()
        roots = [Path(prep["data"]["repo"]).resolve(), ctx.run_dir.resolve()]
        roots += [Path(e["data"]["worktree_path"]).resolve() for e in entries if e["data"].get("worktree_path")]
        if any(sp == r or r in sp.parents for r in roots):
            raise Refused([f"--spawn-error {sp} lies inside the run tree, the run state or a lane worktree: it "
                           f"must be a file the orchestrator wrote outside them"])
        if not sp.is_file():
            raise Refused([f"--spawn-error {sp} is not a file"])
        spawn_raw = sp.read_bytes()[:65536]
    prep_rec = ledger.load_record(prep)
    before = prep_rec["snapshot"]
    tree = Path(prep["data"]["repo"])
    after = snapshot(ctx, tree)
    allowed_refs: set[str] = set()
    base = prep["data"]["base"]
    tip = None
    changed_paths: dict[str, str] = {}
    scope = prep["data"].get("scope") or []
    wt_path = prep["data"].get("worktree_path")
    branch = prep["data"].get("branch")
    if branch:
        allowed_refs.add(f"refs/heads/{branch}")
    for e in entries:
        if e["step"] in in_flight and e["kind"] == "prepare" and e["data"].get("branch"):
            allowed_refs.add(f"refs/heads/{e['data']['branch']}")
    peers: list[dict] = []
    for pstep in sorted(in_flight - {step}):
        pp = _latest_prepare(entries, pstep)
        if pp is not None and pp["role"]:
            peers.append(pp)
    peer_reports = [_report_re(e["role"], e["step"]) for e in peers]

    reasons += _forbidden_run_tree_changes(ctx, tree, before, after, allowed_refs=allowed_refs, step=step,
                                           role=role, contract=contract, own_report=own_report,
                                           peer_reports=peer_reports)
    reasons += _report_integrity(ledger, entries, step, prep, before, after, own_report, peers)

    if contract in (rp.SCOPE, rp.INTEGRATING):
        wt = Path(wt_path)
        rc, sym, _ = ctx.git_rc(["symbolic-ref", "-q", "HEAD"], cwd=wt)
        cur = sym.strip() if rc == 0 else ""
        if cur != f"refs/heads/{branch}":
            reasons.append(f"worktree HEAD is {cur or 'detached'}, expected refs/heads/{branch}")
        tip = ctx.git(["rev-parse", "HEAD"], cwd=wt).strip()
        rc, _, _ = ctx.git_rc(["merge-base", "--is-ancestor", base, tip], cwd=wt)
        if rc != 0:
            reasons.append(f"tip {tip[:12]} does not descend from the base {base[:12]}")
        committed = ctx.git(["diff", "--name-only", "-z", "--no-renames", f"{base}..{tip}"], cwd=wt)
        paths = {p: "" for p in committed.split("\0") if p}
        dirty = dirty_map(ctx, wt)
        for p, val in dirty.items():
            if p in paths:
                continue                                  # committed: subject to the contract, edited or not
            xy = val.split(":", 1)[0]
            untracked = xy in UNTRACKED_XY and not _in_rev(ctx, wt, base, p) and not _in_rev(ctx, wt, tip, p)
            paths[p] = "untracked" if untracked else "dirty"
        reasons += _scope_paths_for(contract, role, step, scope, paths, own_report=own_report)
        for p in dirty:
            if paths[p] != "untracked" or not tolerated(p):
                if not own_report.match(p):
                    reasons.append(f"{p}: uncommitted change in the worktree: commit it on the branch")
        for p_, how in sorted(paths.items()):
            if how == "" and p_.split("/")[-1] in rp.BOOTSTRAP_FILES:
                reasons.append(f"{p_}: a bootstrap file is tolerated only untracked; a commit adding it fails")
        reasons += _check_modes(ctx, wt, base, tip, contract)
        reasons += _check_messages(ctx, wt, base, tip)
        changed_paths = paths

    record: dict[str, Any] = {"step": step, "role": role, "contract": contract, "iteration": iteration,
                              "attempt": a.attempt, "tip": tip, "base": base, "changed": sorted(changed_paths),
                              "reports": {p: h for p, h in sorted(after["agent_outputs"].items())
                                          if own_report.match(p) and not p.endswith(HUMAN_COPY_SUFFIX)}}
    data: dict[str, Any] = {"contract": contract, "tip": tip}

    if role == "verifier":
        subject = a.subject or ""
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_-]*)@([0-9a-f]{40}|[0-9a-f]{64})$", subject)
        if not m:
            reasons.append("a verifier verify needs --subject <step-id>@<full commit sha> (40 or 64 hex, no abbreviation)")
        else:
            report = a.report or f".agent-output/{role}/{step}.md"
            if not own_report.match(report) or ".." in report.split("/"):
                reasons.append(f"report {report!r} is not the step-scoped path .agent-output/{role}/{step}*")
            else:
                rpath = tree / report
                if not rpath.is_file():
                    reasons.append(f"report {report} does not exist")
                else:
                    body = rpath.read_text(encoding="utf-8", errors="replace")
                    verdicts = set(VERDICT_RE.findall(body))
                    if len(verdicts) != 1:
                        reasons.append("the report must carry exactly one 'Verdict: PASS|FAIL|REQUIRES_CHANGES' line")
                        verdict = None
                    else:
                        verdict = verdicts.pop()
                    data["verdict"] = verdict
                    record["verdict"] = verdict
                    record["report_sha256"] = _sha(body.encode("utf-8"))
                    record["report_path"] = report
            data.update(subject_step=m.group(1), subject_sha=m.group(2))
            record.update(subject_step=m.group(1), subject_sha=m.group(2))

    if role in rp.CHECKER_ROLES and (in_flight - {step}):
        # the shared report directory cannot attribute a write before verify: a checker writes alone
        reasons.append(f"checker verified with steps in flight: {', '.join(sorted(in_flight - {step}))}")
    if data.get("verdict") in ("FAIL", "REQUIRES_CHANGES"):
        reasons.append(f"the Verifier verdict is {data['verdict']}")
    result = "PASS" if not reasons else "FAIL"
    data["result"] = result
    data["reasons"] = reasons
    if tainted:
        data["tainted_seqs"] = tainted
    record["result"], record["reasons"] = result, reasons
    entry = ledger.append("verify", step, role=role, iteration=iteration, attempt=a.attempt,
                          record=record, data=data)
    out = {"result": result, "reasons": reasons, "step": step, "role": role, "contract": contract,
           "iteration": iteration, "attempt": a.attempt, "tip": tip, "verdict": data.get("verdict"),
           "nonce": entry["nonce"], "snapshot_digest": entry["record_sha256"], "ledger_head": entry["hash"]}
    pool = str(((prep["data"].get("lane") or {}).get("pool")) or "")
    if spawn_raw is not None and pool:
        # the only input is what the orchestrator witnessed; the child's report and .agent-output are never read
        pattern = routing.classify_quota(spawn_raw.decode("utf-8", errors="replace"), pool)
        if pattern:
            b = ledger.append("breaker", step, role=role, iteration=iteration, attempt=a.attempt,
                              data={"pool": pool, "step": step, "attempt": a.attempt, "reason": "quota-exhausted",
                                    "pattern": pattern, "evidence_sha256": _sha(spawn_raw)})
            out["breaker"] = {"pool": pool, "seq": b["seq"], "evidence_sha256": _sha(spawn_raw)}
            out["ledger_head"] = b["hash"]
    if result == "FAIL" and contract in (rp.SCOPE, rp.INTEGRATING) and prep["data"].get("worktree_id"):
        wid = prep["data"]["worktree_id"]
        if wid not in _removed_ids(ledger.entries()):
            remove_worktree(ctx, wid)
            e2 = ledger.append("worktree-removed", step, role=role, iteration=iteration, attempt=a.attempt,
                               data={"worktree_id": wid})
            out["ledger_head"] = e2["hash"]
            out["worktree_removed"] = wid
    # a human-readable copy with no authority
    _write_human_copy(tree, role, step,
                      json.dumps({k: v for k, v in out.items() if k not in ("nonce",)}, indent=1, sort_keys=True))
    return (EXIT_OK if result == "PASS" else EXIT_FAIL), out


def cmd_integrate(ctx: Ctx, a: argparse.Namespace) -> tuple[int, dict]:
    step, src = _check_id(a.step, "step id"), _check_id(a.from_step, "scope step id")
    expect = parse_expect(a.expect)
    ledger = Ledger(ctx)
    entries = ledger.entries()
    auth, prep = _resolve(ledger, entries, step, expect, iteration=a.iteration)
    if prep["data"]["contract"] != rp.INTEGRATING:
        raise Refused([f"step {step} is not an integrating step"])
    sprep = _latest_prepare(auth, src)
    if sprep is None or sprep["data"]["contract"] != rp.SCOPE:
        raise Refused([f"{src} has no scope prepare entry before the witnessed head"])
    branch = sprep["data"]["branch"]
    tree = Path(prep["data"]["repo"])
    rc, out, err = ctx.git_rc(["rev-parse", f"refs/heads/{branch}"], cwd=tree)
    if rc != 0:
        raise Refused([f"scope branch {branch} does not exist"])
    tip = out.strip()
    reasons = []
    if _scope_pass(ledger, auth, src, tip) is None:
        reasons.append(f"no scope verify PASS for {src} at tip {tip[:12]}")
    if _verifier_pass(ledger, auth, src, tip) is None:
        reasons.append(f"no Verifier PASS bound to {src}@{tip[:12]}")
    if reasons:
        entry = ledger.append("refused", step, role=prep["role"], iteration=prep["iteration"],
                              attempt=prep["attempt"], data={"op": "integrate", "from": src, "reasons": reasons})
        return EXIT_FAIL, _out(entry, ledger, {"result": "REFUSED", "reasons": reasons})
    iwt = Path(prep["data"]["worktree_path"])
    msg = f"kit lanes: integrate {src} ({branch}@{tip[:12]}) into {prep['data']['branch']}"
    rc, mout, merr = ctx.git_rc(["merge", "--no-ff", "-m", msg, f"refs/heads/{branch}"], cwd=iwt, ident=True)
    if rc != 0:
        _, conflicted, _ = ctx.git_rc(["diff", "--name-only", "--diff-filter=U", "-z"], cwd=iwt)
        paths = [p for p in conflicted.split("\0") if p]
        ctx.git_rc(["merge", "--abort"], cwd=iwt)
        entry = ledger.append("integrate", step, role=prep["role"], iteration=prep["iteration"],
                              attempt=prep["attempt"],
                              data={"from": src, "result": "BLOCKED", "paths": paths, "scope_tip": tip})
        return EXIT_BLOCKED, _out(entry, ledger, {"result": "BLOCKED", "paths": paths,
                                                  "reasons": [f"merge conflict in {', '.join(paths) or '?'}"]})
    new_tip = ctx.git(["rev-parse", "HEAD"], cwd=iwt).strip()
    entry = ledger.append("integrate", step, role=prep["role"], iteration=prep["iteration"],
                          attempt=prep["attempt"],
                          data={"from": src, "result": "MERGED", "scope_tip": tip, "tip": new_tip})
    return EXIT_OK, _out(entry, ledger, {"result": "MERGED", "tip": new_tip, "from": src})


def cmd_land(ctx: Ctx, a: argparse.Namespace) -> tuple[int, dict]:
    step = _check_id(a.step, "step id")
    expect = parse_expect(a.expect)
    ledger = Ledger(ctx)
    entries = ledger.entries()
    with _open_lock(ctx.run_dir / "land.lock") as lock:           # lands within a run are serialised
        fcntl.flock(lock, fcntl.LOCK_EX)
        entries = ledger.entries()
        auth, prep = _resolve(ledger, entries, step, expect, iteration=a.iteration)
        reasons: list[str] = []
        if prep["data"]["contract"] != rp.INTEGRATING:
            raise Refused([f"step {step} is not an integrating step"])
        iteration = prep["iteration"]
        tree = Path(prep["data"]["repo"])
        branch = prep["data"]["branch"]
        rc, out, _ = ctx.git_rc(["rev-parse", f"refs/heads/{branch}"], cwd=tree)
        tip = out.strip() if rc == 0 else ""
        if not tip:
            reasons.append(f"integration branch {branch} does not exist")
        # no other land for this (step, iteration): this check reads every entry, not only witnessed ones
        if any(e["kind"] == "land" and e["step"] == step and e["iteration"] == iteration
               and e["data"].get("result") == "LANDED" for e in entries):
            reasons.append(f"step {step} already landed in iteration {iteration}")
        vpass = _latest_verify(auth, step, iteration, prep["attempt"])
        if vpass is not None:
            ledger.require_record(vpass)
            if vpass["data"].get("result") != "PASS":
                vpass = None
        if vpass is None:
            reasons.append(f"no integrating verify PASS for {step} in iteration {iteration}")
        elif vpass["data"].get("tip") != tip:
            reasons.append(f"the integration branch tip {tip[:12]} is not the verified tip "
                           f"{str(vpass['data'].get('tip'))[:12]}")
        elif _verifier_pass(ledger, auth, step, tip) is None:
            reasons.append(f"no Verifier PASS bound to {step}@{tip[:12]}")
        # fail-closed chain: every integrated scope step must still show verify PASS and a Verifier PASS
        for e in auth:
            if e["kind"] == "integrate" and e["step"] == step and e["iteration"] == iteration \
                    and e["data"].get("result") == "MERGED":
                st = e["data"]["scope_tip"]
                if _scope_pass(ledger, auth, e["data"]["from"], st) is None \
                        or _verifier_pass(ledger, auth, e["data"]["from"], st) is None:
                    reasons.append(f"integrated step {e['data']['from']} lacks its verify or Verifier PASS")
        head = ctx.git(["rev-parse", "HEAD"], cwd=tree).strip()
        base = prep["data"]["base"]
        if base != head:
            reasons.append(f"base {base} is not run HEAD {head}")
        before = ledger.load_record(prep)["snapshot"]
        if tracked_digest(ctx, tree) != before["tracked"]:
            reasons.append("the run tree's index or tracked content changed since the integrating prepare")
        if reasons:
            entry = ledger.append("refused", step, role=prep["role"], iteration=iteration, attempt=prep["attempt"],
                                  data={"op": "land", "reasons": reasons})
            return EXIT_FAIL, _out(entry, ledger, {"result": "REFUSED", "reasons": reasons})
        rc, mout, merr = ctx.git_rc(["merge", "--ff-only", f"refs/heads/{branch}"], cwd=tree, ident=True)
        if rc != 0:
            reasons = [f"git merge --ff-only failed: {merr.strip() or mout.strip()}"]
            entry = ledger.append("land", step, role=prep["role"], iteration=iteration, attempt=prep["attempt"],
                                  data={"result": "FAILED", "reasons": reasons})
            return EXIT_FAIL, _out(entry, ledger, {"result": "REFUSED", "reasons": reasons})
        new_head = ctx.git(["rev-parse", "HEAD"], cwd=tree).strip()
        entry = ledger.append("land", step, role=prep["role"], iteration=iteration, attempt=prep["attempt"],
                              data={"result": "LANDED", "tip": tip, "from_head": head, "head": new_head})
        removed: list[str] = []
        warnings: list[str] = []
        try:
            fresh = ledger.entries()
            removed += _remove_recorded(ctx, ledger, fresh, step, keep_seq=None)
            for e in fresh:
                if e["kind"] == "integrate" and e["step"] == step and e["iteration"] == iteration:
                    removed += _remove_recorded(ctx, ledger, ledger.entries(), e["data"]["from"])
        except (LaneCmdError, OSError) as exc:
            warnings.append(f"cleanup after land failed, a worktree may be left behind: {exc}")
            ledger.append("cleanup-failed", step, role=prep["role"], iteration=iteration, attempt=prep["attempt"],
                          data={"error": str(exc)})
        final = ledger.entries()[-1]
        res = {"result": "LANDED", "tip": tip, "head": new_head, "removed": removed,
               "snapshot_digest": entry["record_sha256"] or ""}
        if warnings:
            res["warnings"] = warnings
        return EXIT_OK, _out(final, ledger, res)


def cmd_count(ctx: Ctx, a: argparse.Namespace) -> tuple[int, dict]:
    ledger = Ledger(ctx)
    entries = ledger.entries()
    checker = a.role in rp.CHECKER_ROLES
    used = sum(1 for e in entries if e["kind"] == "count" and e["data"].get("pool") == a.pool
               and not e["data"].get("checker"))
    if (not checker) and a.cap is not None and used >= a.cap:
        entry = ledger.append("refused", a.step or "-", role=a.role or "", data={
            "op": "count", "pool": a.pool, "reasons": [f"cap reached on pool {a.pool} ({used}/{a.cap})"]})
        return EXIT_FAIL, _out(entry, ledger, {"result": "REFUSED", "pool": a.pool, "count": used, "cap": a.cap,
                                               "reasons": [f"cap reached on pool {a.pool} ({used}/{a.cap})"]})
    entry = ledger.append("count", a.step or "-", role=a.role or "", data={"pool": a.pool, "checker": checker})
    total = used + (0 if checker else 1)
    return EXIT_OK, _out(entry, ledger, {"result": "COUNTED", "pool": a.pool, "count": total,
                                         "checker_dispatches": sum(
                                             1 for e in ledger.entries() if e["kind"] == "count"
                                             and e["data"].get("pool") == a.pool and e["data"].get("checker")),
                                         "cap": a.cap})


# ---------------------------------------------------------------------------------------------
# CLI


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="ai-resources lanes", description="Lane step control (ADR-0007)")
    sub = ap.add_subparsers(dest="action", required=True)
    _populate(sub)
    return ap


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--run", required=True, help="Run id")
    p.add_argument("--repo", default="", help="The run tree (default: the current directory)")


def _populate(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("prepare", help="Snapshot the run tree and create the step's worktree")
    _common(p)
    p.add_argument("--step", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--contract", required=True, choices=list(rp.CONTRACTS))
    p.add_argument("--model", required=True, help="The model that will actually run the step")
    p.add_argument("--lane", default="", help="The lane the step runs on: claude-code or the lane agent id "
                                              "(required once lanes.yaml exists; must be the resolved lane "
                                              "or a recorded fallback)")
    p.add_argument("--attempt", type=int, default=1)
    p.add_argument("--reenter", action="store_true", help="Re-entering the step through on_concern_return_to")
    p.add_argument("--scope", nargs="*", default=[], help="write_scope paths (scope contract)")
    p.add_argument("--pool", default="", help="Count the dispatch against this pool")
    p.add_argument("--cap", type=int, default=None)
    p.add_argument("--expect-head", dest="expect_head", default="",
                   help="The ledger_head the orchestrator last witnessed: breaker and table entries after it "
                        "carry no authority")
    p.set_defaults(lanes_func=cmd_prepare)

    p = sub.add_parser("verify", help="Check the step's contract after the child ran")
    _common(p)
    p.add_argument("--step", required=True)
    p.add_argument("--attempt", type=int, default=1)
    p.add_argument("--iteration", type=int, default=None)
    p.add_argument("--expect", required=True, help="<nonce>:<snapshot_digest>:<ledger_head>")
    p.add_argument("--in-flight", dest="in_flight", nargs="*", default=[], help="Step ids running in parallel")
    p.add_argument("--subject", default="", help="Verifier steps: <step-id>@<sha>")
    p.add_argument("--report", default="", help="Verifier steps: the step-scoped report path")
    p.add_argument("--spawn-error", dest="spawn_error", default="",
                   help="A file the orchestrator wrote from the sessions_spawn result it saw, outside the run "
                        "tree and every lane worktree: a recorded quota-exhaustion text trips the pool breaker")
    p.set_defaults(lanes_func=cmd_verify)

    p = sub.add_parser("integrate", help="Merge a verified scope branch into the integration worktree")
    _common(p)
    p.add_argument("--step", required=True, help="The integrating step")
    p.add_argument("--from", dest="from_step", required=True, help="The scope step to merge")
    p.add_argument("--iteration", type=int, default=None)
    p.add_argument("--expect", required=True)
    p.set_defaults(lanes_func=cmd_integrate)

    p = sub.add_parser("land", help="Fast-forward the run tree to the verified integration branch")
    _common(p)
    p.add_argument("--step", required=True)
    p.add_argument("--iteration", type=int, default=None)
    p.add_argument("--expect", required=True)
    p.set_defaults(lanes_func=cmd_land)

    p = sub.add_parser("gates", help="Antigravity gates: status (read-only), or `gates record --run-dir DIR`")
    gp = p.add_subparsers(dest="gates_action")
    rp_ = gp.add_parser("record", help="Write the evidence record from a probe window (terminal only, typed attestation)")
    rp_.add_argument("--run-dir", dest="run_dir", required=True, help="The probe window directory (named by its UTC stamp)")
    rp_.add_argument("--yes", action="store_true", help="Refused: every stage needs its typed attestation")
    p.set_defaults(lanes_func=None)

    p = sub.add_parser("count", help="Count a dispatch against a pool (retries included)")
    _common(p)
    p.add_argument("--pool", required=True)
    p.add_argument("--role", default="")
    p.add_argument("--step", default="")
    p.add_argument("--cap", type=int, default=None)
    p.set_defaults(lanes_func=cmd_count)


# ---------------------------------------------------------------------------------------------
# Antigravity gates: status and the evidence record (ADR-0010 PD-7)

ATTEST = {"G0": "PASS merge-gate", "G1": "PASS V5", "G2": "PASS V6", "G3": "PASS V7", "G4": "PASS V9",
          "G5": "PASS V12", "G6": "PASS operator", "G7": "APPROVE G7"}
CELL_ATTEST = "PASS cell claude/google/openrouter"


def cmd_gates_status(out: Callable[[str], None] = print) -> int:
    """Read-only: the gate table, the record state, who last touched the record. Runs nothing and writes nothing."""
    from . import agy_gate_evidence as ev
    from . import agy_lane_gates as gates
    table = gates.status_table()
    out(gates.summary_line())
    for g in gates.GATES:
        row = table[g.id]
        out(f"  {g.id}  {row['status']:<10} {g.name}" + (f"  [{row['evidence']}]" if row["evidence"] else ""))
    record = gates.record_status()
    if record.status == "valid" and record.commit:
        out(f"  record last touched by commit {record.commit[:8]}, author {record.author}")
    elif record.status == "invalid":
        out(f"  evidence file invalid: {record.reason}")
    out(f"  record file: {ev.RECORD_REL}")
    return EXIT_OK


def _build_manifest(run_dir: Path) -> list[dict]:
    """`[{relpath, sha256}]` of the regular files under `run_dir`. A symlink, a special file or too many files
    refuses: the listing has to describe exactly what the operator looked at."""
    from . import agy_gate_evidence as ev
    listing: list[dict] = []
    for root, dirs, files in os.walk(run_dir, followlinks=False):
        for name in sorted(dirs):
            if (Path(root) / name).is_symlink():
                raise Refused([f"{Path(root, name)} is a symlink: the run directory must hold regular files only"])
        for name in sorted(files):
            path = Path(root) / name
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode):
                raise Refused([f"{path} is not a regular file: the run directory must hold regular files only"])
            h = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            listing.append({"relpath": path.relative_to(run_dir).as_posix(), "sha256": h.hexdigest()})
            if len(listing) > ev.MAX_MANIFEST_FILES:
                raise Refused([f"the run directory has more than {ev.MAX_MANIFEST_FILES} files"])
    if not listing:
        raise Refused(["the run directory is empty"])
    return sorted(listing, key=lambda i: i["relpath"])


def cmd_gates_record(a: argparse.Namespace, *, ask: Callable[[str], str] | None = None, tty: bool | None = None,
                     root: Path | None = None, now: Callable[[], Any] | None = None,
                     out: Callable[[str], None] = print) -> int:
    """Write the evidence record from a probe window, after a typed attestation per stage.

    Refuses: `--yes`, a non-TTY or piped stdin/stdout, a kit root that is not a git work tree, a run directory that is
    not a plain directory named by its UTC stamp. It never runs `agy` or `openclaw`, never touches the network and never
    commits: it prints the commit command. Nothing is written unless at least one stage is attested and the result
    validates through the same loader the lane gates read."""
    import getpass
    from datetime import datetime, timezone
    from . import agy_gate_evidence as ev
    from . import agy_lane_gates as gates

    def refuse(reason: str) -> int:
        out(f"refused: {reason}")
        return EXIT_FAIL

    if getattr(a, "yes", False):
        return refuse("--yes is not accepted: every stage needs its typed attestation")
    is_tty = (sys.stdin.isatty() and sys.stdout.isatty()) if tty is None else tty
    if not is_tty:
        return refuse("record needs a terminal (stdin and stdout must be a TTY); it never reads piped answers")
    root = root or ev.kit_root()
    if not ev.is_git_root(root):
        return refuse(f"{root} is not a git work tree: the record is committed, so it needs one")
    given = Path(a.run_dir)
    if given.is_symlink() or not given.is_dir():
        return refuse(f"{given} is not a plain directory")
    run_dir = given.resolve()
    run_id = run_dir.name
    if not re.fullmatch(r"\d{8}T\d{6}Z", run_id):
        return refuse(f"the run directory must be named by its UTC stamp (20261101T101010Z), not {run_id!r}")
    try:
        listing = _build_manifest(run_dir)
    except Refused as exc:
        return refuse("; ".join(exc.reasons))
    digest = ev.manifest_digest(listing)
    ask = ask or input
    stamp = (now() if now else datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    who = (os.environ.get("USER") or getpass.getuser() or "operator")[:120]
    out(f"Run {run_id}: {len(listing)} files, manifest {digest[:12]}. Type the exact attestation to record a stage; "
        "anything else (or Enter) leaves it unproven.")
    entries: list[dict] = []
    for g in gates.GATES:
        out(f"\n{g.id}  {g.name}  ({g.verification or 'operator'})")
        if g.id == "G7":
            from .setup import lanes_render
            out(lanes_render.ACK_TEXT["google"])
            out(f"sha256 of this text: {ev.ack_text_sha256()}")
        if ask(f'Type "{ATTEST[g.id]}" to record {g.id}: ').strip() != ATTEST[g.id]:
            out(f"{g.id}: not attested; it stays {gates.EVIDENCE[g.id].status}.")
            continue
        entry = {"id": g.id, "status": gates.PROVEN, "verification": g.verification, "run_id": run_id,
                 "manifest_sha256": digest, "recorded_at": stamp, "recorded_by": who}
        if g.id == "G0":
            entry["anchor"] = ask("Heading of ADR-0007 Evidence that records the merge gate (it must contain "
                                  f"{run_id}): ").strip()
        elif g.id == "G1":
            entry["confirmed_slugs"] = [x.strip() for x in ask("Slugs V5 confirmed (comma separated): ").split(",")
                                        if x.strip()]
        elif g.id == "G6":
            raw = ask("Measured cost per step, tokens (a positive integer): ").strip()
            if not raw.isdigit():
                return refuse("G6 needs a positive integer")
            entry["cost_per_step_tokens"] = int(raw)
        elif g.id == "G7":
            entry["ack_text_sha256"] = ev.ack_text_sha256()
        entries.append(entry)
    cells: dict = {}
    if ask(f'Type "{CELL_ATTEST}" if the Claude Code Gemini tool-call probe through OpenRouter passed (Enter to skip): '
           ).strip() == CELL_ATTEST:
        cells["claude/google/openrouter"] = {"run_id": run_id, "manifest_sha256": digest, "recorded_at": stamp,
                                             "recorded_by": who}
    if not entries and not cells:
        return refuse("nothing was attested; nothing was written")
    doc: dict = {"schema": ev.SCHEMA, "generator": ev.GENERATOR, "run_id": run_id, "manifest": listing,
                 "gates": entries}
    if cells:
        doc["cells"] = cells
    doc["seal"] = ev.seal_of(doc)
    data = (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        ev.validate(data, root)
    except ev.RecordError as exc:
        return refuse(f"the record would be invalid ({exc}); nothing was written")
    target = ev.record_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.tmp-{os.getpid()}"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
    ev.clear_cache()
    checked = ev.load(root, check_git=False)
    if checked.status != "valid":
        target.unlink(missing_ok=True)
        ev.clear_cache()
        return refuse(f"the written record did not validate ({checked.reason}); it was removed")
    ids = ", ".join(e["id"] for e in entries) or "cell"
    out(f"\nWrote {ev.RECORD_REL} ({ids}). Review it, then commit it yourself (nothing was committed):")
    out(f'  git add {ev.RECORD_REL} && git commit -m "evidence: Antigravity gates {ids} from run {run_id}"')
    out("Until that commit, the lane stays held: the loader reads a record only when it is tracked and equal to HEAD.")
    return EXIT_OK


def run_gates(args: argparse.Namespace) -> int:
    if getattr(args, "gates_action", None) == "record":
        return cmd_gates_record(args)
    return cmd_gates_status()


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("lanes", help="Lane step control: prepare, verify, integrate, land, count (ADR-0007)")
    sp = p.add_subparsers(dest="lanes_action", required=True)
    _populate(sp)
    p.set_defaults(func=cmd_lanes)


def run(args: argparse.Namespace, runner: Runner | None = None) -> int:
    if (getattr(args, "lanes_action", None) or getattr(args, "action", None)) == "gates":
        return run_gates(args)
    runner = runner or default_runner
    try:
        run_id = _check_id(args.run, "run id")
        repo = Path(args.repo or os.getcwd()).resolve()
        ctx = Ctx(runner, repo, run_id, state_root())
        code, out = args.lanes_func(ctx, args)
    except Refused as exc:
        print(json.dumps({"result": "BLOCKED" if exc.blocked else "REFUSED", "reasons": exc.reasons}, sort_keys=True))
        return EXIT_BLOCKED if exc.blocked else EXIT_FAIL
    except (LaneCmdError, OSError, ValueError) as exc:
        print(json.dumps({"result": "ERROR", "reasons": [str(exc)]}, sort_keys=True))
        return EXIT_USAGE
    print(json.dumps(out, sort_keys=True))
    return code


def cmd_lanes(args: argparse.Namespace) -> int:
    return run(args, getattr(sys.modules[__name__], "RUNNER", None))


RUNNER: Runner | None = None


def main(argv: list[str] | None = None, runner: Runner | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(args, runner)


if __name__ == "__main__":
    raise SystemExit(main())

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
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from . import roles_policy as rp

Runner = Callable[[list, "str | None"], "tuple[int, str, str]"]

GENESIS = "0" * 64
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_ATTEMPTS = 3
RETRY_DELAY_S = 5.0
GIT = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null"]
KIT_IDENT = ["-c", "user.name=ai-resources lanes", "-c", "user.email=lanes@ai-resources.invalid"]
HANDOFF_FILES = ("handoff.md", ".agent-output/handoff.md")
HUMAN_COPY_SUFFIX = ".lane-verify.json"      # the kit's own human copy of a verify result: no authority
EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_BLOCKED = 0, 1, 2, 3
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
    return Path(base) / "ai-resources" / "lanes"


# ---------------------------------------------------------------------------------------------
# ledger


def _sha(data: bytes | str) -> str:
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _entry_hash(entry: dict) -> str:
    return _sha(_canon({k: v for k, v in entry.items() if k != "hash"}))


class Ledger:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx

    def exists(self) -> bool:
        return self.ctx.ledger_path.is_file()

    def entries(self) -> list[dict]:
        """All entries, with the chain verified. Raises Refused when it is broken."""
        if not self.exists():
            return []
        out: list[dict] = []
        prev = GENESIS
        for n, line in enumerate(self.ctx.ledger_path.read_text(encoding="utf-8").splitlines()):
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
               nonce: str | None = None, record: dict | None = None, data: dict | None = None) -> dict:
        self.ctx.run_dir.mkdir(parents=True, exist_ok=True)
        self.ctx.records_dir.mkdir(parents=True, exist_ok=True)
        with open(self.ctx.run_dir / "ledger.lock", "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            entries = self.entries()
            nonce = nonce or secrets.token_hex(16)
            entry: dict[str, Any] = {
                "seq": len(entries), "prev": self.head(entries), "kind": kind, "step": step, "role": role,
                "iteration": iteration, "attempt": attempt, "nonce": nonce, "record": None, "record_sha256": None,
                "data": data or {},
            }
            if record is not None:
                name = f"{kind}-{step}-i{iteration}-a{attempt}-{nonce[:12]}.json"
                path = self.ctx.records_dir / name
                body = json.dumps(record, sort_keys=True, indent=1)
                with open(path, "x", encoding="utf-8") as fh:        # write-once
                    fh.write(body)
                entry["record"] = name
                entry["record_sha256"] = _sha(body)
            entry["hash"] = _entry_hash(entry)
            with open(self.ctx.ledger_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, sort_keys=True) + "\n")
            return entry

    def record_ok(self, entry: dict) -> bool:
        if not entry.get("record"):
            return False
        path = self.ctx.records_dir / entry["record"]
        try:
            return _sha(path.read_bytes()) == entry.get("record_sha256")
        except OSError:
            return False

    def load_record(self, entry: dict) -> dict:
        return json.loads((self.ctx.records_dir / entry["record"]).read_text(encoding="utf-8"))


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
        for f in sorted(files):
            p = Path(root) / f
            h.update(str(p.relative_to(path)).encode())
            h.update(_hash_path(p).encode())
    return h.hexdigest()


def dirty_map(ctx: Ctx, tree: Path) -> dict[str, str]:
    """path -> `<XY>:<content sha>` for every modified, staged or untracked (not ignored) path."""
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
        if path.endswith(HUMAN_COPY_SUFFIX):
            continue
        result[path] = f"{xy}:{_hash_path(tree / path)}"
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
        out[p] = _hash_tree(path)
    return out


def agent_outputs(tree: Path) -> dict[str, str]:
    root = tree / ".agent-output"
    out: dict[str, str] = {}
    if root.is_dir():
        for p in sorted(root.rglob("*")):
            if (p.is_file() or p.is_symlink()) and not p.name.endswith(HUMAN_COPY_SUFFIX):
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
    }


# ---------------------------------------------------------------------------------------------
# path rules


def _report_re(role: str, step: str) -> re.Pattern:
    return re.compile(rf"^\.agent-output/{re.escape(role)}/{re.escape(step)}(?:[./].*)?$")


def tolerated(path: str) -> bool:
    parts = path.split("/")
    for t in rp.TOLERANCE_LIST:
        if t.endswith("/"):
            if t[:-1] in parts:
                return True
        elif path == t:
            return True
    return False


def forbidden_path(path: str, own_report: re.Pattern) -> str | None:
    if path in HANDOFF_FILES:
        return "the handoff file"
    if own_report.match(path):
        return None
    if path.startswith(".agent-output/") or path == ".agent-output":
        return "another step's .agent-output file"
    for f in rp.LANE_WRITE_FORBIDDEN:
        if f.endswith("/"):
            if path.startswith(f) or path == f[:-1]:
                return f"forbidden path {f}"
        elif path == f or path.startswith(f + "/"):
            return f"forbidden path {f}"
    return None


def _scope_paths_for(contract: str, role: str, step: str, scope: list[str], paths: dict[str, str],
                     *, own_report: re.Pattern) -> list[str]:
    """Reasons a set of changed paths violates the contract. `paths` maps path -> mode info or ''."""
    reasons: list[str] = []
    for path in sorted(paths):
        if tolerated(path):
            continue
        if own_report.match(path):
            continue
        bad = forbidden_path(path, own_report)
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
    if prep["nonce"] != nonce:
        raise Refused(["--expect nonce does not match the step's prepare entry"])
    if not ledger.record_ok(prep):
        raise Refused(["the prepare snapshot was rewritten: record sha256 differs from the ledger"])
    if prep["record_sha256"] != digest:
        raise Refused(["--expect snapshot_digest does not match the snapshot"])
    return auth, prep


def _roles_of(entries: list[dict], step: str) -> set[str]:
    return {e["role"] for e in entries if e["step"] == step and e["role"]}


def _verifier_pass(ledger: Ledger, auth: list[dict], subject_step: str, tip: str) -> dict | None:
    for e in reversed(auth):
        if e["kind"] != "verify" or e["role"] != "verifier" or e["data"].get("result") != "PASS":
            continue
        if e["data"].get("verdict") != "PASS":
            continue
        if e["data"].get("subject_step") != subject_step or e["data"].get("subject_sha") != tip:
            continue
        if _roles_of(auth, e["step"]) != {"verifier"} or not ledger.record_ok(e):
            continue
        rec = ledger.load_record(e)
        if rec.get("subject_sha") == tip and rec.get("verdict") == "PASS":
            return e
    return None


def _scope_pass(ledger: Ledger, auth: list[dict], step: str, tip: str) -> dict | None:
    for e in reversed(auth):
        if (e["kind"] == "verify" and e["step"] == step and e["data"].get("result") == "PASS"
                and e["data"].get("tip") == tip and ledger.record_ok(e)):
            return e
    return None


def _out(entry: dict | None, ledger: Ledger, extra: dict) -> dict:
    res = dict(extra)
    if entry is not None:
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
    if not ID_RE.match(value or ""):
        raise LaneCmdError(f"invalid {what} {value!r}")
    return value


def _tree(ctx: Ctx) -> Path:
    top = ctx.git(["rev-parse", "--show-toplevel"], cwd=ctx.repo).strip()
    return Path(top)


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
    family = rp.model_family(a.model)
    ledger = Ledger(ctx)
    entries = ledger.entries()

    prev_iters = [e["iteration"] for e in entries if e["kind"] == "prepare" and e["step"] == step]
    last_iter = max(prev_iters) if prev_iters else 0
    iteration = last_iter + 1 if (a.reenter or last_iter == 0) else last_iter
    if any(e["kind"] == "prepare" and e["step"] == step and e["iteration"] == iteration
           and e["attempt"] == a.attempt for e in entries):
        raise Refused([f"step {step} attempt {a.attempt} already prepared in iteration {iteration}"])
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
            if (wfam is not None and wfam == family) or (wfam is None and wref == a.model):
                raise Refused([f"checker {role} model {a.model!r} (family {family}) equals the family of writer "
                               f"step {e['step']} ({e['role']}, model {wref!r}, family {wfam})"])

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
    wt = None
    if contract in (rp.SCOPE, rp.INTEGRATING):
        wt = create_worktree(ctx, _wt_name(role, step, ctx.run, iteration, a.attempt), snap["head"])
        data.update(worktree_id=wt["id"], worktree_path=wt["path"], branch=wt["branch"])
        snap["refs"] = refs_map(ctx, tree)           # include the new branch: it is the step's own
    entry = ledger.append("prepare", step, role=role, iteration=iteration, attempt=a.attempt,
                          record={"snapshot": snap, "step": step, "role": role, "iteration": iteration,
                                  "attempt": a.attempt}, data=data)
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
                                step: str, role: str, contract: str, own_report: re.Pattern) -> list[str]:
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
        if before["agent_outputs"].get(p) != after["agent_outputs"].get(p) and not own_report.match(p) \
                and p not in HANDOFF_FILES:
            reasons.append(f"{p}: another step's .agent-output file changed")
    for p in sorted(before["tripwires"]):
        if before["tripwires"][p] != after["tripwires"].get(p):
            reasons.append(f"tripwire file {p} changed")
    # changed dirty paths in the run tree
    changed = {p for p in set(before["dirty"]) | set(after["dirty"])
               if before["dirty"].get(p) != after["dirty"].get(p)}
    for p in sorted(changed):
        if tolerated(p) or own_report.match(p):
            continue
        if contract == rp.REPORT:
            reasons.append(f"{p}: a report step may write only .agent-output/{role}/{step}*")
        else:
            reasons.append(f"{p}: the run tree changed during a worktree step")
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

    reasons += _forbidden_run_tree_changes(ctx, tree, before, after, allowed_refs=allowed_refs, step=step,
                                           role=role, contract=contract, own_report=own_report)

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
        for p in dirty:
            paths[p] = "dirty"
        reasons += _scope_paths_for(contract, role, step, scope, paths, own_report=own_report)
        for p in dirty:
            if not tolerated(p) and not own_report.match(p):
                reasons.append(f"{p}: uncommitted change in the worktree: commit it on the branch")
        reasons += _check_modes(ctx, wt, base, tip, contract)
        reasons += _check_messages(ctx, wt, base, tip)
        changed_paths = paths

    record: dict[str, Any] = {"step": step, "role": role, "contract": contract, "iteration": iteration,
                              "attempt": a.attempt, "tip": tip, "base": base, "changed": sorted(changed_paths)}
    data: dict[str, Any] = {"contract": contract, "tip": tip}

    if role == "verifier":
        subject = a.subject or ""
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)@([0-9a-f]{7,64})$", subject)
        if not m:
            reasons.append("a verifier verify needs --subject <step-id>@<sha>")
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
    if result == "FAIL" and contract in (rp.SCOPE, rp.INTEGRATING) and prep["data"].get("worktree_id"):
        wid = prep["data"]["worktree_id"]
        if wid not in _removed_ids(ledger.entries()):
            remove_worktree(ctx, wid)
            e2 = ledger.append("worktree-removed", step, role=role, iteration=iteration, attempt=a.attempt,
                               data={"worktree_id": wid})
            out["ledger_head"] = e2["hash"]
            out["worktree_removed"] = wid
    # a human-readable copy with no authority
    try:
        human = tree / ".agent-output" / role / f"{step}.lane-verify.json"
        human.parent.mkdir(parents=True, exist_ok=True)
        human.write_text(json.dumps({k: v for k, v in out.items() if k not in ("nonce",)}, indent=1, sort_keys=True),
                         encoding="utf-8")
    except OSError:
        pass
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
    with open(ctx.run_dir / "land.lock", "a+") as lock:           # lands within a run are serialised
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
        vpass = None
        for e in reversed(auth):
            if (e["kind"] == "verify" and e["step"] == step and e["iteration"] == iteration
                    and e["attempt"] == prep["attempt"] and e["data"].get("result") == "PASS" and ledger.record_ok(e)):
                vpass = e
                break
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
        try:
            fresh = ledger.entries()
            removed += _remove_recorded(ctx, ledger, fresh, step, keep_seq=None)
            for e in fresh:
                if e["kind"] == "integrate" and e["step"] == step and e["iteration"] == iteration:
                    removed += _remove_recorded(ctx, ledger, ledger.entries(), e["data"]["from"])
        except LaneCmdError as exc:
            reasons = [f"cleanup: {exc}"]
        final = ledger.entries()[-1]
        return EXIT_OK, _out(final, ledger, {"result": "LANDED", "tip": tip, "head": new_head,
                                             "removed": removed, "snapshot_digest": entry["record_sha256"] or ""})


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
    p.add_argument("--attempt", type=int, default=1)
    p.add_argument("--reenter", action="store_true", help="Re-entering the step through on_concern_return_to")
    p.add_argument("--scope", nargs="*", default=[], help="write_scope paths (scope contract)")
    p.add_argument("--pool", default="", help="Count the dispatch against this pool")
    p.add_argument("--cap", type=int, default=None)
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

    p = sub.add_parser("count", help="Count a dispatch against a pool (retries included)")
    _common(p)
    p.add_argument("--pool", required=True)
    p.add_argument("--role", default="")
    p.add_argument("--step", default="")
    p.add_argument("--cap", type=int, default=None)
    p.set_defaults(lanes_func=cmd_count)


def add_subparser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("lanes", help="Lane step control: prepare, verify, integrate, land, count (ADR-0007)")
    sp = p.add_subparsers(dest="lanes_action", required=True)
    _populate(sp)
    p.set_defaults(func=cmd_lanes)


def run(args: argparse.Namespace, runner: Runner | None = None) -> int:
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

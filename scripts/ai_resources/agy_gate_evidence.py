"""The committed evidence record of the Antigravity lane gates (ADR-0010 N3/PD-7, ADR-0007 section 11).

`agy_lane_gates.GATES` and `EVIDENCE` stay literals that nobody edits by hand. When the operator has run the probe window,
`ai-resources lanes gates record --run-dir <dir>` writes ONE file, `scripts/ai_resources/data/agy_gate_evidence.json`, and
the operator commits it. This module reads that file, fails closed, and turns a valid one into gate state.

Trust model (PD-7): tamper evidence plus git review, not authentication. Nothing here can tell who typed the attestation.
What it does give:

- the file is read without following symlinks, with a size cap, and with a strict schema (unknown keys, duplicate gates,
  wrong types and short hashes are refused);
- a `seal` over the canonical content, so a hand-written JSON that passes the schema but was not made by `record` is
  refused (the seal is a guard against casual edits and generator mix-ups, not a secret);
- in a git kit root the file must be tracked and equal to `HEAD`; the last-touching commit and its author are reported;
- the manifest listing (`relpath`, `sha256` of every file of the run directory) is committed with the record, and every
  entry's `manifest_sha256` must equal the hash of that listing;
- G0 names a heading of ADR-0007 "Evidence" that contains the run id; G1 covers every Antigravity slug a preset uses;
  G7 equals the hash of the current Google acknowledgement text, so changing the text un-proves it.

No import-time I/O (architect C4): `load` is an explicit cached function and `EVIDENCE` is still a literal.
Any defect makes the whole record invalid, every gate keeps its literal status, and `refusal()` says why.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from . import agy_lane_gates as gates_mod

RECORD_REL = "scripts/ai_resources/data/agy_gate_evidence.json"
ADR_REL = "docs/decisions/0007-subscription-lanes.md"
SCHEMA = 1
MAX_BYTES = 64 * 1024
MAX_MANIFEST_FILES = 400
GENERATOR = "ai-resources lanes gates record"
SEAL_DOMAIN = "ai-resources agy gate evidence v1\n"
CELL_KEYS = ("claude/google/openrouter",)
GIT_TIMEOUT_S = 10
PRESET_PROFILES = ("balanced", "performance", "low-cost")

_RUN_ID = re.compile(r"^\d{8}T\d{6}Z$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_TOP_KEYS = {"schema", "generator", "run_id", "manifest", "gates", "cells", "seal"}
_BASE_GATE_KEYS = {"id", "status", "verification", "run_id", "manifest_sha256", "recorded_at", "recorded_by"}
_EXTRA_GATE_KEYS = {"G0": {"anchor"}, "G1": {"confirmed_slugs"}, "G6": {"cost_per_step_tokens"},
                    "G7": {"ack_text_sha256"}}
_CELL_FIELDS = {"run_id", "manifest_sha256", "recorded_at", "recorded_by"}
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


class RecordError(ValueError):
    """The record is invalid; the message is the reason `refusal()` shows."""


@dataclass(frozen=True)
class Record:
    status: str                                   # absent | valid | invalid
    reason: str = ""
    gates: Mapping[str, gates_mod.Gate] = field(default_factory=dict)
    cells: frozenset = frozenset()
    run_id: str = ""
    recorded_at: str = ""
    recorded_by: str = ""
    confirmed_slugs: tuple = ()
    commit: str = ""                              # last commit that touched the file ("" outside git)
    author: str = ""
    file_sha8: str = ""


ABSENT = Record("absent")

_CACHE: dict[tuple, Record] = {}


def kit_root() -> Path:
    from . import repo_root
    return Path(repo_root())


def record_path(root: Path | None = None) -> Path:
    return (root or kit_root()) / RECORD_REL


def clear_cache() -> None:
    _CACHE.clear()


# --- canonical form and seal ---------------------------------------------------------------------------------------

def canonical(doc: Mapping[str, Any]) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def seal_of(doc: Mapping[str, Any]) -> str:
    body = {k: v for k, v in doc.items() if k != "seal"}
    return hashlib.sha256((SEAL_DOMAIN + canonical(body)).encode("utf-8")).hexdigest()


def manifest_digest(listing: list[Mapping[str, str]]) -> str:
    """sha256 over the sorted `(relpath, sha256)` pairs of the run directory's regular files."""
    h = hashlib.sha256()
    for item in sorted(listing, key=lambda i: i["relpath"]):
        h.update(item["relpath"].encode("utf-8") + b"\0" + item["sha256"].encode("ascii") + b"\n")
    return h.hexdigest()


def ack_text_sha256() -> str:
    from .setup import lanes_render
    return hashlib.sha256(lanes_render.ACK_TEXT["google"].encode("utf-8")).hexdigest()


# --- reading --------------------------------------------------------------------------------------------------------

def _read_nofollow(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        raise RecordError(f"cannot open the record without following links ({exc.strerror or type(exc).__name__})")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RecordError("the record is not a regular file")
        data = os.read(fd, MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_BYTES:
        raise RecordError(f"the record is larger than {MAX_BYTES} bytes")
    return data


def _utc(text: Any, what: str) -> str:
    if not isinstance(text, str) or not text:
        raise RecordError(f"{what}: not a UTC timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise RecordError(f"{what}: not an ISO timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise RecordError(f"{what}: not in UTC")
    return text


def _text(value: Any, what: str, limit: int = 160) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or _CTRL.search(value):
        raise RecordError(f"{what}: must be a short printable text")
    return value


def _hex(value: Any, what: str) -> str:
    if not isinstance(value, str) or not _HEX64.match(value):
        raise RecordError(f"{what}: must be 64 lowercase hex characters")
    return value


def _preset_agy_slugs(root: Path) -> set[str]:
    """Every Antigravity slug any preset profile names (the slugs G1 must cover)."""
    try:
        import yaml
    except ImportError:                                                     # pragma: no cover - PyYAML is a dependency
        raise RecordError("PyYAML is required to check the preset slugs")
    slugs: set[str] = set()
    base = root / "profiles"
    files = [base / f"{p}.yaml" for p in PRESET_PROFILES]
    files += sorted((base / "gateway").glob("*.yaml")) if (base / "gateway").is_dir() else []
    for path in files:
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            raise RecordError(f"cannot read preset profile {path.name}")
        block = doc.get("lanes") or {}
        for cands in (block.get("roles") or {}).values():
            slugs |= {c[4:] for c in cands or () if isinstance(c, str) and c.startswith("agy:")}
    return slugs


def _headings(root: Path) -> list[str]:
    """The headings of ADR-0007's "Evidence" section (text only, no leading `#`)."""
    try:
        text = (root / ADR_REL).read_text(encoding="utf-8")
    except OSError:
        raise RecordError("the G0 anchor cannot be checked: ADR-0007 is not readable")
    start = re.search(r"^## Evidence\s*$", text, re.M)
    if not start:
        raise RecordError("ADR-0007 has no Evidence section")
    body = text[start.end():]
    nxt = re.search(r"^## ", body, re.M)
    section = body[: nxt.start()] if nxt else body
    return [m.group(1).strip() for m in re.finditer(r"^#{3,6}\s+(.+?)\s*$", section, re.M)]


def _check_gate(entry: Any, doc: Mapping[str, Any], root: Path, digest: str, preset_slugs: set[str]) -> gates_mod.Gate:
    if not isinstance(entry, dict):
        raise RecordError("a gate entry is not an object")
    gid = entry.get("id")
    known = {g.id: g for g in gates_mod.GATES}
    if gid not in known:
        raise RecordError(f"unknown gate id {gid!r}")
    gate = known[gid]
    allowed = _BASE_GATE_KEYS | _EXTRA_GATE_KEYS.get(gid, set())
    extra = set(entry) - allowed
    if extra:
        raise RecordError(f"{gid}: unknown key {sorted(extra)[0]!r}")
    missing = allowed - set(entry)
    if missing:
        raise RecordError(f"{gid}: missing {sorted(missing)[0]!r}")
    if entry["status"] != gates_mod.PROVEN:
        raise RecordError(f"{gid}: status must be 'proven'")
    if entry["verification"] != gate.verification:
        raise RecordError(f"{gid}: verification must be {gate.verification!r}")
    if entry["run_id"] != doc["run_id"]:
        raise RecordError(f"{gid}: run_id differs from the record's run")
    if _hex(entry["manifest_sha256"], f"{gid}.manifest_sha256") != digest:
        raise RecordError(f"{gid}: manifest_sha256 does not match the committed manifest listing")
    _utc(entry["recorded_at"], f"{gid}.recorded_at")
    _text(entry["recorded_by"], f"{gid}.recorded_by", 120)
    if gid == "G0":
        anchor = _text(entry["anchor"], "G0.anchor", 200)
        if doc["run_id"] not in anchor or anchor not in _headings(root):
            raise RecordError("G0.anchor must name a heading of ADR-0007 Evidence that contains the run id")
    elif gid == "G1":
        slugs = entry["confirmed_slugs"]
        if (not isinstance(slugs, list) or not slugs or not all(isinstance(s, str) for s in slugs)
                or len(set(slugs)) != len(slugs)):
            raise RecordError("G1.confirmed_slugs must be a non-empty list of distinct slugs")
        from . import model_pins
        if not set(slugs) <= set(model_pins.AGY_LANE_MODELS):
            raise RecordError("G1.confirmed_slugs names a slug that is not in AGY_LANE_MODELS")
        if not preset_slugs <= set(slugs):
            raise RecordError("G1.confirmed_slugs does not cover " + ", ".join(sorted(preset_slugs - set(slugs))))
    elif gid == "G6":
        cost = entry["cost_per_step_tokens"]
        ok_int = isinstance(cost, int) and not isinstance(cost, bool) and 0 < cost <= 10 ** 9
        ok_map = (isinstance(cost, dict) and cost
                  and all(isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and 0 < v <= 10 ** 9
                          for k, v in cost.items()))
        if not (ok_int or ok_map):
            raise RecordError("G6.cost_per_step_tokens must be a positive count or a role -> count map")
    elif gid == "G7":
        if _hex(entry["ack_text_sha256"], "G7.ack_text_sha256") != ack_text_sha256():
            raise RecordError("G7.ack_text_sha256 differs from the current Google acknowledgement text")
    return replace(gate, status=gates_mod.PROVEN,
                   evidence=f"record {doc['run_id']} ({entry['recorded_by']}, {entry['recorded_at']})", note="")


def validate(data: bytes, root: Path) -> Record:
    """The record in `data`, or `RecordError`. No git and no cache here: pure parsing and checks."""
    try:
        doc = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError) as exc:
        raise RecordError(f"not valid JSON ({exc})")
    if not isinstance(doc, dict):
        raise RecordError("the record is not an object")
    extra = set(doc) - _TOP_KEYS
    if extra:
        raise RecordError(f"unknown key {sorted(extra)[0]!r}")
    for key in ("schema", "generator", "run_id", "manifest", "gates", "seal"):
        if key not in doc:
            raise RecordError(f"missing {key!r}")
    if doc["schema"] != SCHEMA or isinstance(doc["schema"], bool):
        raise RecordError(f"unsupported schema {doc['schema']!r}")
    if doc["generator"] != GENERATOR:
        raise RecordError("not written by `ai-resources lanes gates record`")
    if _hex(doc["seal"], "seal") != seal_of(doc):
        raise RecordError("the seal does not match the content (a record is made by `lanes gates record`, not by hand)")
    if not isinstance(doc["run_id"], str) or not _RUN_ID.match(doc["run_id"]):
        raise RecordError("run_id must look like 20261101T101010Z")
    listing = doc["manifest"]
    if not isinstance(listing, list) or not listing or len(listing) > MAX_MANIFEST_FILES:
        raise RecordError("manifest must list the run directory's files")
    seen: set[str] = set()
    for item in listing:
        if not isinstance(item, dict) or set(item) != {"relpath", "sha256"}:
            raise RecordError("a manifest item must have exactly relpath and sha256")
        rel = _text(item["relpath"], "manifest.relpath", 300)
        if rel.startswith("/") or ".." in rel.split("/") or rel in seen:
            raise RecordError("manifest.relpath must be unique, relative and without '..'")
        seen.add(rel)
        _hex(item["sha256"], "manifest.sha256")
    if [i["relpath"] for i in listing] != sorted(seen):
        raise RecordError("the manifest must be sorted by relpath")
    digest = manifest_digest(listing)
    gates_in = doc["gates"]
    if not isinstance(gates_in, list) or not gates_in:
        raise RecordError("gates must be a non-empty list")
    ids = [g.get("id") if isinstance(g, dict) else None for g in gates_in]
    if len(set(map(str, ids))) != len(ids):
        raise RecordError("duplicate gate id")
    preset_slugs = _preset_agy_slugs(root) if "G1" in ids else set()
    gates = {}
    for entry in gates_in:
        g = _check_gate(entry, doc, root, digest, preset_slugs)
        gates[g.id] = g
    cells_in = doc.get("cells") or {}
    if not isinstance(cells_in, dict):
        raise RecordError("cells must be an object")
    cells = set()
    for key, cell in cells_in.items():
        if key not in CELL_KEYS:
            raise RecordError(f"unknown cell {key!r}")
        if not isinstance(cell, dict) or set(cell) != _CELL_FIELDS:
            raise RecordError(f"cell {key}: wrong fields")
        if cell["run_id"] != doc["run_id"] or _hex(cell["manifest_sha256"], "cell.manifest_sha256") != digest:
            raise RecordError(f"cell {key}: run or manifest differs from the record's")
        _utc(cell["recorded_at"], "cell.recorded_at")
        _text(cell["recorded_by"], "cell.recorded_by", 120)
        cells.add(key)
    first = gates_in[0]
    slugs = tuple(next((e["confirmed_slugs"] for e in gates_in if e.get("id") == "G1"), ()))
    return Record("valid", gates=gates, cells=frozenset(cells), run_id=doc["run_id"],
                  recorded_at=first["recorded_at"], recorded_by=first["recorded_by"], confirmed_slugs=slugs,
                  file_sha8=hashlib.sha256(data).hexdigest()[:8])


def _no_duplicates(pairs: list) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


# --- git ----------------------------------------------------------------------------------------------------------

def _git_env() -> dict[str, str]:
    return {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C", "HOME": "/nonexistent",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"}


def _git(root: Path, *args: str) -> tuple[int, str]:
    argv = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", *args]
    try:
        proc = subprocess.run(argv, cwd=str(root), env=_git_env(), capture_output=True, text=True,
                              timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, type(exc).__name__
    return proc.returncode, proc.stdout


def is_git_root(root: Path) -> bool:
    return (root / ".git").exists()


def _git_state(root: Path) -> tuple[str, str, str]:
    """`(problem, commit, author)` for the record in a git kit root. A problem fails the record closed."""
    rc, _ = _git(root, "ls-files", "--error-unmatch", "--", RECORD_REL)
    if rc != 0:
        return "the record is not tracked by git: commit it first", "", ""
    rc, _ = _git(root, "diff", "--quiet", "HEAD", "--", RECORD_REL)
    if rc == 1:
        return "the record differs from HEAD: commit or restore it", "", ""
    if rc != 0:
        return "git could not compare the record with HEAD", "", ""
    rc, out = _git(root, "log", "-1", "--format=%H%n%an", "--", RECORD_REL)
    lines = out.splitlines() if rc == 0 else []
    return "", (lines[0] if lines else ""), (lines[1] if len(lines) > 1 else "")


def _signature(root: Path, path: Path) -> tuple:
    def sig(p: Path) -> tuple:
        try:
            st = os.lstat(str(p))
            return (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            return ()
    return (sig(path), sig(root / ".git" / "index"), sig(root / ".git" / "HEAD"))


# --- the cached loader --------------------------------------------------------------------------------------------

def load(root: Path | None = None, *, check_git: bool = True) -> Record:
    """The record of the kit at `root` (default: this kit). Cached on the file's and git's own timestamps.

    `absent` when there is no file; `invalid` (with `reason`) for any defect; `valid` carries the proven gates."""
    root = root or kit_root()
    path = record_path(root)
    if not os.path.lexists(str(path)):
        # ADR-0016 D3: no record means the gates are not proven. There is no host default and no overlay.
        return ABSENT
    key = (str(root), check_git, _signature(root, path))
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    try:
        rec = validate(_read_nofollow(path), root)
        if check_git and is_git_root(root):
            problem, commit, author = _git_state(root)
            if problem:
                raise RecordError(problem)
            rec = replace(rec, commit=commit, author=author)
    except RecordError as exc:
        rec = Record("invalid", reason=str(exc))
    _CACHE.clear()
    _CACHE[key] = rec
    return rec


def slug_confirmed(slug: str, root: Path | None = None) -> bool:
    """Whether the record confirms this Antigravity slug (G1 proven by a valid record naming it)."""
    rec = load(root)
    return rec.status == "valid" and "G1" in rec.gates and slug in rec.confirmed_slugs


def cell_verified(key: str, root: Path | None = None) -> bool:
    rec = load(root)
    return rec.status == "valid" and key in rec.cells

"""A throwaway kit root for the Antigravity evidence record tests (ADR-0010 S7).

Never the real repo and never /tmp: callers pass pytest's `tmp_path` (the suite's basetemp lives under ~/.cache)."""
from __future__ import annotations

import copy
import json
import pathlib
import shutil
import subprocess

from ai_resources import agy_gate_evidence as ev
from ai_resources import agy_lane_gates as gates

REPO = pathlib.Path(__file__).resolve().parents[1]
RUN_ID = "20261101T101010Z"
ANCHOR = f"Phase 2 merge gate PASS {RUN_ID}"
WHEN = "2026-11-01T10:30:00Z"
WHO = "operator"
ADR = f"""# ADR-0007

## Decision

### 1. Something

## Evidence

### {ANCHOR}

The isolated merge gate passed.

## Alternatives Considered
"""


def make_root(tmp: pathlib.Path, *, git: bool = False) -> pathlib.Path:
    root = tmp / "kit"
    (root / "docs" / "decisions").mkdir(parents=True)
    (root / "docs" / "decisions" / "0007-subscription-lanes.md").write_text(ADR, encoding="utf-8")
    shutil.copytree(REPO / "profiles", root / "profiles")
    (root / "scripts" / "ai_resources" / "data").mkdir(parents=True)
    if git:
        run_git(root, "init", "-q", "-b", "main")
        run_git(root, "add", "-A")
        run_git(root, "commit", "-q", "-m", "kit")
    return root


def run_git(root: pathlib.Path, *args: str) -> str:
    cmd = ["git", "-c", "user.name=Op Erator", "-c", "user.email=op@example.invalid", "-c", "commit.gpgsign=false", *args]
    return subprocess.run(cmd, cwd=root, check=True, capture_output=True, text=True).stdout


def listing() -> list[dict]:
    return [{"relpath": "v5/models.txt", "sha256": "a" * 64}, {"relpath": "v6/run.log", "sha256": "b" * 64}]


def preset_slugs(root: pathlib.Path) -> list[str]:
    return sorted(ev._preset_agy_slugs(root))


def valid_doc(root: pathlib.Path, *, cells: bool = True) -> dict:
    lst = listing()
    digest = ev.manifest_digest(lst)
    base = {"run_id": RUN_ID, "manifest_sha256": digest, "recorded_at": WHEN, "recorded_by": WHO}
    entries = []
    for g in gates.GATES:
        entry = {"id": g.id, "status": "proven", "verification": g.verification, **base}
        if g.id == "G0":
            entry["anchor"] = ANCHOR
        elif g.id == "G1":
            entry["confirmed_slugs"] = preset_slugs(root)
        elif g.id == "G6":
            entry["cost_per_step_tokens"] = 12000
        elif g.id == "G7":
            entry["ack_text_sha256"] = ev.ack_text_sha256()
        entries.append(entry)
    doc = {"schema": 1, "generator": ev.GENERATOR, "run_id": RUN_ID, "manifest": lst, "gates": entries}
    if cells:
        doc["cells"] = {"claude/google/openrouter": dict(base)}
    return resealed(doc)


def resealed(doc: dict) -> dict:
    doc = copy.deepcopy(doc)
    doc["seal"] = ev.seal_of(doc)
    return doc


def write(root: pathlib.Path, doc: dict | bytes | str) -> pathlib.Path:
    path = ev.record_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(doc, dict):
        doc = json.dumps(doc, indent=2, sort_keys=True) + "\n"
    path.write_bytes(doc.encode("utf-8") if isinstance(doc, str) else doc)
    return path


def use(monkeypatch, root: pathlib.Path) -> None:
    """Point the loader at `root` and forget what it cached."""
    monkeypatch.setattr(ev, "kit_root", lambda: root)
    ev.clear_cache()

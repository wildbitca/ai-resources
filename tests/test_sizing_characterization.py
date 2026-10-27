"""Characterization of the sizing/scorecard surface: the CLI, the Python helpers and both workflow scripts.

tests/golden/sizing_snapshot.json was taken on the revision before the sizing consolidation (ff08d9d) over every
accepted input spelling (see tests/sizing_snapshot.py). Any observable change shows up here as a diff of one case.
When a change is intended, regenerate with `UPDATE_SIZING_GOLDEN=1 pytest tests/test_sizing_characterization.py`
and list the cases that moved in the commit message.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

import sizing_snapshot

GOLDEN = Path(__file__).resolve().parent / "golden" / "sizing_snapshot.json"


def _dump(snap: dict) -> str:
    """One case per line, so a git diff of the golden names the cases that changed."""
    lines = ["{"]
    sections = list(snap)
    for si, section in enumerate(sections):
        lines.append(f"{json.dumps(section)}: {{")
        keys = sorted(snap[section])
        for ki, key in enumerate(keys):
            lines.append(f"{json.dumps(key)}: {json.dumps(snap[section][key], sort_keys=True)}{',' if ki < len(keys) - 1 else ''}")
        lines.append("}" + ("," if si < len(sections) - 1 else ""))
    lines.append("}")
    return "\n".join(lines) + "\n"


def _check(section: str, built: dict) -> None:
    if os.environ.get("UPDATE_SIZING_GOLDEN"):
        return
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))[section]
    assert sorted(built) == sorted(golden), "the set of characterized cases changed"
    moved = [k for k in golden if built[k] != golden[k]]
    assert not moved, f"{len(moved)} case(s) differ from the golden, first: {moved[0]}\n golden={golden[moved[0]]}\n built ={built[moved[0]]}"


@pytest.fixture(scope="module")
def snapshot_store():
    store: dict = {}
    yield store
    if os.environ.get("UPDATE_SIZING_GOLDEN") and len(store) == 4:
        GOLDEN.write_text(_dump({k: store[k] for k in ("python", "cli", "plan", "implement")}), encoding="utf-8")


def test_python_helpers_are_unchanged(snapshot_store):
    snapshot_store["python"] = built = sizing_snapshot.python_snapshot()
    _check("python", built)


def test_scorecard_cli_is_unchanged(snapshot_store):
    snapshot_store["cli"] = built = sizing_snapshot.cli_snapshot()
    _check("cli", built)


@pytest.mark.node_scripts
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_kit_plan_is_unchanged(snapshot_store):
    snapshot_store["plan"] = built = sizing_snapshot.plan_cases()
    _check("plan", built)


@pytest.mark.node_scripts
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_kit_implement_is_unchanged(snapshot_store):
    snapshot_store["implement"] = built = sizing_snapshot.impl_cases()
    _check("implement", built)

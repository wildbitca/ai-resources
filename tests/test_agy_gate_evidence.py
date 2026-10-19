"""ADR-0010 S7: the fail-closed Antigravity evidence record. No gate is flipped and no record is shipped.

Every record here lives in a throwaway kit root under pytest's tmp_path; the real record path is never written."""
from __future__ import annotations

import copy
import json
import os
import pathlib
import subprocess
import sys

import pytest

import agy_evidence_fakes as fk
from ai_resources import agy_gate_evidence as ev
from ai_resources import agy_lane_gates as gates
from ai_resources import model_pins
from ai_resources.setup import compat, lanes_render
from ai_resources.setup.cockpits import openclaw_lanes

REPO = pathlib.Path(__file__).resolve().parents[1]
LITERAL = {g.id: g.status for g in gates.GATES}


@pytest.fixture(autouse=True)
def _fresh_cache():
    ev.clear_cache()
    yield
    ev.clear_cache()


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = fk.make_root(tmp_path)
    fk.use(monkeypatch, r)
    return r


def _statuses():
    return {g.id: gates.effective_evidence()[g.id].status for g in gates.GATES}


# --- AC-S7a/g: no record, nothing changes -----------------------------------------------------------------------

def test_the_shipped_tree_has_no_record_and_the_gates_are_the_literals():
    assert not (REPO / ev.RECORD_REL).exists()
    assert ev.load().status == "absent"
    assert _statuses() == LITERAL == {"G0": "not proven", "G1": "not proven", "G2": "not proven", "G3": "not proven",
                                      "G4": "not proven", "G5": "not proven", "G6": "partial", "G7": "partial"}
    assert model_pins.AGY_LANE_MODELS_CONFIRMED is False
    assert openclaw_lanes.writable_backends() == {"claude-cli"}
    assert "evidence file invalid" not in (gates.refusal() or "")
    assert gates.refusal() == gates.refusal(gates.EVIDENCE)


def test_with_no_record_the_refusal_compat_and_pins_equal_the_literal_values(root):
    assert ev.load().status == "absent"
    assert gates.refusal() == gates.refusal(gates.EVIDENCE)
    assert gates.status_table() == gates.status_table(gates.EVIDENCE)
    assert compat.cell("claude", "google", compat.MULTI_OPENROUTER).verified is False
    assert compat.claude_google_openrouter_verified() is False
    assert not model_pins.agy_slug_confirmed("gemini-3.8-flash-high")


def test_the_evidence_literal_is_untouched_and_nothing_reads_the_disk_at_import():
    """C4: EVIDENCE is a literal and importing the modules does no I/O."""
    code = (
        "import builtins, os, subprocess, sys\n"
        f"sys.path.insert(0, {str(REPO / 'scripts')!r})\n"
        "def boom(*a, **k):\n    raise AssertionError('I/O at import')\n"
        "builtins.open = boom; os.open = boom; subprocess.run = boom; subprocess.Popen = boom\n"
        "from ai_resources import agy_lane_gates, agy_gate_evidence, model_pins\n"
        "assert agy_gate_evidence._CACHE == {}\n"
        "assert all(g.status != 'proven' for g in agy_lane_gates.EVIDENCE.values())\n"
        "print('ok')\n")
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout.strip() == "ok", out.stderr


# --- AC-S7d: a complete valid record ------------------------------------------------------------------------------

def test_a_complete_valid_record_proves_every_gate_and_opens_the_lane(root):
    fk.write(root, fk.valid_doc(root))
    rec = ev.load(root)
    assert rec.status == "valid", rec.reason
    assert gates.all_proven() and gates.refusal() is None
    assert openclaw_lanes.writable_backends() == {"claude-cli", "agy-cli-lane"}
    assert gates.gate_proven("G5") and openclaw_lanes.agy_home_wanted()
    assert model_pins.AGY_LANE_MODELS_CONFIRMED is False                    # the constant is never flipped (C3)
    for slug in fk.preset_slugs(root):
        assert model_pins.agy_slug_confirmed(slug)
    assert not model_pins.agy_slug_confirmed("gpt-oss-120b-medium")         # per slug, not all or nothing
    assert compat.cell("claude", "google", compat.MULTI_OPENROUTER).verified is True
    assert "on, gates 8/8 proven" in gates.summary_line() and fk.RUN_ID in gates.summary_line()


def test_a_record_without_cells_leaves_the_openrouter_cell_unverified(root):
    fk.write(root, fk.valid_doc(root, cells=False))
    assert ev.load(root).status == "valid"
    assert compat.cell("claude", "google", compat.MULTI_OPENROUTER).verified is False
    assert gates.all_proven()


def test_a_partial_record_proves_only_its_gates(root):
    doc = fk.valid_doc(root)
    doc["gates"] = [e for e in doc["gates"] if e["id"] in ("G2", "G5")]
    doc.pop("cells")
    fk.write(root, fk.resealed(doc))
    assert ev.load(root).status == "valid"
    st = _statuses()
    assert st["G2"] == st["G5"] == "proven" and st["G0"] == "not proven" and st["G7"] == "partial"
    assert not gates.all_proven() and openclaw_lanes.writable_backends() == {"claude-cli"}


# --- AC-S7b: every defect fails closed -------------------------------------------------------------------------

def _mut(fn):
    def apply(root):
        doc = fk.valid_doc(root)
        fn(doc)
        return fk.resealed(doc)
    return apply


BAD = {
    "unknown top key": _mut(lambda d: d.update(extra=1)),
    "unknown gate key": _mut(lambda d: d["gates"][0].update(extra=1)),
    "duplicate gate": _mut(lambda d: d["gates"].append(copy.deepcopy(d["gates"][0]))),
    "short hash": _mut(lambda d: d["gates"][2].update(manifest_sha256="abc123")),
    "bad run id": _mut(lambda d: d.update(run_id="2026-11-01")),
    "run id differs": _mut(lambda d: d["gates"][2].update(run_id="20261102T000000Z")),
    "G0 anchor missing": _mut(lambda d: d["gates"][0].update(anchor=f"No such heading {fk.RUN_ID}")),
    "G0 anchor without the run id": _mut(lambda d: d["gates"][0].update(anchor="Phase 2 merge gate PASS")),
    "G1 slugs miss a preset slug": _mut(lambda d: d["gates"][1].update(confirmed_slugs=["gemini-3.8-flash-high"])),
    "G1 slug outside AGY_LANE_MODELS": _mut(lambda d: d["gates"][1]["confirmed_slugs"].append("gemini-9")),
    "G6 cost is not a count": _mut(lambda d: d["gates"][6].update(cost_per_step_tokens="lots")),
    "G7 hash differs": _mut(lambda d: d["gates"][7].update(ack_text_sha256="0" * 64)),
    "unknown cell": _mut(lambda d: d["cells"].update({"claude/openai/openrouter": copy.deepcopy(
        d["cells"]["claude/google/openrouter"])})),
    "status is not proven": _mut(lambda d: d["gates"][2].update(status="partial")),
    "wrong verification": _mut(lambda d: d["gates"][2].update(verification="V9")),
    "not UTC": _mut(lambda d: d["gates"][2].update(recorded_at="2026-11-01T10:30:00+02:00")),
    "manifest unsorted": _mut(lambda d: d["manifest"].reverse()),
    "manifest path escapes": _mut(lambda d: d["manifest"][0].update(relpath="../x")),
    "manifest digest differs": _mut(lambda d: d["manifest"][0].update(sha256="c" * 64)),
    "schema 2": _mut(lambda d: d.update(schema=2)),
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_a_defective_record_keeps_every_literal_status_and_the_refusal_says_why(root, name):
    fk.write(root, BAD[name](root))
    rec = ev.load(root)
    assert rec.status == "invalid", name
    assert _statuses() == LITERAL
    assert not gates.all_proven()
    assert "evidence file invalid" in gates.refusal()
    assert openclaw_lanes.writable_backends() == {"claude-cli"}
    assert not model_pins.agy_slug_confirmed("gemini-3.8-flash-high")
    assert compat.cell("claude", "google", compat.MULTI_OPENROUTER).verified is False


@pytest.mark.parametrize("field", ["id", "status", "verification", "run_id", "manifest_sha256", "recorded_at",
                                   "recorded_by"])
def test_a_proven_entry_that_lacks_a_required_field_is_not_proven(root, field):
    """AC-S7c."""
    doc = fk.valid_doc(root)
    del doc["gates"][3][field]
    fk.write(root, fk.resealed(doc))
    assert ev.load(root).status == "invalid"
    assert _statuses() == LITERAL


@pytest.mark.parametrize("gate,field", [("G0", "anchor"), ("G1", "confirmed_slugs"), ("G6", "cost_per_step_tokens"),
                                        ("G7", "ack_text_sha256")])
def test_the_extra_field_of_a_gate_is_required(root, gate, field):
    doc = fk.valid_doc(root)
    entry = next(e for e in doc["gates"] if e["id"] == gate)
    del entry[field]
    fk.write(root, fk.resealed(doc))
    assert ev.load(root).status == "invalid" and _statuses() == LITERAL


def test_malformed_oversize_symlinked_and_duplicate_key_files_fail_closed(root, tmp_path):
    path = ev.record_path(root)
    for payload in (b"not json", b"[]", b"{" + b" " * (ev.MAX_BYTES + 10) + b"}"):
        fk.write(root, payload)
        ev.clear_cache()
        assert ev.load(root).status == "invalid" and _statuses() == LITERAL
    text = json.dumps(fk.valid_doc(root))
    fk.write(root, text.replace('"schema": 1', '"schema": 1, "schema": 1'))
    ev.clear_cache()
    assert ev.load(root).status == "invalid"
    real = tmp_path / "elsewhere.json"
    real.write_text(text, encoding="utf-8")
    path.unlink()
    os.symlink(real, path)
    ev.clear_cache()
    rec = ev.load(root)
    assert rec.status == "invalid" and _statuses() == LITERAL


def test_a_hand_written_record_that_passes_the_schema_but_was_not_made_by_record_is_rejected(root):
    """Operator decision (a): schema-valid JSON without the seal `record` computes is refused."""
    doc = fk.valid_doc(root)
    handmade = {k: v for k, v in doc.items() if k != "seal"}
    handmade["seal"] = "0" * 64
    fk.write(root, handmade)
    rec = ev.load(root)
    assert rec.status == "invalid" and "seal" in rec.reason
    assert _statuses() == LITERAL
    no_seal = {k: v for k, v in doc.items() if k != "seal"}
    fk.write(root, no_seal)
    ev.clear_cache()
    assert ev.load(root).status == "invalid"
    other = copy.deepcopy(doc)
    other["generator"] = "hand"
    fk.write(root, fk.resealed(other))
    ev.clear_cache()
    assert "record" in ev.load(root).reason


def test_a_changed_google_text_un_proves_g7(root, monkeypatch):
    fk.write(root, fk.valid_doc(root))
    assert ev.load(root).status == "valid"
    monkeypatch.setitem(lanes_render.ACK_TEXT, "google", lanes_render.ACK_TEXT["google"] + " (edited)")
    ev.clear_cache()
    rec = ev.load(root)
    assert rec.status == "invalid" and "G7" in rec.reason


# --- git: untracked or different from HEAD fails closed ---------------------------------------------------------------

def test_in_a_git_root_an_untracked_record_fails_closed_and_a_committed_one_loads(tmp_path, monkeypatch):
    r = fk.make_root(tmp_path, git=True)
    fk.use(monkeypatch, r)
    fk.write(r, fk.valid_doc(r))
    rec = ev.load(r)
    assert rec.status == "invalid" and "not tracked" in rec.reason
    assert _statuses() == LITERAL and not gates.all_proven()
    fk.run_git(r, "add", ev.RECORD_REL)
    fk.run_git(r, "commit", "-q", "-m", "evidence")
    rec = ev.load(r)
    assert rec.status == "valid", rec.reason
    assert rec.author == "Op Erator" and len(rec.commit) == 40
    assert gates.all_proven()
    assert rec.commit[:8] in gates.summary_line() and "Op Erator" in gates.summary_line()
    # an edit after the commit (same schema, resealed) differs from HEAD
    other = fk.valid_doc(r)
    other["gates"][6]["cost_per_step_tokens"] = 99
    fk.write(r, fk.resealed(other))
    assert ev.load(r).status == "invalid" and "differs from HEAD" in ev.load(r).reason
    assert _statuses() == LITERAL


def test_the_cache_follows_the_file_and_the_loader_is_explicitly_cleared(root):
    assert ev.load(root).status == "absent"
    fk.write(root, fk.valid_doc(root))
    assert ev.load(root).status == "valid"
    fk.write(root, b"broken")
    assert ev.load(root).status == "invalid"
    ev.clear_cache()
    assert ev._CACHE == {}


def test_g1_must_cover_every_slug_the_presets_use(root):
    slugs = fk.preset_slugs(root)
    assert "gemini-3.8-flash-high" in slugs and "gemini-3.1-pro-low" in slugs
    assert set(slugs) <= set(model_pins.AGY_LANE_MODELS)

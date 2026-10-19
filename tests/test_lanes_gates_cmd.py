"""ADR-0010 S7: `ai-resources lanes gates` (read-only) and `lanes gates record` (terminal only, typed attestation).

A throwaway kit root and a throwaway probe-window directory under pytest's tmp_path. The record is never written to the
real repo, no agy or openclaw command runs, and nothing touches the network."""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess

import pytest

import agy_evidence_fakes as fk
from ai_resources import agy_gate_evidence as ev
from ai_resources import agy_lane_gates as gates
from ai_resources import lanes_cmd

STAMP = fk.RUN_ID


@pytest.fixture(autouse=True)
def _fresh():
    ev.clear_cache()
    yield
    ev.clear_cache()


@pytest.fixture
def kit(tmp_path, monkeypatch):
    r = fk.make_root(tmp_path, git=True)
    fk.use(monkeypatch, r)
    return r


@pytest.fixture
def window(tmp_path):
    d = tmp_path / "window" / STAMP
    (d / "v5").mkdir(parents=True)
    (d / "v6").mkdir()
    (d / "v5" / "models.txt").write_text("gemini-3.8-flash-high\n", encoding="utf-8")
    (d / "v6" / "run.log").write_text("sandbox ok\n", encoding="utf-8")
    return d


def answers(kit, *, only=None, cell=True, anchor=fk.ANCHOR, slugs=None, cost="12000"):
    """The scripted keyboard: one answer per prompt, in the order `record` asks."""
    slugs = ",".join(slugs or fk.preset_slugs(kit))
    queue = []
    for gid in (g.id for g in gates.GATES):
        if only is not None and gid not in only:
            queue.append("")
            continue
        queue.append(lanes_cmd.ATTEST[gid])
        if gid == "G0":
            queue.append(anchor)
        elif gid == "G1":
            queue.append(slugs)
        elif gid == "G6":
            queue.append(cost)
    queue.append(lanes_cmd.CELL_ATTEST if cell else "")
    it = iter(queue)
    return lambda prompt: next(it)


def record(kit, window, ask, *, yes=False, tty=True, now=None):
    lines = []
    a = argparse.Namespace(run_dir=str(window), yes=yes, gates_action="record")
    rc = lanes_cmd.cmd_gates_record(a, ask=ask, tty=tty, root=kit, out=lines.append,
                                    now=now or (lambda: __import__("datetime").datetime(2026, 11, 1, 10, 30,
                                                                                         tzinfo=__import__("datetime").timezone.utc)))
    return rc, lines


# --- status (read-only) -------------------------------------------------------------------------------------------

def test_gates_status_is_read_only_and_names_the_next_action(capsys, kit):
    before = sorted(p for p in kit.rglob("*") if p.is_file())
    assert lanes_cmd.main(["gates"]) == 0
    out = capsys.readouterr().out
    assert "gates 0/8 proven (G6, G7 partial)" in out
    assert "evidence record: absent" in out and "ai-resources lanes gates record" in out
    assert "G0  not proven" in out and "G7  partial" in out
    assert before == sorted(p for p in kit.rglob("*") if p.is_file())
    assert not ev.record_path(kit).exists()


def test_gates_status_shows_the_last_touching_commit_and_author(capsys, kit):
    fk.write(kit, fk.valid_doc(kit))
    fk.run_git(kit, "add", ev.RECORD_REL)
    fk.run_git(kit, "commit", "-q", "-m", "evidence")
    ev.clear_cache()
    assert lanes_cmd.main(["gates"]) == 0
    out = capsys.readouterr().out
    assert "on, gates 8/8 proven" in out
    assert "record last touched by commit" in out and "Op Erator" in out


def test_gates_status_shows_why_an_invalid_record_is_ignored(capsys, kit):
    fk.write(kit, b"{}")
    assert lanes_cmd.main(["gates"]) == 0
    out = capsys.readouterr().out
    assert "evidence file invalid" in out and "gates 0/8 proven" in out


# --- record: refusals write nothing ---------------------------------------------------------------------------------

def _nothing_written(kit):
    return not ev.record_path(kit).exists() and not list(ev.record_path(kit).parent.glob(".*tmp*"))


def test_record_refuses_yes(kit, window):
    rc, lines = record(kit, window, answers(kit), yes=True)
    assert rc != 0 and "--yes" in lines[0] and _nothing_written(kit)


def test_record_refuses_without_a_terminal(kit, window):
    rc, lines = record(kit, window, answers(kit), tty=False)
    assert rc != 0 and "terminal" in lines[0] and _nothing_written(kit)


def test_record_reads_the_real_stdin_and_refuses_piped_input(kit, window, monkeypatch):
    class Piped:
        def isatty(self):
            return False
    monkeypatch.setattr("sys.stdin", Piped())
    a = argparse.Namespace(run_dir=str(window), yes=False, gates_action="record")
    lines = []
    assert lanes_cmd.cmd_gates_record(a, ask=answers(kit), root=kit, out=lines.append) != 0
    assert "terminal" in lines[0] and _nothing_written(kit)


def test_record_refuses_a_root_that_is_not_a_git_work_tree(tmp_path, window, monkeypatch):
    plain = fk.make_root(tmp_path / "plain")
    fk.use(monkeypatch, plain)
    rc, lines = record(plain, window, answers(plain))
    assert rc != 0 and "git work tree" in lines[0] and _nothing_written(plain)


def test_record_refuses_a_window_that_is_not_named_by_its_stamp_or_holds_a_symlink(kit, window, tmp_path):
    odd = tmp_path / "window" / "today"
    odd.mkdir()
    (odd / "f").write_text("x", encoding="utf-8")
    rc, lines = record(kit, odd, answers(kit))
    assert rc != 0 and "UTC stamp" in lines[0] and _nothing_written(kit)
    os.symlink("/etc/hostname", window / "v5" / "link")
    rc, lines = record(kit, window, answers(kit))
    assert rc != 0 and "symlink" in lines[0] or "regular" in lines[0]
    assert _nothing_written(kit)


def test_record_writes_nothing_when_nothing_is_attested(kit, window):
    rc, lines = record(kit, window, answers(kit, only=set(), cell=False))
    assert rc != 0 and "nothing was attested" in lines[-1] and _nothing_written(kit)


def test_record_writes_nothing_when_g1_does_not_cover_the_preset_slugs(kit, window):
    rc, lines = record(kit, window, answers(kit, slugs=["gemini-3.8-flash-high"]))
    assert rc != 0 and "invalid" in lines[-1] and "gemini-4-argon-high" in lines[-1] and _nothing_written(kit)


def test_record_writes_nothing_for_an_anchor_that_is_not_an_evidence_heading(kit, window):
    rc, lines = record(kit, window, answers(kit, anchor=f"Made up {STAMP}"))
    assert rc != 0 and _nothing_written(kit)


# --- record: the happy path -------------------------------------------------------------------------------------------

def test_record_writes_a_record_the_loader_accepts_only_after_the_operator_commits_it(kit, window):
    rc, lines = record(kit, window, answers(kit))
    assert rc == 0, lines
    text = "\n".join(lines)
    assert "git add scripts/ai_resources/data/agy_gate_evidence.json && git commit -m" in text
    assert "nothing was committed" in text
    doc = json.loads(ev.record_path(kit).read_text(encoding="utf-8"))
    assert doc["run_id"] == STAMP and doc["generator"] == ev.GENERATOR and doc["seal"] == ev.seal_of(doc)
    assert {e["id"] for e in doc["gates"]} == {g.id for g in gates.GATES}
    assert [m["relpath"] for m in doc["manifest"]] == ["v5/models.txt", "v6/run.log"]
    assert set(doc["cells"]) == {"claude/google/openrouter"}
    assert not (ev.record_path(kit).stat().st_mode & 0o022)                  # not group or world writable
    # uncommitted: the lane stays closed
    ev.clear_cache()
    assert ev.load(kit).status == "invalid" and not gates.all_proven()
    fk.run_git(kit, "add", ev.RECORD_REL)
    fk.run_git(kit, "commit", "-q", "-m", "evidence")
    ev.clear_cache()
    assert ev.load(kit).status == "valid" and gates.all_proven()


def test_record_with_a_partial_attestation_proves_only_those_gates(kit, window):
    rc, _ = record(kit, window, answers(kit, only={"G2"}, cell=False))
    assert rc == 0
    fk.run_git(kit, "add", ev.RECORD_REL)
    fk.run_git(kit, "commit", "-q", "-m", "evidence")
    ev.clear_cache()
    assert [g.id for g in gates.unproven()] == ["G0", "G1", "G3", "G4", "G5", "G6", "G7"]


def test_record_shows_the_google_text_and_its_hash_before_the_g7_approval(kit, window):
    from ai_resources.setup import lanes_render
    rc, lines = record(kit, window, answers(kit))
    assert rc == 0
    text = "\n".join(lines)
    assert lanes_render.ACK_TEXT["google"] in text and ev.ack_text_sha256() in text


def test_a_wrong_attestation_text_leaves_the_stage_unproven(kit, window):
    queue = iter(["pass v5"] + [""] * 20)
    rc, lines = record(kit, window, lambda prompt: next(queue))
    assert rc != 0 and _nothing_written(kit)


def test_record_never_runs_agy_openclaw_or_any_other_process(kit, window, monkeypatch):
    """AC-S7f: the command is arithmetic and file writes; the runner of every module stays unused."""
    calls = []
    real_run, real_popen = subprocess.run, subprocess.Popen
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or real_run(*a, **k))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: calls.append(a) or real_popen(*a, **k))
    monkeypatch.setattr(lanes_cmd, "default_runner", lambda *a, **k: calls.append(a) or (1, "", ""))
    rc, _ = record(kit, window, answers(kit))
    assert rc == 0
    flat = " ".join(str(c) for c in calls)
    assert "agy" not in flat.replace("agy_gate_evidence", "") and "openclaw" not in flat
    assert calls == []


def test_the_parser_accepts_gates_and_record_only_with_a_run_dir():
    p = lanes_cmd.build_parser()
    assert p.parse_args(["gates"]).action == "gates"
    assert p.parse_args(["gates", "record", "--run-dir", "x"]).gates_action == "record"
    with pytest.raises(SystemExit):
        p.parse_args(["gates", "record"])
    assert p.parse_args(["gates", "record", "--run-dir", "x", "--yes"]).yes is True


def test_the_record_path_is_forbidden_to_lane_children_and_watched(tmp_path):
    from ai_resources import roles_policy as rp
    assert lanes_cmd._forbidden_root("scripts/ai_resources/data/agy_gate_evidence.json") == "agy_gate_evidence.json"
    assert lanes_cmd._forbidden_root("pkg/data/agy_gate_evidence.json")
    assert rp.AGY_GATE_EVIDENCE_REL in rp.TRIPWIRE_PATHS
    assert rp.AGY_GATE_EVIDENCE_REL.endswith(rp.LANE_WRITE_FORBIDDEN[-1])
    assert str(lanes_cmd._tripwires()[rp.AGY_GATE_EVIDENCE_REL])

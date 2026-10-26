"""Regression tests for the scorecard defects D1-D7 found by the independent verifier (one section per defect)."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from ai_resources import cli, scorecard

from test_kit_script_size_gate import IMPL_JS, IMPL_RESPONSES, PLAN_JS, PLAN_RESPONSES, TEST_OK, run

REPO = Path(__file__).resolve().parents[1]
RULES = scorecard.load_rules()
CASES = json.loads((REPO / "evals" / "scorecard" / "cases.json").read_text(encoding="utf-8"))
DIMS = RULES["dimensions"]
ZERO = dict.fromkeys(DIMS, 0)
_NODE_MARKS = (pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed"), pytest.mark.node_scripts)


def needs_node(fn):
    for mark in _NODE_MARKS:
        fn = mark(fn)
    return fn


SKILL = (REPO / "skills" / "kit-orchestration" / "references" / "scorecard.md").read_text(encoding="utf-8")


def _handoff(tmp_path: Path) -> Path:
    path = tmp_path / ".agent-output" / "handoff.md"
    path.parent.mkdir()
    shutil.copy(REPO / "handoff.md.template", path)
    return path


def _key(path: Path) -> str:
    line = next(l for l in path.read_text(encoding="utf-8").splitlines() if l.startswith("scorecard:"))
    return json.loads(line.split(":", 1)[1].split("#", 1)[0].strip())


# --- D1: the scorecard and escalations are recorded in handoff.md by code --------------------------------------
def test_d1_cli_records_the_card_in_the_handoff(tmp_path, capsys):
    path = _handoff(tmp_path)
    scores = {**ZERO, "resumability": 2, "uncertainty": 2}
    assert cli.main(["scorecard", json.dumps(scores), "--handoff", str(path)]) == 0
    assert _key(path) == "STANDARD res2 blast0 risk0 unc2 ver0 spr0"
    assert "STANDARD" in capsys.readouterr().out


def test_d1_cli_creates_the_handoff_from_the_template(tmp_path):
    path = tmp_path / "new" / "handoff.md"
    assert cli.main(["scorecard", json.dumps({**ZERO, "risk": 3}), "--handoff", str(path)]) == 0
    assert _key(path).startswith("FULL ") and "workflow:" in path.read_text(encoding="utf-8")


def test_d1_escalation_is_appended_to_the_record(tmp_path):
    path = _handoff(tmp_path)
    cli.main(["scorecard", json.dumps({**ZERO, "resumability": 2}), "--handoff", str(path)])
    assert cli.main(["scorecard", "escalate", "standard", "2", "0", "--handoff", str(path), "--reason", "tests failed twice"]) == 0
    assert _key(path).endswith("-> FULL (tests failed twice)")
    cli.main(["scorecard", "escalate", "standard", "0", "0", "--handoff", str(path)])  # no escalation: nothing appended
    assert _key(path).count("->") == 1


def test_d1_docs_name_the_recording_and_define_every_abbreviation():
    handoff_ref = (REPO / "skills" / "kit-orchestration" / "references" / "handoff.md").read_text(encoding="utf-8")
    template = (REPO / "handoff.md.template").read_text(encoding="utf-8")
    line = next(l for l in template.splitlines() if l.startswith("scorecard:"))
    for dim, short in scorecard.ABBREV.items():
        assert f"{short}={dim}" in line.replace(" ", "") or f"{short}={dim}" in line, (dim, line)
        assert f"| `{short}` | {dim} |" in handoff_ref and f"`{short}` = {dim}" in SKILL
    assert "--handoff" in handoff_ref and "scorecard" in handoff_ref
    assert not any(tok in line for tok in (" r1 ", " b1 ", " k0 ", " u2 ", " v1 ", " s0"))


# --- D2: a bump records the effective mode and the reason ---------------------------------------------------------
LITE_SCORES = {**ZERO, "blast_radius": 1}


def test_d2_python_bump_records_effective_mode():
    card = scorecard.bump_for_risk(scorecard.score(LITE_SCORES), ["2: security"])
    assert (card["mode"], card["scored_mode"]) == ("standard", "lite")
    assert "risk items named" in card["bump_reason"] and "verifier" in card["roster"]
    assert scorecard.bump_for_risk(scorecard.score(LITE_SCORES), [])["mode"] == "lite"


def test_d2_cli_prints_and_records_the_bump(tmp_path, capsys):
    path = _handoff(tmp_path)
    assert cli.main(["scorecard", json.dumps(LITE_SCORES), "--risk-item", "2: security", "--handoff", str(path)]) == 0
    out = capsys.readouterr().out
    assert "mode: STANDARD" in out and "scored LITE" in out
    assert "bumped" in _key(path)


# /kit-plan classifies; /kit-implement reads the scores the plan recorded in its Sizing block (it never classifies).
SCRIPT_CASES = [(PLAN_JS, PLAN_RESPONSES, {"goal": "g"}, "classify", {}),
                (IMPL_JS, IMPL_RESPONSES, {"goal": "g", "plan_path": "p.md"}, "read-sizing", {"found": True})]


@pytest.mark.parametrize("script,responses,args,key,extra", SCRIPT_CASES)
@needs_node
def test_d2_scripts_record_effective_mode_and_reason(tmp_path, script, responses, args, key, extra):
    responses = {**responses, key: {"reason": "r", "scores": LITE_SCORES, "risk_items": ["2: security"], **extra}}
    _, result = run(script, tmp_path, args, responses)
    card = result["scorecard"]
    assert result["size"] == "light" and card["mode"] == "standard" and card["scored_mode"] == "lite"
    assert "risk items named" in card["bump_reason"] and "verifier" in card["roster"]
    assert result["bump_reason"] == card["bump_reason"] and "bumped" in result["scorecard_line"]


# --- D3: escalation is enforced in code where the data is --------------------------------------------------------
def test_d3_gate_command_escalates_from_numbers(capsys):
    for argv, want in [(["gate", "lite", "--failures", "2", "--diff-lines", "0"], "standard"),
                       (["gate", "lite", "--failures", "0", "--diff-lines", "150"], "standard"),
                       (["gate", "lite", "--failures", "4", "--diff-lines", "0"], "full"),
                       (["gate", "lite", "--diff-lines", "400"], "full"),
                       (["gate", "lite", "--failures", "1", "--diff-lines", "10"], "lite")]:
        assert cli.main(["scorecard", *argv]) == 0
        assert capsys.readouterr().out.strip() == want, argv


def test_d3_gate_measures_the_diff_itself(tmp_path, capsys):
    git = lambda *a: subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *a], check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    (tmp_path / "f.txt").write_text("x\n")
    git("add", "."); git("commit", "-q", "-m", "base")
    (tmp_path / "f.txt").write_text("".join(f"line {i}\n" for i in range(160)))
    assert scorecard.diff_lines("main", cwd=str(tmp_path)) == 161
    assert scorecard.diff_lines("HEAD", cwd=str(tmp_path)) == 161
    path = _handoff(tmp_path)
    old = Path.cwd()
    import os
    os.chdir(tmp_path)
    try:
        assert cli.main(["scorecard", "gate", "lite", "--base", "HEAD", "--handoff", str(path)]) == 0
    finally:
        os.chdir(old)
    assert capsys.readouterr().out.strip() == "standard" and "-> STANDARD" in _key(path)


@needs_node
def test_d3_inline_run_reenters_and_escalates_lite_to_standard_and_full(tmp_path):
    lite = {"reason": "r", "scores": CASES[0]["scores"]}
    for extra, size in [({"failures": 2}, "light"), ({"diff_lines": 200}, "light"), ({"failures": 4}, "full"), ({"diff_lines": 400}, "full")]:
        labels, result = run(IMPL_JS, tmp_path, {"goal": "g", "size": "inline", "scorecard": scorecard.score(CASES[0]["scores"]), **extra},
                             {**IMPL_RESPONSES, "classify": lite})
        assert result["size"] == size and "implement" in labels, (extra, labels)
        assert result["escalated_from"] == "inline" and result["scorecard"]["escalations"], extra
        assert "-> " in result["scorecard_line"]
    labels, result = run(IMPL_JS, tmp_path, {"goal": "g", "size": "inline", "failures": 1, "diff_lines": 10}, IMPL_RESPONSES)
    assert labels == [] and result["status"] == "inline"


def test_d3_doc_states_code_enforced_versus_instruction():
    assert "Code-enforced versus instruction" in SKILL
    assert "ai-resources scorecard gate" in SKILL


# --- D4: an escalated run gets the new mode's test rounds -------------------------------------------------------
@needs_node
def test_d4_escalation_mid_loop_grants_full_test_rounds(tmp_path):
    failing = {"passed": False, "command": "pytest", "failures": ["t::x"]}
    responses = {**IMPL_RESPONSES, "test-gate": failing, "fix-tests": IMPL_RESPONSES["implement"]}
    labels, result = run(IMPL_JS, tmp_path, {"goal": "g", "size": "light"}, responses)
    assert result["size"] == "full" and result["escalated_from"] == "light"
    assert "fix-tests:2" in labels and "fix-tests:3" not in labels
    full_only = run(IMPL_JS, tmp_path, {"goal": "g", "size": "full"}, responses)[0]
    assert full_only.count("fix-tests:1") == 1 and "fix-tests:2" in full_only


@needs_node
def test_d4_passing_second_round_still_gets_a_rerun_after_escalation(tmp_path):
    labels, result = run(IMPL_JS, tmp_path, {"goal": "g", "size": "light"}, {**IMPL_RESPONSES, "test-gate": TEST_OK})
    assert "fix-tests:1" not in labels and result["escalated_from"] is None


# --- D5: every workflow skill and YAML scores first ------------------------------------------------------------
def test_d5_every_workflow_skill_scores_first():
    skills = sorted((REPO / "skills").glob("workflow-*/SKILL.md"))
    assert len(skills) >= 19  # workflow-odd was folded into /kit-plan and /kit-implement
    for skill in skills:
        text = skill.read_text(encoding="utf-8")
        assert "Score first" in text and '"Scorecard"' in text, skill


def test_d5_no_workflow_yaml_classifies_with_the_old_task_size():
    for yaml_file in (REPO / "workflows").glob("*.yaml"):
        text = yaml_file.read_text(encoding="utf-8")
        assert 'Classify with the resume test' not in text, yaml_file
    assert not (REPO / "workflows" / "odd.workflow.yaml").exists()  # folded into /kit-plan and /kit-implement


def test_d5_generated_skills_are_in_sync_with_the_generator():
    import yaml
    from ai_resources import generate
    for wf in (REPO / "workflows").glob("*.workflow.yaml"):
        doc = yaml.safe_load(wf.read_text(encoding="utf-8"))
        want = generate._workflow_skill_md(wf.name, doc)
        assert (REPO / "skills" / f"workflow-{doc['name']}" / "SKILL.md").read_text(encoding="utf-8") == want, wf.name


# --- D6: invalid scores mean FULL everywhere ---------------------------------------------------------------------
BAD = [{**ZERO, "risk": 9}, {**ZERO, "risk": True}, {"risk": 1}, {**ZERO, "bogus": 1}, "not a dict", [1, 2], None]


@pytest.mark.parametrize("bad", BAD, ids=lambda b: repr(b)[:30])
def test_d6_python_invalid_scores_are_full(bad):
    card = scorecard.score_or_full(bad) if bad is not None else scorecard.score_or_full(None)
    assert card["mode"] == "full" and card["handoff"] and card["roster"] == RULES["base"]["full"]
    # scores present but invalid beat a bare legacy mode
    if bad is not None:
        assert scorecard.score_or_full(bad, legacy_mode="lite")["mode"] == "full"


def test_d6_python_bare_legacy_mode_without_scores_is_honoured():
    assert scorecard.score_or_full(None, "inline")["mode"] == "lite"
    assert scorecard.score_or_full(None, "light")["mode"] == "standard"
    assert scorecard.score_or_full(None, "nonsense")["mode"] == "full"


def test_d6_cli_invalid_scores_print_a_full_card(capsys):
    for arg in ['{"risk": 9}', "not json", json.dumps({**ZERO, "risk": 4})]:
        assert cli.main(["scorecard", arg]) == 0
        cap = capsys.readouterr()
        assert "mode: FULL" in cap.out and "FULL" in cap.err
    assert cli.main(["scorecard"]) == 2  # usage errors still fail


@pytest.mark.parametrize("script,responses,args,key,extra", SCRIPT_CASES)
@needs_node
def test_d6_scripts_bare_legacy_mode_with_invalid_scores_is_full(tmp_path, script, responses, args, key, extra):
    for scores in ({**ZERO, "risk": 9}, {}, "x"):
        resp = {**responses, key: {"mode": "inline", "reason": "r", "scores": scores, **extra}}
        labels, result = run(script, tmp_path, args, resp)
        assert result["size"] == "full", scores
    ok = run(script, tmp_path, args, {**responses, key: {"mode": "inline", "reason": "r", **extra}})[1]
    assert ok["size"] == "inline"  # no scores at all: the legacy mode still works


@pytest.mark.parametrize("script,responses,args,key,extra", SCRIPT_CASES)
@needs_node
def test_d6_scripts_no_scores_and_no_answer_is_light_not_full(tmp_path, script, responses, args, key, extra):
    # Decision: invalid scores are FULL, but a classifier that failed or answered nothing keeps main's light default.
    resp = {k: v for k, v in responses.items() if k != key}
    assert run(script, tmp_path, args, resp)[1]["size"] == "light"


def test_d6_docs_state_the_fallback():
    assert "Invalid or missing scores mean FULL" in SKILL
    assert "Invalid scores mean FULL" in (REPO / "skills" / "kit-orchestration" / "SKILL.md").read_text(encoding="utf-8")


# --- D7: the LITE threshold ----------------------------------------------------------------------------------------
def test_d7_a_lone_verifiability_3_is_standard_and_a_lone_2_is_lite():
    assert scorecard.score({**ZERO, "verifiability": 3})["mode"] == "standard"
    assert scorecard.score({**ZERO, "verifiability": 2})["mode"] == "lite"
    assert scorecard.score({**ZERO, "verifiability": 3})["roster"] == ["implementer", "tester", "verifier"]
    assert RULES["lite"]["max_verifiability"] == 2


@needs_node
def test_d7_scripts_agree(tmp_path):
    for ver, size in ((3, "light"), (2, "inline")):
        resp = {**PLAN_RESPONSES, "classify": {"reason": "r", "scores": {**ZERO, "verifiability": ver}}}
        assert run(PLAN_JS, tmp_path, {"goal": "g"}, resp)[1]["size"] == size


def test_d7_docs_state_the_decision():
    assert "`verifiability` of 3" in SKILL and "never LITE" in SKILL
    assert "`verifiability` <= 2" in SKILL


# --- fixtures: the real split is 3 LITE / 5 STANDARD / 4 FULL and the docs cite it ---------------------------
def test_fixture_split_matches_the_docs():
    split = {m: sum(c["mode"] == m for c in CASES) for m in scorecard.MODES}
    assert split == {"lite": 3, "standard": 5, "full": 4} and len(CASES) == 12
    docs = (REPO / "docs" / "orchestration.md").read_text(encoding="utf-8")
    assert "12 sample requests (3 LITE, 5 STANDARD, 4 FULL)" in docs

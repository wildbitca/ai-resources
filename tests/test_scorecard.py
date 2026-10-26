"""The task scorecard maps six 0-3 scores to a mode and roster deterministically (skills/kit-orchestration "Scorecard")."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ai_resources import cli, scorecard

REPO = Path(__file__).resolve().parents[1]
CASES = json.loads((REPO / "evals" / "scorecard" / "cases.json").read_text(encoding="utf-8"))
RULES = scorecard.load_rules()


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_fixture_maps_to_expected_mode_and_roster(case):
    card = scorecard.score(case["scores"])
    assert (card["mode"], card["roster"], card["handoff"]) == (case["mode"], case["roster"], case["handoff"])


def test_fixtures_cover_every_mode():
    assert {c["mode"] for c in CASES} == set(scorecard.MODES)


def test_lite_has_no_roster_and_no_handoff_but_tests_stay_mandatory():
    card = scorecard.score(CASES[0]["scores"])
    assert card["roster"] == [] and not card["handoff"]
    skill = (REPO / "skills" / "kit-orchestration" / "SKILL.md").read_text(encoding="utf-8")
    assert "tests are mandatory in every mode" in skill.lower()
    ref = (REPO / "skills" / "kit-orchestration" / "references" / "scorecard.md").read_text(encoding="utf-8")
    assert "Tests are mandatory in every mode" in ref and "references/scorecard.md" in skill


def test_triggers_add_specific_roles_to_standard():
    base = dict.fromkeys(RULES["dimensions"], 0)
    base["resumability"] = 1  # standard needs something; keep it out of lite via risk below
    for dim, level, role in [("risk", 2, "security-auditor"), ("uncertainty", 2, "software-architect"),
                             ("spread", 2, "software-architect"), ("blast_radius", 2, "code-reviewer")]:
        card = scorecard.score({**base, dim: level})
        assert card["mode"] == "standard" and role in card["roster"], (dim, card)
        below = scorecard.score({**base, dim: level - 1})
        assert role not in below["roster"]


def test_scoring_is_monotone_in_mode():
    import itertools
    dims = RULES["dimensions"]
    for vals in itertools.product((0, 3), repeat=len(dims)):
        s = dict(zip(dims, vals))
        for d in dims:
            if s[d] < 3:
                higher = scorecard.score({**s, d: s[d] + 1})["mode"]
                assert scorecard._ORDER[higher] >= scorecard._ORDER[scorecard.score(s)["mode"]], (s, d)


def test_invalid_scores_are_rejected():
    ok = dict.fromkeys(RULES["dimensions"], 0)
    for bad in [{**ok, "risk": 4}, {**ok, "risk": -1}, {**ok, "risk": True}, {**ok, "risk": "1"}]:
        with pytest.raises(ValueError):
            scorecard.score(bad)
    with pytest.raises(ValueError):
        scorecard.score({k: v for k, v in ok.items() if k != "risk"})
    with pytest.raises(ValueError):
        scorecard.score({**ok, "bogus": 1})


def test_escalation_never_downgrades_and_triggers_on_failures_or_diff():
    e = scorecard.escalate
    assert e("lite", 0, 10) == "lite"
    assert e("lite", 1, 149) == "lite"
    assert e("lite", 2, 0) == "standard"
    assert e("lite", 0, 150) == "standard"
    assert e("lite", 4, 0) == "full" and e("lite", 0, 400) == "full"
    assert e("standard", 1, 399) == "standard"
    assert e("standard", 2, 0) == "full"
    assert e("full", 99, 9999) == "full"
    assert e("standard", 0, 0) == "standard"  # never lower
    with pytest.raises(ValueError):
        e("huge", 0, 0)


def test_every_roster_role_has_a_tier_and_is_a_real_agent():
    roles = {p.stem for p in (REPO / "agents" / "roles").glob("*.md")}
    for role in RULES["base"]["full"]:
        assert role in RULES["tiers"]
        assert role in roles, role


def test_render_prints_scores_mode_roster_and_rationale():
    text = scorecard.render(scorecard.score(CASES[5]["scores"]))
    assert "mode: FULL" in text and "security-auditor (strong)" in text and "| risk | 3 |" in text and "rationale:" in text


def test_cli_scorecard_and_escalate(capsys):
    assert cli.main(["scorecard", json.dumps(CASES[2]["scores"])]) == 0
    assert "mode: STANDARD" in capsys.readouterr().out
    assert cli.main(["scorecard", "escalate", "lite", "2", "0"]) == 0
    assert capsys.readouterr().out.strip() == "standard"


def test_scripts_embed_the_same_rules_as_the_json():
    for name in ("kit-plan.js", "kit-implement.js"):
        src = (REPO / "workflows" / "scripts" / name).read_text(encoding="utf-8")
        m = re.search(r"^const SCORECARD = (\{.*\})$", src, re.M)
        assert m, name
        assert json.loads(m.group(1)) == RULES, name


def test_reference_table_matches_the_rules():
    ref = (REPO / "skills" / "kit-orchestration" / "references" / "scorecard.md").read_text(encoding="utf-8")
    esc = RULES["escalation"]
    assert f"fail twice, or the diff reaches {esc['lite']['standard']['diff_lines']} " in ref
    assert f"diff reaches {esc['standard']['full']['diff_lines']} lines" in ref
    for role, tier in RULES["tiers"].items():
        assert role in ref


# --- the workflow scripts apply the same rules (node) --------------------------------------------------------------
import shutil  # noqa: E402

from test_kit_script_size_gate import IMPL_JS, IMPL_RESPONSES, PLAN_JS, PLAN_RESPONSES, TEST_OK, run  # noqa: E402

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
SIZE_OF = {"lite": "inline", "standard": "light", "full": "full"}


@needs_node
@pytest.mark.node_scripts
@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_kit_plan_script_maps_scores_like_the_python_module(case, tmp_path):
    responses = {**PLAN_RESPONSES, "classify": {"reason": "r", "scores": case["scores"]}}
    labels, result = run(PLAN_JS, tmp_path, {"goal": case["request"]}, responses)
    assert labels[0] == "classify"
    assert result["size"] == SIZE_OF[case["mode"]]
    assert result["scorecard"]["roster"] == case["roster"] and result["scorecard"]["handoff"] == case["handoff"]


@needs_node
@pytest.mark.node_scripts
def test_invalid_scores_fall_back_to_full(tmp_path):
    bad = {**dict.fromkeys(RULES["dimensions"], 0), "risk": 9}
    responses = {**PLAN_RESPONSES, "classify": {"reason": "r", "scores": bad}}
    _, result = run(PLAN_JS, tmp_path, {"goal": "g"}, responses)
    assert result["size"] == "full" and result["scorecard"] is None


@needs_node
@pytest.mark.node_scripts
def test_new_flag_names_override_the_scorecard(tmp_path):
    for flag, size in [("--lite", "inline"), ("--standard", "light"), ("--full", "full")]:
        labels, result = run(PLAN_JS, tmp_path, f"do it {flag}", PLAN_RESPONSES)
        assert result["size"] == size and "classify" not in labels


@needs_node
@pytest.mark.node_scripts
def test_implement_lite_card_still_requires_tests_and_spawns_nothing(tmp_path):
    case = CASES[0]
    responses = {**IMPL_RESPONSES, "classify": {"reason": "r", "scores": case["scores"]}}
    labels, result = run(IMPL_JS, tmp_path, {"goal": "g"}, responses)
    assert labels == ["classify"] and result["status"] == "inline" and "Tests are mandatory" in result["next"]


@needs_node
@pytest.mark.node_scripts
def test_implement_escalates_light_to_full_when_tests_fail_twice(tmp_path):
    failing = {"passed": False, "command": "pytest", "failures": ["t::x"]}
    responses = {**IMPL_RESPONSES, "test-gate": failing, "fix-tests": IMPL_RESPONSES["implement"]}
    labels, result = run(IMPL_JS, tmp_path, {"goal": "g", "size": "light"}, responses)
    assert result["escalated_from"] == "light" and result["size"] == "full"
    assert result["status"] == "needs-attention" and "sign-off" in labels and "review:security" in labels


@needs_node
@pytest.mark.node_scripts
def test_implement_escalates_on_a_growing_diff_and_never_downgrades(tmp_path):
    big = {**IMPL_RESPONSES["implement"], "diff_lines": 450}
    labels, result = run(IMPL_JS, tmp_path, {"goal": "g", "size": "light"}, {**IMPL_RESPONSES, "implement": big, "test-gate": TEST_OK})
    assert result["escalated_from"] == "light" and result["size"] == "full"
    labels, result = run(IMPL_JS, tmp_path, {"goal": "g", "size": "full"}, {**IMPL_RESPONSES, "implement": {**big, "diff_lines": 0}})
    assert result["size"] == "full" and result["escalated_from"] is None

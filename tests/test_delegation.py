"""The delegation rule (skills/kit-orchestration "Delegation"): resumability, then divisibility, then a named reason.

The Python module and the JS block of sizing-core.js.inc are twins; the parity test runs both over the same cases.
"""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from ai_resources import cli, scorecard

import sizing_snapshot as snap

REPO = Path(__file__).resolve().parents[1]
REASONS = scorecard.load_rules()["delegation"]["reasons"]
W, E, V = "parallel-writers-on-disjoint-units", "explore-without-polluting-main-context", "independent-verification"

A = {"id": "A", "files": ["src/a.py", "tests/test_a.py"]}
B = {"id": "B", "files": ["src/b.py", "tests/test_b.py"]}
OVERLAP = {"id": "C", "files": ["src"]}
BLANK = {"id": "D", "files": []}

CASES = {
    "lite-is-inline": ("lite", dict(units=[A, B], reason=W)),
    "lite-security": ("lite", dict(security_or_merge=True)),
    "disjoint-writers": ("full", dict(units=[A, B], reason=W)),
    "overlap-refuses-writers": ("full", dict(units=[A, OVERLAP], reason=W)),
    "dependent-units": ("full", dict(units=[A, {**B, "depends_on": ["A"]}], reason=W)),
    "one-unit-large": ("full", dict(units=[A], reason=W)),
    "doubt-no-model": ("standard", dict(units=[A, BLANK], reason=W)),
    "doubt-model-yes": ("standard", dict(units=[A, BLANK], reason=W, model={"divisible": True, "evidence": "ok", "tokens": 180})),
    "doubt-model-no-tokens": ("standard", dict(units=[A, BLANK], reason=W, model={"divisible": True})),
    "doubt-model-no": ("standard", dict(units=[A, BLANK], reason=W, model={"divisible": False, "evidence": "shared", "tokens": 90})),
    "explore": ("standard", dict(units=[A], reason=E)),
    "verify": ("standard", dict(reason=V)),
    "no-reason": ("standard", dict(units=[A, B])),
    "unknown-reason": ("standard", dict(units=[A, B], reason="because")),
    "security-no-reason": ("full", dict(units=[A], security_or_merge=True)),
    "security-writers-overlap": ("full", dict(units=[A, OVERLAP], reason=W, security_or_merge=True)),
}


DIMS = scorecard.load_rules()["dimensions"]


def decide(mode, kw=None, **more):
    return scorecard.decide_delegation(mode, **{**(kw or {}), **more})


def test_the_three_reasons_are_the_documented_ones():
    assert REASONS == [E, W, V]


def test_resumable_work_stops_at_layer_one_and_pays_nothing():
    d = decide(*CASES["lite-is-inline"])
    assert (d["layer"], d["delegate"], d["divisible"], d["cost_tokens"]) == ("resumability", False, None, 0)


def test_divisible_work_with_a_named_reason_gets_parallel_writers():
    d = decide(*CASES["disjoint-writers"])
    assert (d["layer"], d["divisible"], d["reason"], d["parallel_writers"]) == ("reason", True, W, True)


@pytest.mark.parametrize("case", ["overlap-refuses-writers", "dependent-units", "one-unit-large"])
def test_work_that_does_not_divide_stays_with_one_agent_even_when_large(case):
    d = decide(*CASES[case])
    assert d["divisible"] is False and d["delegate"] is False and d["parallel_writers"] is False and d["layer"] == "divisibility"


def test_in_doubt_means_one_agent_and_no_model_cost_without_a_call():
    d = decide(*CASES["doubt-no-model"])
    assert d["divisible"] is False and d["parallel_writers"] is False and d["cost_source"] == "none" and "in doubt" in d["evidence"]


def test_the_model_answer_is_used_only_when_the_heuristic_is_doubtful_and_its_cost_is_recorded():
    d = decide(*CASES["doubt-model-yes"])
    assert d["divisible"] is True and d["parallel_writers"] is True and (d["cost_tokens"], d["cost_source"]) == (180, "model")
    d = decide(*CASES["doubt-model-no-tokens"])
    assert (d["cost_tokens"], d["cost_source"]) == (None, "unavailable")
    d = decide("full", dict(units=[A, B], reason=W, model={"divisible": False, "tokens": 5}))
    assert d["divisible"] is True and d["cost_tokens"] == 0, "a certain heuristic ignores the model"


def test_without_a_named_reason_nothing_is_delegated():
    for case in ("no-reason", "unknown-reason"):
        d = decide(*CASES[case])
        assert d["delegate"] is False and d["reason"] is None


def test_other_reasons_delegate_without_writers():
    for case, reason in (("explore", E), ("verify", V)):
        d = decide(*CASES[case])
        assert d["delegate"] is True and d["reason"] == reason and d["parallel_writers"] is False


@pytest.mark.parametrize("case", ["lite-security", "security-no-reason", "security-writers-overlap"])
def test_independent_verification_is_never_skipped_for_security_or_merge_work(case):
    d = decide(*CASES[case])
    assert d["verifier"] is True and d["delegate"] is True and d["reason"] in (V, W)


def test_heuristic_overlap_includes_directories_and_ignores_trailing_slashes():
    assert not scorecard.divisibility([{"id": 1, "files": ["a/"]}, {"id": 2, "files": ["a/b.py"]}])["divisible"]
    assert scorecard.divisibility([{"id": 1, "files": ["a/x.py"]}, {"id": 2, "files": ["a/y.py"]}])["divisible"]
    assert scorecard.divisibility("junk")["certain"] and not scorecard.divisibility(None)["divisible"]


def test_the_model_prompt_carries_only_units_and_files():
    prompt = scorecard.divisibility_prompt([A, {**B, "depends_on": ["A"]}])
    assert "A: src/a.py, tests/test_a.py" in prompt and "B: src/b.py, tests/test_b.py (after A)" in prompt
    assert "plan" not in prompt.split("\n", 1)[1].lower()


def test_the_decision_is_recorded_in_the_card_and_its_one_line_form():
    card = scorecard.with_delegation(scorecard.score_or_full(dict.fromkeys(scorecard.load_rules()["dimensions"], 1)),
                                     decide(*CASES["doubt-model-yes"]))
    line = scorecard.compact(card)
    assert "(delegation: parallel-writers-on-disjoint-units, layer reason, divisible yes, cost 180)" in line
    assert "- delegation: " in scorecard.render(card)
    plain = scorecard.score_or_full(dict.fromkeys(scorecard.load_rules()["dimensions"], 1))
    assert "delegation" not in scorecard.compact(plain), "a card without a decision keeps its 2.9.0 form"


def test_cli_records_the_decision_in_the_handoff(tmp_path, capsys):
    handoff = tmp_path / "handoff.md"
    scores = json.dumps(dict.fromkeys(scorecard.load_rules()["dimensions"], 1))
    units = json.dumps([A, B])
    assert cli.main(["scorecard", scores, "--units", units, "--delegate-reason", W, "--handoff", str(handoff)]) == 0
    assert "layer reason, divisible yes" in handoff.read_text(encoding="utf-8")
    assert "- delegation: parallel-writers-on-disjoint-units" in capsys.readouterr().out
    assert cli.main(["scorecard", scores, "--units", "not json"]) == 2


SCORES = {"lite": dict.fromkeys(DIMS, 0), "standard": {**dict.fromkeys(DIMS, 0), "verifiability": 3},
          "full": {**dict.fromkeys(DIMS, 0), "risk": 3}}


def _js_decision(mode, kw):
    """What kit-implement.js records on the card for the same inputs, run through the node harness."""
    args = {"goal": "g", "plan_path": "p.md"}
    for src, dst in (("units", "unit_list"), ("reason", "delegation_reason"), ("security_or_merge", "security_or_merge")):
        if src in kw:
            args[dst] = kw[src]
    model = kw.get("model")
    answer = None if model is None else {"divisible": model["divisible"], "evidence": model.get("evidence", "")}
    if answer is not None and model.get("tokens") is not None:
        answer["usage"] = {"total_tokens": model["tokens"]}
    responses = {**snap.IMPL_RESPONSES, "read-sizing": {"found": True, "scores": SCORES[mode]}, "divisibility": answer}
    out = snap.run_js(snap.IMPL_JS, args, responses)
    return out["result"]["scorecard"]["delegation"], out["calls"]


@pytest.mark.node_scripts
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("case", sorted(CASES))
def test_js_and_python_decide_identically(case):
    mode, kw = CASES[case]
    got, calls = _js_decision(mode, kw)
    doubtful = not scorecard.divisibility(kw.get("units"))["certain"] and mode != "lite"
    asked = [c for c in calls if c["label"] == "divisibility"]
    assert len(asked) == (1 if doubtful else 0), "the small-model call happens only when the heuristic is doubtful"
    if kw.get("model") is None and doubtful:
        kw = {**kw, "model": {"divisible": False, "evidence": "the call returned nothing"}}
    want = decide(mode, kw)
    if doubtful and kw["model"].get("tokens") is None:
        want["cost_tokens"], want["cost_source"] = None, "unavailable"
    want_js = {**want, "evidence": got["evidence"]} if kw.get("model") and not kw["model"].get("evidence") else want
    assert got == want_js


@pytest.mark.node_scripts
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_divisibility_call_sees_only_units_and_files_and_a_small_model():
    _, calls = _js_decision("standard", {"units": [A, BLANK], "reason": W, "model": {"divisible": False, "tokens": 3}})
    prompt = scorecard.divisibility_prompt([A, BLANK])
    (call,) = [c for c in calls if c["label"] == "divisibility"]
    assert call["agentType"] == "explore" and (call["sha"], call["len"]) == (hashlib.sha256(prompt.encode()).hexdigest()[:16], len(prompt))


@pytest.mark.node_scripts
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_without_units_reason_or_flag_the_run_is_exactly_what_it_was_in_2_9_0():
    out = snap.run_js(snap.IMPL_JS, {"goal": "g", "plan_path": "p.md"}, {**snap.IMPL_RESPONSES, "read-sizing": {"found": True, "scores": SCORES["standard"]}})
    assert "delegation" not in out["result"]["scorecard"] and "delegation" not in out["result"]["scorecard_line"]
    assert not [c for c in out["calls"] if c["label"] == "divisibility"]


@pytest.mark.node_scripts
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_an_explicit_size_flag_still_wins_and_records_no_delegation():
    out = snap.run_js(snap.IMPL_JS, {"goal": "g --lite", "plan_path": "p.md", "unit_list": [A, B], "delegation_reason": W}, snap.IMPL_RESPONSES)
    assert out["result"]["size"] == "inline" and not out["result"]["scorecard"]


DELEGATION_CASES = [c for c in json.loads((REPO / "evals" / "scorecard" / "cases.json").read_text(encoding="utf-8")) if "delegation" in c]


@pytest.mark.parametrize("case", DELEGATION_CASES, ids=[c["id"] for c in DELEGATION_CASES])
def test_eval_cases_expect_these_delegation_decisions(case):
    got = decide(case["mode"], case["delegation"]["input"])
    for key, value in case["delegation"]["expect"].items():
        assert got[key] == value, key

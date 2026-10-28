"""Observable outputs of the sizing/scorecard surface, for the characterization test (tests/test_sizing_characterization.py).

`build()` runs the `ai-resources scorecard` CLI, the Python helpers and both workflow scripts (through the node
harness) over every accepted input spelling and returns one JSON-able dict. The committed golden file is that dict
taken on the pre-refactor revision; the test fails on any difference, so a refactor cannot change behaviour silently.
Prompts are stored as sha256 plus length (the labels, agent types and every result field are stored in full).
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import itertools
import json
import subprocess
from pathlib import Path

def _tempdir():
    from ai_resources import scratch
    return scratch.tempdir("sizing-")


REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "tests" / "kit_script_harness.mjs"
PLAN_JS = REPO / "workflows" / "scripts" / "kit-plan.js"
IMPL_JS = REPO / "workflows" / "scripts" / "kit-implement.js"
CASES = json.loads((REPO / "evals" / "scorecard" / "cases.json").read_text(encoding="utf-8"))
DIMS = ["resumability", "blast_radius", "risk", "uncertainty", "verifiability", "spread"]
ZERO = dict.fromkeys(DIMS, 0)

UNDERSTAND = {"summary": "s", "files": ["a.py"], "notes": ["n"]}
DRAFT = {"approach": "a", "steps": ["1"], "files": ["a.py"], "risks": []}
JUDGE = {"winner": 1, "rationale": "r", "scores": [8, 5, 5]}
PLAN = {"plan_path": ".agent-output/planner/x-plan.md", "summary": "s", "acceptance_criteria": ["S1"],
        "requires_tests": True, "security_critical": False}
IMPL = {"summary": "s", "files_changed": ["a.py"], "tests_added": ["t.py"], "commands_run": ["pytest"]}
TEST_OK = {"passed": True, "command": "pytest", "failures": []}
TEST_BAD = {"passed": False, "command": "pytest", "failures": ["t::a"]}
NO_FINDINGS = {"findings": []}
SIGNOFF = {"met": True, "gaps": [], "evidence": ["pytest"]}
PLAN_RESPONSES = {"understand": UNDERSTAND, "draft": DRAFT, "judge": JUDGE, "write-plan": PLAN}
IMPL_RESPONSES = {"implement": IMPL, "test-gate": TEST_OK, "review": NO_FINDINGS, "sign-off": SIGNOFF,
                  "record-run": {"advice": "recorded"}, "reassess": {"advice": "keep"}}

# Score inputs: valid, boundary and invalid. Order matters only for the stable keys.
BAD_SCORES = {
    "risk9": {**ZERO, "risk": 9}, "risk-neg": {**ZERO, "risk": -1}, "risk-bool": {**ZERO, "risk": True},
    "risk-str": {**ZERO, "risk": "1"}, "risk-float": {**ZERO, "risk": 1.5}, "partial": {"risk": 1},
    "extra-key": {**ZERO, "bogus": 1}, "empty": {}, "string": "not a dict", "list": [1, 2],
}
LEGACY = [None, "inline", "light", "full", "lite", "standard", "bogus", ""]


def _scores_corners():
    return {"corner-" + "".join(map(str, v)): dict(zip(DIMS, v)) for v in itertools.product((0, 3), repeat=len(DIMS))}


def _scores_named():
    out = {c["id"]: c["scores"] for c in CASES}
    for v in range(4):
        out[f"ver{v}-only"] = {**ZERO, "verifiability": v}
        out[f"res{v}-only"] = {**ZERO, "resumability": v}
        out[f"risk{v}-only"] = {**ZERO, "risk": v}
    out["lite-edge"] = {**ZERO, "resumability": 1, "blast_radius": 1, "verifiability": 1}
    out["total-10"] = {**ZERO, "blast_radius": 1, "uncertainty": 1, "verifiability": 2, "resumability": 1, "spread": 1, "risk": 1, "blast_radius": 1}
    return out


def _clean(card):
    return json.loads(json.dumps(card))


# --- Python surface ---------------------------------------------------------------------------------------------

def _cli(argv, files=None):
    from ai_resources import scorecard
    out, err = io.StringIO(), io.StringIO()
    with _tempdir() as td, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        argv = [a.replace("@TMP", td) for a in argv]
        for name, text in (files or {}).items():
            (Path(td) / name).write_text(text, encoding="utf-8")
        rc = scorecard.main(argv)
        written = {p.name: p.read_text(encoding="utf-8") for p in sorted(Path(td).rglob("*")) if p.is_file()}
    return {"rc": rc, "stdout": out.getvalue(), "stderr": err.getvalue().replace(td, "@TMP"), "files": written}


def python_snapshot():
    from ai_resources import scorecard
    snap = {}
    named = _scores_named()
    for name, scores in {**named, **BAD_SCORES, **_scores_corners()}.items():
        for legacy in LEGACY if name in BAD_SCORES or name.startswith("typo") or name == "lite-edge" else [None]:
            snap[f"score_or_full/{name}/{legacy}"] = _clean(scorecard.score_or_full(scores, legacy))
    for legacy in LEGACY:
        snap[f"score_or_full/None/{legacy}"] = _clean(scorecard.score_or_full(None, legacy))
    for name, scores in named.items():
        card = scorecard.score_or_full(scores)
        snap[f"bump/{name}"] = _clean(scorecard.bump_for_risk(card, ["3: contract"]))
        snap[f"compact/{name}"] = scorecard.compact(card)
        snap[f"render/{name}"] = scorecard.render(card)
        snap[f"render-bump/{name}"] = scorecard.render(scorecard.bump_for_risk(card, ["3: contract", "1: data"]))
        snap[f"compact-bump/{name}"] = scorecard.compact(scorecard.bump_for_risk(card, ["3: contract"]))
    for legacy in LEGACY:
        card = scorecard.score_or_full(None, legacy)
        snap[f"render-legacy/{legacy}"] = scorecard.render(card)
        snap[f"compact-legacy/{legacy}"] = scorecard.compact(card)
    snap["render/invalid"] = scorecard.render(scorecard.score_or_full({"risk": 1}))
    snap["compact/invalid"] = scorecard.compact(scorecard.score_or_full({"risk": 1}))
    for mode, failures, lines in itertools.product(("lite", "standard", "full"), (0, 1, 2, 3, 4, 9), (0, 149, 150, 399, 400, 1000)):
        snap[f"escalate/{mode}/{failures}/{lines}"] = scorecard.escalate(mode, failures, lines)
        snap[f"observe/{mode}/{failures}/{lines}"] = list(scorecard.observe(mode, failures, lines))
    try:
        scorecard.escalate("bogus", 0, 0)
    except ValueError as e:
        snap["escalate/bogus"] = str(e)
    for scores in ({"resumability": 1}, {**ZERO, "risk": 4}, {**ZERO, "x": 1}):
        try:
            scorecard.validate(scores)
        except ValueError as e:
            snap[f"validate/{json.dumps(scores, sort_keys=True)}"] = str(e)
    snap["abbrev"] = scorecard.ABBREV
    snap["modes"] = list(scorecard.MODES)
    snap["full_card"] = _clean(scorecard.full_card("why"))
    return snap


def cli_snapshot():
    snap = {}
    named = _scores_named()
    j = json.dumps
    for name in list(named)[:14] + ["ver3-only", "risk3-only", "res2-only"]:
        snap[f"cli/score/{name}"] = _cli([j(named[name])])
        snap[f"cli/score-risk/{name}"] = _cli([j(named[name]), "--risk-item", "3: contract", "--risk-item", "1: data"])
        snap[f"cli/score-handoff/{name}"] = _cli([j(named[name]), "--handoff", "@TMP/h.md"])
        snap[f"cli/score-handoff-risk/{name}"] = _cli([j(named[name]), "--risk-item", "3: contract", "--handoff", "@TMP/sub/h.md"])
    for name, bad in BAD_SCORES.items():
        snap[f"cli/bad/{name}"] = _cli([j(bad)])
        snap[f"cli/bad-handoff/{name}"] = _cli([j(bad), "--handoff", "@TMP/h.md"])
    for raw in ("not json", "", "null", "42", "[1]", '"x"', "{"):
        snap[f"cli/raw/{raw}"] = _cli([raw])
    snap["cli/no-args"] = _cli([])
    snap["cli/flag-missing-value"] = _cli(["{}", "--handoff"])
    snap["cli/risk-missing-value"] = _cli([j(ZERO), "--risk-item"])
    for argv in (["escalate", "lite", "2", "0"], ["escalate", "lite", "0", "150"], ["escalate", "lite", "4", "0"], ["escalate", "standard", "2", "0"],
                 ["escalate", "standard", "1", "399"], ["escalate", "full", "9", "999"], ["escalate", "bogus", "1", "1"], ["escalate", "lite"],
                 ["escalate", "lite", "x", "1"], ["gate", "lite", "--failures", "2", "--diff-lines", "0"],
                 ["gate", "lite", "--failures", "0", "--diff-lines", "400"], ["gate", "standard", "--failures", "2", "--diff-lines", "0"],
                 ["gate", "full", "--failures", "9", "--diff-lines", "999"], ["gate", "lite", "--diff-lines", "10"], ["gate", "bogus", "--diff-lines", "1"],
                 ["gate"], ["gate", "lite", "--failures", "z", "--diff-lines", "1"]):
        snap["cli/" + " ".join(argv)] = _cli(argv)
        snap["cli-handoff/" + " ".join(argv)] = _cli(argv + ["--handoff", "@TMP/h.md"])
        snap["cli-handoff-reason/" + " ".join(argv)] = _cli(argv + ["--handoff", "@TMP/h.md", "--reason", "because"])
    # appending to an existing card, and a hand-written value in the handoff
    pre = Path(REPO / "handoff.md.template").read_text(encoding="utf-8")
    snap["cli/append-existing"] = _cli(["gate", "lite", "--failures", "2", "--diff-lines", "0", "--handoff", "@TMP/h.md"],
                                       {"h.md": pre.replace('scorecard: ""', 'scorecard: "LITE res0"')})
    snap["cli/record-then-score"] = _cli([j(named["multi-module-refactor"]), "--handoff", "@TMP/h.md"],
                                         {"h.md": pre.replace('scorecard: ""', 'scorecard: "OLD"')})
    snap["cli/no-front-matter"] = _cli([j(named["multi-module-refactor"]), "--handoff", "@TMP/h.md"], {"h.md": "no front matter\n"})
    return snap


# --- node surface -----------------------------------------------------------------------------------------------

def run_js(script, args, responses, raw=False):
    with _tempdir() as td:
        cfg = Path(td) / "cfg.json"
        cfg.write_text(json.dumps({"args": args, "responses": responses}))
        p = subprocess.run(["node", str(HARNESS), str(script), str(cfg)], capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        return {"crash": p.stderr.strip().splitlines()[-1] if p.stderr.strip() else "?"}
    data = json.loads(p.stdout)
    calls = [{"label": c["label"], "agentType": c["agentType"], "sha": hashlib.sha256(c["prompt"].encode()).hexdigest()[:16], "len": len(c["prompt"]),
              **({"prompt": c["prompt"]} if raw else {})}
             for c in data["calls"]]
    return {"calls": calls, "result": data["result"]}


def plan_cases():
    named = _scores_named()
    cases = {}
    for goal in ("g", "g --inline", "g --lite", "g --light", "g --standard", "g --full", "g --inline --full", "g --full --inline", "--full", "",
                 "g--full", "g --fullish", "--inline fix typo", "a  --light  b", "g --full --full"):
        cases[f"flag/{goal!r}"] = (goal, PLAN_RESPONSES)
    cases["flag/None"] = (None, PLAN_RESPONSES)
    for size in ("inline", "lite", "light", "standard", "full", "bogus", "toString", "", None, 3):
        cases[f"size/{size!r}"] = ({"goal": "g", "size": size}, PLAN_RESPONSES)
    cases["size/beats-flag"] = ({"goal": "g --full", "size": "inline"}, PLAN_RESPONSES)
    cases["size/bogus-uses-flag"] = ({"goal": "g --full", "size": "bogus"}, PLAN_RESPONSES)
    cases["args/task-key"] = ({"task": "do it", "kind": "bugfix", "constraints": "none"}, PLAN_RESPONSES)
    cases["args/kind-refactor-light"] = ({"goal": "g", "kind": "refactor", "size": "light"}, PLAN_RESPONSES)
    for name in list(named)[:12] + ["ver3-only", "ver2-only", "lite-edge", "total-10", "risk2-only", "res2-only"]:
        cases[f"classify/scores/{name}"] = ({"goal": "g"}, {**PLAN_RESPONSES, "classify": {"scores": named[name], "reason": "r", "started_at": 1700000000, "forecast_lines": 12, "units": 1, "files": 2, "suggested_mode": "light"}})
        cases[f"classify/scores-risk/{name}"] = ({"goal": "g"}, {**PLAN_RESPONSES, "classify": {"scores": named[name], "reason": "r", "risk_items": ["3: contract"]}})
    for name, bad in BAD_SCORES.items():
        cases[f"classify/bad/{name}"] = ({"goal": "g"}, {**PLAN_RESPONSES, "classify": {"scores": bad, "reason": "r"}})
        cases[f"classify/bad+mode/{name}"] = ({"goal": "g"}, {**PLAN_RESPONSES, "classify": {"scores": bad, "mode": "inline", "reason": "r"}})
    cases["classify/extra-key-valid"] = ({"goal": "g"}, {**PLAN_RESPONSES, "classify": {"scores": {**ZERO, "bogus": 1}, "reason": "r"}})
    cases["classify/scores-null"] = ({"goal": "g"}, {**PLAN_RESPONSES, "classify": {"scores": None, "reason": "r"}})
    for mode in ("inline", "light", "full", "lite", "standard", "bogus", "", None):
        cases[f"classify/mode/{mode}"] = ({"goal": "g"}, {**PLAN_RESPONSES, "classify": {"mode": mode, "reason": "r"}})
        cases[f"classify/mode-risk/{mode}"] = ({"goal": "g"}, {**PLAN_RESPONSES, "classify": {"mode": mode, "reason": "r", "risk_items": ["2: security"]}})
    cases["classify/nothing"] = ({"goal": "g"}, {**PLAN_RESPONSES, "classify": {"reason": "r"}})
    cases["classify/failed"] = ({"goal": "g"}, PLAN_RESPONSES)
    cases["classify/reader-dead"] = ({"goal": "g", "size": "full"}, {**PLAN_RESPONSES, "understand": None})
    cases["classify/no-draft"] = ({"goal": "g", "size": "full"}, {**PLAN_RESPONSES, "draft": None})
    cases["classify/no-plan"] = ({"goal": "g", "size": "full"}, {**PLAN_RESPONSES, "write-plan": None})
    cases["classify/judges-zero"] = ({"goal": "g", "size": "full"}, {**PLAN_RESPONSES, "judge": {"winner": 1, "rationale": "r", "scores": [0, 0, 0]}})
    cases["plan/ledger-ids"] = ({"goal": "g", "size": "light"}, {**PLAN_RESPONSES, "write-plan": {**PLAN, "tasks_path": "t.md", "task_ids": ["T1"], "spec_ids": ["S1"]}})
    cases["classify/forced-next"] = ({"goal": "g --light"}, PLAN_RESPONSES)
    return {k: run_js(PLAN_JS, a, r) for k, (a, r) in cases.items()}


def impl_cases():
    named = _scores_named()
    cases = {}
    for size in ("inline", "lite", "light", "standard", "full", "bogus", "toString"):
        cases[f"size/{size}"] = ({"goal": "g", "size": size}, IMPL_RESPONSES)
    for goal in ("g --inline", "g --lite", "g --light", "g --standard", "g --full", "g --full --inline"):
        cases[f"flag/{goal}"] = (goal, IMPL_RESPONSES)
    cases["flag/none"] = ({"goal": "g"}, IMPL_RESPONSES)
    cases["flag/empty"] = ({}, IMPL_RESPONSES)
    cases["flag/plan-only-inline-size"] = ({"plan_path": "p.md", "size": "inline"}, IMPL_RESPONSES)
    cases["args/plan-key"] = ({"plan": "p.md", "size": "light"}, IMPL_RESPONSES)
    # sizing block read from the plan
    for name in list(named)[:12] + ["ver3-only", "lite-edge"]:
        cases[f"sizing/scores/{name}"] = ({"goal": "g", "plan_path": "p.md"}, {**IMPL_RESPONSES, "read-sizing": {"found": True, "scores": named[name], "risk_items": [], "forecast_lines": 40, "units": 1, "files": 2}})
    for name, bad in BAD_SCORES.items():
        cases[f"sizing/bad/{name}"] = ({"goal": "g", "plan_path": "p.md"}, {**IMPL_RESPONSES, "read-sizing": {"found": True, "scores": bad}})
        cases[f"sizing/bad+mode/{name}"] = ({"goal": "g", "plan_path": "p.md"}, {**IMPL_RESPONSES, "read-sizing": {"found": True, "scores": bad, "mode": "light"}})
    for mode in ("inline", "light", "full", "lite", "standard", "bogus", None):
        cases[f"sizing/mode/{mode}"] = ({"goal": "g", "plan_path": "p.md"}, {**IMPL_RESPONSES, "read-sizing": {"found": True, "mode": mode, "risk_items": ["3: x"], "forecast_lines": 9, "units": 2, "files": 3}})
    cases["sizing/not-found"] = ({"goal": "g", "plan_path": "p.md"}, {**IMPL_RESPONSES, "read-sizing": {"found": False, "mode": "full", "scores": ZERO}})
    cases["sizing/scores-null"] = ({"goal": "g", "plan_path": "p.md"}, {**IMPL_RESPONSES, "read-sizing": {"found": True, "scores": None, "mode": "full"}})
    cases["sizing/risk-args-win"] = ({"goal": "g", "plan_path": "p.md", "risk_items": ["1: a"]}, {**IMPL_RESPONSES, "read-sizing": {"found": True, "mode": "light", "risk_items": ["3: b"]}})
    cases["sizing/unreadable"] = ({"goal": "g", "plan_path": "p.md"}, IMPL_RESPONSES)
    # passed-on scorecards and risk bumps
    for name in ("typo-in-readme", "multi-module-refactor", "auth-token-rotation"):
        from ai_resources import scorecard
        card = scorecard.score(named[name])
        js_card = {**card, "size": {"lite": "inline", "standard": "light", "full": "full"}[card["mode"]], "escalations": []}
        cases[f"card/{name}"] = ({"goal": "g", "size": js_card["size"], "scorecard": js_card}, IMPL_RESPONSES)
        cases[f"card-risk/{name}"] = ({"goal": "g", "size": js_card["size"], "scorecard": js_card, "risk_items": ["3: c"]}, IMPL_RESPONSES)
    cases["card/not-object"] = ({"goal": "g", "size": "light", "scorecard": "x"}, IMPL_RESPONSES)
    cases["risk/inline-bumps"] = ({"goal": "g", "size": "inline", "risk_items": ["2: authz"]}, IMPL_RESPONSES)
    cases["risk/light-sec"] = ({"goal": "g", "size": "light", "risk_items": ["security"]}, IMPL_RESPONSES)
    cases["risk/light-nonsec"] = ({"goal": "g", "size": "light", "risk_items": ["5: ci"]}, IMPL_RESPONSES)
    cases["risk/full"] = ({"goal": "g", "size": "full", "risk_items": ["1: data", "3: x"]}, IMPL_RESPONSES)
    cases["risk/not-array"] = ({"goal": "g", "size": "light", "risk_items": "3"}, IMPL_RESPONSES)
    # inline re-entry with observed numbers
    for f, d in ((0, 0), (1, 149), (2, 0), (0, 150), (3, 0), (4, 0), (0, 399), (0, 400), (9, 9), ("2", 0), (2.5, 0), (-1, -5)):
        cases[f"inline-obs/{f}/{d}"] = ({"goal": "g", "size": "inline", "failures": f, "diff_lines": d}, IMPL_RESPONSES)
    cases["inline-obs/via-flag"] = ({"goal": "g --inline", "failures": 2}, IMPL_RESPONSES)
    cases["inline-obs/with-card"] = ({"goal": "g", "size": "inline", "failures": 2, "scorecard": {"mode": "lite", "scored_mode": "lite", "scores": named["rename-cli-flag-internal"], "escalations": []}}, IMPL_RESPONSES)
    # impl-driven changes
    for to in ("inline", "light", "full", "lite", "bogus"):
        for size in ("light", "full"):
            cases[f"mode_change/{size}->{to}"] = ({"goal": "g", "size": size}, {**IMPL_RESPONSES, "implement": {**IMPL, "mode_change": {"to": to, "reason": "because"}}})
    cases["mode_change/no-reason"] = ({"goal": "g", "size": "light"}, {**IMPL_RESPONSES, "implement": {**IMPL, "mode_change": {"to": "full", "reason": ""}}})
    cases["mode_change/same"] = ({"goal": "g", "size": "light"}, {**IMPL_RESPONSES, "implement": {**IMPL, "mode_change": {"to": "light", "reason": "r"}}})
    for name in ("multi-module-refactor", "typo-in-readme", "auth-token-rotation"):
        from ai_resources import scorecard
        card = scorecard.score(named[name])
        size = {"lite": "inline", "standard": "light", "full": "full"}[card["mode"]]
        js_card = {**card, "size": size, "escalations": []}
        for to in ("inline", "light", "full"):
            for risk in ([], ["3: c"]):
                cases[f"mode_change-card/{name}->{to}/{len(risk)}"] = ({"goal": "g", "size": size if size != "inline" else "light", "scorecard": js_card, "risk_items": risk},
                                                                      {**IMPL_RESPONSES, "implement": {**IMPL, "mode_change": {"to": to, "reason": "why"}}})
    for dl in (0, 149, 150, 399, 400, 5000):
        for size in ("light", "full"):
            cases[f"diff/{size}/{dl}"] = ({"goal": "g", "size": size}, {**IMPL_RESPONSES, "implement": {**IMPL, "diff_lines": dl}})
    cases["diff/card"] = ({"goal": "g", "size": "light", "scorecard": {"mode": "standard", "scored_mode": "standard", "scores": named["multi-module-refactor"], "escalations": []}},
                          {**IMPL_RESPONSES, "implement": {**IMPL, "diff_lines": 500}})
    cases["impl/blocked"] = ({"goal": "g", "size": "light"}, {**IMPL_RESPONSES, "implement": {**IMPL, "blocked": True, "block_reason": "need a call"}})
    cases["impl/none"] = ({"goal": "g", "size": "light"}, {**IMPL_RESPONSES, "implement": None})
    cases["impl/start-epoch"] = ({"goal": "g", "size": "light"}, {**IMPL_RESPONSES, "implement": {**IMPL, "started_at": 1700000001, "changed_lines": 77, "reassess_advice": "keep going"}})
    cases["impl/start-arg"] = ({"goal": "g", "size": "light", "start": 1700000002, "estimated": "full", "forced": True, "forecast_lines": 5, "units": 3, "files": 4}, {**IMPL_RESPONSES, "implement": {**IMPL, "started_at": 1700000001}})
    cases["impl/estimated-bogus"] = ({"goal": "g", "size": "light", "estimated": "lite"}, IMPL_RESPONSES)
    # test loop
    for size in ("light", "full"):
        cases[f"tests/{size}/always-failing"] = ({"goal": "g", "size": size}, {**IMPL_RESPONSES, "test-gate": TEST_BAD, "fix-tests": IMPL})
        cases[f"tests/{size}/fix-then-green"] = ({"goal": "g", "size": size}, {**IMPL_RESPONSES, "test-gate:": TEST_OK, "test-gate": TEST_BAD, "fix-tests": IMPL})
        cases[f"tests/{size}/fix-dead"] = ({"goal": "g", "size": size}, {**IMPL_RESPONSES, "test-gate": TEST_BAD})
        cases[f"tests/{size}/no-failure-list"] = ({"goal": "g", "size": size}, {**IMPL_RESPONSES, "test-gate": {"passed": False, "command": "pytest", "failures": []}})
        cases[f"tests/{size}/gate-dead"] = ({"goal": "g", "size": size}, {**IMPL_RESPONSES, "test-gate": None})
    cases["tests/light-card-escalates"] = ({"goal": "g", "size": "light", "scorecard": {"mode": "standard", "scored_mode": "standard", "scores": named["multi-module-refactor"], "escalations": []}},
                                          {**IMPL_RESPONSES, "test-gate": TEST_BAD, "fix-tests": IMPL})
    # review, verify, fix, sign-off
    many = {"findings": [{"title": f"f{i}", "file": "a.py", "line": i, "detail": "d", "severity": ["blocker", "major", "minor"][i % 3]} for i in range(20)]}
    one = {"findings": [{"title": "t", "file": "a.py", "detail": "d", "severity": "blocker"}]}
    minor = {"findings": [{"title": "t", "file": "a.py", "detail": "d", "severity": "minor"}]}
    for size in ("light", "full"):
        cases[f"review/{size}/many"] = ({"goal": "g", "size": size}, {**IMPL_RESPONSES, "review": many, "verify": {"refuted": False, "reason": "real", "suggested_fix": "do x"}, "apply-fixes": IMPL})
        cases[f"review/{size}/one-confirmed"] = ({"goal": "g", "size": size}, {**IMPL_RESPONSES, "review": one, "verify": {"refuted": False, "reason": "real"}, "apply-fixes": IMPL})
        cases[f"review/{size}/one-refuted"] = ({"goal": "g", "size": size}, {**IMPL_RESPONSES, "review": one, "verify": {"refuted": True, "reason": "no"}})
        cases[f"review/{size}/minor"] = ({"goal": "g", "size": size}, {**IMPL_RESPONSES, "review": minor, "verify": {"refuted": False, "reason": "real"}})
        cases[f"signoff/{size}/gaps"] = ({"goal": "g", "size": size, "risk_items": ["3: c"]}, {**IMPL_RESPONSES, "sign-off": {"met": False, "gaps": ["S1"], "evidence": []}})
        cases[f"signoff/{size}/gaps-advice"] = ({"goal": "g", "size": size, "risk_items": ["3: c"]}, {**IMPL_RESPONSES, "sign-off": {"met": False, "gaps": ["S1"], "evidence": []}, "reassess:verifier-fail": {"advice": "rethink"}})
        cases[f"signoff/{size}/dead"] = ({"goal": "g", "size": size, "risk_items": ["3: c"]}, {**IMPL_RESPONSES, "sign-off": None})
    cases["plan/ledger"] = ({"goal": "g", "size": "light", "plan_path": "p.md", "tasks_path": "t.md", "task_ids": ["T1"], "spec_ids": ["S1"], "acceptance_criteria": ["c1"], "security_critical": False}, {**IMPL_RESPONSES})
    cases["plan/ledger-full"] = ({"goal": "g", "size": "full", "plan_path": "p.md", "tasks_path": "t.md", "task_ids": ["T1"], "spec_ids": ["S1"], "acceptance_criteria": ["c1"], "security_critical": False}, {**IMPL_RESPONSES})
    cases["plan/no-goal"] = ({"plan_path": "p.md", "size": "light"}, IMPL_RESPONSES)
    return {k: run_js(IMPL_JS, a, r) for k, (a, r) in cases.items()}


def build():
    return {"python": python_snapshot(), "cli": cli_snapshot(), "plan": plan_cases(), "implement": impl_cases()}


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(REPO / "scripts"))
    json.dump(build(), sys.stdout, indent=1, sort_keys=True)

"""The plan-sizing checker: a plan must carry a coherent `## Sizing` block (skills/kit-orchestration "Task size")."""
from __future__ import annotations

from pathlib import Path

from ai_resources import cli, plan_sizing

REPO = Path(__file__).resolve().parents[1]

GOOD = """# Plan

## Sizing
- mode: light | resume_test: fail (sessions) | risk_items: none
- forecast: lines~120 units=1 surfaces=[a.py] | route: inline | verification: self | spawn_budget: 0

## Steps
"""


def swap(text, old, new):
    assert old in text
    return text.replace(old, new)


def test_shipped_examples_are_valid():
    assert plan_sizing.problems((REPO / "templates" / "tasks.template.md").read_text(encoding="utf-8")) == []


def test_good_block_has_no_problems_and_parses_pipe_separated_pairs():
    assert plan_sizing.problems(GOOD) == []
    f = plan_sizing.sizing_fields(GOOD)
    assert f["mode"] == "light" and f["spawn_budget"] == "0" and f["resume_test"].startswith("fail")


def test_missing_block_or_keys_are_reported():
    assert plan_sizing.problems("# Plan\nno sizing") == ["no `## Sizing` block"]
    broken = swap(GOOD, " | route: inline | verification: self | spawn_budget: 0", "")
    assert [p for p in plan_sizing.problems(broken) if "missing" in p] == [
        "Sizing: missing `route`", "Sizing: missing `verification`", "Sizing: missing `spawn_budget`"]


def test_only_the_exact_sizing_heading_counts():
    assert plan_sizing.problems("## Sizing (actual, filled at close)\n- mode: light\n") == ["no `## Sizing` block"]


def test_unknown_mode_and_bad_budget():
    out = plan_sizing.problems(swap(swap(GOOD, "mode: light", "mode: huge"), "spawn_budget: 0", "spawn_budget: many"))
    assert any("mode `huge`" in p for p in out) and any("spawn_budget `many`" in p for p in out)


def test_inline_rules():
    inline = swap(GOOD, "mode: light", "mode: inline")
    assert plan_sizing.problems(inline) == []
    assert any("risk item" in p for p in plan_sizing.problems(swap(inline, "risk_items: none", "risk_items: [3: hook]")))
    assert any("spawn_budget 0" in p for p in plan_sizing.problems(swap(inline, "spawn_budget: 0", "spawn_budget: 2")))
    assert any("route inline" in p for p in plan_sizing.problems(swap(inline, "route: inline", "route: explorer(map)")))


def test_risk_item_needs_an_independent_verifier_and_light_has_no_writers():
    risky = swap(GOOD, "risk_items: none", "risk_items: [3: contract]")
    assert any("verifier(item N)" in p for p in plan_sizing.problems(risky))
    assert plan_sizing.problems(swap(risky, "verification: self", "verification: verifier(item 3)")) == []
    assert any("writers" in p for p in plan_sizing.problems(swap(GOOD, "route: inline", "route: writers(2, disjoint)")))


def test_comments_in_the_template_style_are_ignored():
    text = "## Sizing\n- mode: full   # inline | light | full\n- resume_test: fail\n- risk_items: none\n- forecast: lines~900 units=2\n- route: writers(2, disjoint)\n- verification: verifier(item 3)\n- spawn_budget: 2\n"
    assert plan_sizing.problems(text) == []


def test_cli_exit_codes(tmp_path, capsys):
    ok, bad = tmp_path / "ok.md", tmp_path / "bad.md"
    ok.write_text(GOOD, encoding="utf-8")
    bad.write_text("# nothing", encoding="utf-8")
    assert cli.main(["plan-check", str(ok)]) == 0
    assert cli.main(["plan-check", str(ok), str(bad)]) == 1
    assert "no `## Sizing` block" in capsys.readouterr().err
    assert cli.main(["plan-check", str(tmp_path / "missing.md")]) == 1

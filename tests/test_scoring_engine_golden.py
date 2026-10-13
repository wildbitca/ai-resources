"""Golden cases for the engine. The scores below are the operator's ILLUSTRATIVE examples and fixtures only:
they are not evidence and are never seed data (AC-12, AC-11)."""
from __future__ import annotations

import pytest

from ai_resources.scoring.compose import compose
from ai_resources.setup import profiles
from scoring_fakes import GEMINI, HAIKU, OPUS, SONNET, SOL, fixture_db, make_input, row

# SONNET is on the Claude subscription pool, SOL on openai-api, GEMINI on gemini-api.
ENABLED = ("anthropic", "google", "openai")


def run(rows, roles, **kw):
    kw.setdefault("enabled", ENABLED)
    kw.setdefault("allow_unverified", True)
    return compose(make_input(fixture_db(rows), roles=roles, **kw))


def chosen(comp, role):
    return next(a for a in comp.assignments if a.role == role)


def test_planner_8_0_vs_7_9_follows_the_less_loaded_pool():
    rows = [row("planner", SONNET, 8.0), row("planner", SOL, 7.9)]
    scarce_claude = run(rows, ("planner",), static_weights={"claude-subscription": 0.3, "openai-api": 1.0})
    assert chosen(scarce_claude, "planner").model == SOL
    scarce_openai = run(rows, ("planner",), static_weights={"claude-subscription": 1.0, "openai-api": 0.3})
    assert chosen(scarce_openai, "planner").model == SONNET


def test_architect_8_0_vs_7_0_takes_the_best_whatever_the_load():
    rows = [row("software-architect", SONNET, 8.0), row("software-architect", SOL, 7.0)]
    comp = run(rows, ("software-architect",), static_weights={"claude-subscription": 0.3, "openai-api": 1.0})
    assert chosen(comp, "software-architect").model == SONNET


def test_implementer_9_vs_8_5_takes_the_best_critical_class():
    rows = [row("implementer", SONNET, 9.0), row("implementer", SOL, 8.5)]
    comp = run(rows, ("implementer",), static_weights={"claude-subscription": 0.3, "openai-api": 1.0})
    assert chosen(comp, "implementer").model == SONNET


def test_tester_9_vs_9_is_pure_balancing():
    rows = [row("tester", SONNET, 9.0), row("tester", SOL, 9.0)]
    to_openai = run(rows, ("tester",), static_weights={"claude-subscription": 0.3, "openai-api": 1.0})
    to_claude = run(rows, ("tester",), static_weights={"claude-subscription": 1.0, "openai-api": 0.3})
    assert chosen(to_openai, "tester").model == SOL and chosen(to_claude, "tester").model == SONNET


def test_reviewer_9_vs_8_7_follows_the_standard_tolerance_of_0_5():
    rows = [row("code-reviewer", SONNET, 9.0), row("code-reviewer", SOL, 8.7)]
    comp = run(rows, ("code-reviewer",), static_weights={"claude-subscription": 0.3, "openai-api": 1.0})
    assert chosen(comp, "code-reviewer").model == SOL        # 0.3 gap is inside the 0.5 band
    wide = [row("code-reviewer", SONNET, 9.0), row("code-reviewer", SOL, 8.4)]
    comp = run(wide, ("code-reviewer",), static_weights={"claude-subscription": 0.3, "openai-api": 1.0})
    assert chosen(comp, "code-reviewer").model == SONNET     # 0.6 gap is outside


def test_unverified_cell_never_enters_without_the_flag():
    rows = [row("planner", SONNET, 8.0), row("planner", SOL, 7.9)]
    comp = run(rows, ("planner",), allow_unverified=False, static_weights={"claude-subscription": 0.3})
    assert chosen(comp, "planner").model == SONNET
    assert any(r.model == SOL and r.kind == "unverified" for r in comp.rejected)


def test_diversity_moves_the_reviewer_off_the_implementers_vendor_within_tolerance():
    rows = [row("implementer", SONNET, 9.0), row("code-reviewer", SONNET, 9.0), row("code-reviewer", SOL, 8.6)]
    comp = run(rows, ("implementer", "code-reviewer"))
    rv = chosen(comp, "code-reviewer")
    assert rv.model == SOL and "diversity" in rv.flags and "unverified" in rv.flags
    far = [row("implementer", SONNET, 9.0), row("code-reviewer", SONNET, 9.0), row("code-reviewer", SOL, 8.0)]
    assert chosen(run(far, ("implementer", "code-reviewer")), "code-reviewer").model == SONNET


# --- P11: the default is kept -------------------------------------------------------------------
BASELINE = {r: c["model"] for r, c in profiles.load_profile("balanced-claude-55")["by_role"].items()}
ROLES_5 = ("explore", "tester", "implementer", "planner", "software-architect")


def anthropic_rows(haiku_score):
    rows = []
    for role in ROLES_5:
        rows += [row(role, SONNET, 6.4), row(role, OPUS, 6.5), row(role, HAIKU, haiku_score)]
    return rows


def test_default_is_kept_when_the_baseline_is_within_band_everywhere():
    db = fixture_db(anthropic_rows(6.0))
    comp = compose(make_input(db, mode="mixed", enabled=("anthropic",), roles=ROLES_5, baseline=BASELINE,
                              baseline_name="balanced-claude-55"))
    assert comp.kept_default and comp.recommendation == "balanced-claude-55"
    assert comp.by_role == {} and "default-kept" in comp.flags
    assert {a.role: a.model for a in comp.assignments} == {r: BASELINE[r] for r in ROLES_5}


def test_default_is_not_kept_when_the_baseline_falls_outside_a_band():
    db = fixture_db(anthropic_rows(3.5))
    comp = compose(make_input(db, mode="mixed", enabled=("anthropic",), roles=ROLES_5, baseline=BASELINE,
                              baseline_name="balanced-claude-55"))
    assert not comp.kept_default and comp.recommendation == ""
    assert comp.by_role and comp.by_role["explore"]["model"] != BASELINE["explore"]

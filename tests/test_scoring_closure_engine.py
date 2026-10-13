"""Closure of Verifier finding F1: "default kept" must honour the mode's shape contract. Scores are
ILLUSTRATIVE FIXTURES, never evidence."""
from __future__ import annotations

from ai_resources.scoring.compose import compose
from scoring_fakes import OPUS, SONNET, fixture_db, make_input, row

ROLES_5 = ("explore", "tester", "implementer", "planner", "software-architect")
BASELINE = {r: (OPUS if r == "implementer" else SONNET) for r in ROLES_5}


def _anthropic_rows(roles=ROLES_5, sonnet=8.0, opus=8.0):
    rows = []
    for role in roles:
        rows += [row(role, SONNET, sonnet), row(role, OPUS, opus)]
    return rows


# --- F1: "default kept" must not override the mode's shape contract ---------------------------------
def test_uniform_mode_never_keeps_a_two_model_baseline():
    comp = compose(make_input(fixture_db(_anthropic_rows()), mode="uniform", enabled=("anthropic",),
                              roles=ROLES_5, baseline=BASELINE, baseline_name="balanced-claude-55"))
    assert len({a.model for a in comp.assignments}) == 1
    assert comp.shape == "single" and comp.kept_default is False
    assert "default-kept" not in comp.flags and comp.recommendation == ""
    assert comp.by_role and len({e["model"] for e in comp.by_role.values()}) == 1


def test_uniform_mode_keeps_a_baseline_that_is_itself_one_model():
    uniform = {r: SONNET for r in ROLES_5}
    comp = compose(make_input(fixture_db(_anthropic_rows(sonnet=8.0, opus=7.9)), mode="uniform",
                              enabled=("anthropic",), roles=ROLES_5, baseline=uniform,
                              baseline_name="balanced-claude-55"))
    assert len({a.model for a in comp.assignments}) == 1


def test_single_provider_mode_never_keeps_a_baseline_spanning_providers():
    # A baseline whose models resolve to two providers (anthropic-direct and vertex) is not single-provider.
    rows = []
    for role in ROLES_5:
        rows += [row(role, SONNET, 8.0), row(role, OPUS, 8.0, route="vertex-global")]
    comp = compose(make_input(fixture_db(rows), mode="single-provider", enabled=("anthropic", "vertex"),
                              roles=ROLES_5, baseline=BASELINE, baseline_name="balanced-claude-55",
                              allow_unverified=True))
    if comp.kept_default:
        assert len({a.route for a in comp.assignments}) == 1
    assert comp.shape == "single-provider"
    assert len({a.route for a in comp.assignments}) == 1

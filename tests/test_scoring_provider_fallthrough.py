"""Follow-up U2: in single-provider mode a loss-cap refusal on the best-ranked provider must not hide a
provider that is within the cap. Scores are ILLUSTRATIVE FIXTURES, never evidence."""
from __future__ import annotations

from ai_resources.scoring.compose import compose
from scoring_fakes import GEMINI, OPUS, SONNET, fixture_db, make_input, row

ROLES = ("planner", "implementer")
WEIGHTS = {"claude-subscription": 0.3, "vertex-claude": 1.0}


def _compose(rows):
    return compose(make_input(fixture_db(rows), roles=ROLES, enabled=("anthropic", "vertex"),
                              allow_unverified=True, mode="single-provider", static_weights=WEIGHTS))


def test_a_cap_refusal_on_the_best_ranked_provider_falls_through_to_a_feasible_one():
    """vertex covers both roles and ranks first (12.5 vs 9.5) but loses 6.5 on the implementer, over the cap of
    5.0. anthropic covers one role, loses nothing, and is the only feasible single-provider team."""
    rows = [row("planner", OPUS, 9.5, route="vertex-global"),
            row("implementer", SONNET, 3.0, route="vertex-global"),
            row("implementer", SONNET, 9.5)]                                  # anthropic-direct
    comp = _compose(rows)
    assert not comp.refusal, comp.refusal
    assert {a.route for a in comp.assignments} == {"anthropic-direct"}
    assert [a.role for a in comp.assignments] == ["implementer"]
    assert comp.weighted_loss <= comp.loss_cap


def test_when_every_provider_breaks_the_cap_the_best_ranked_refusal_is_reported():
    """Neither provider holds a verified anchor, so each is judged against the other's best score."""
    rows = [row("planner", OPUS, 9.5, route="vertex-global"),
            row("implementer", SONNET, 3.0, route="vertex-global"),         # vertex: score 12.5, loss 6.5
            row("planner", GEMINI, 2.0),
            row("implementer", GEMINI, 9.5)]                                  # google: score 11.5, loss 7.5
    comp = compose(make_input(fixture_db(rows), roles=ROLES, enabled=("vertex", "google"),
                              allow_unverified=True, mode="single-provider", static_weights=WEIGHTS))
    assert comp.refused
    assert "weighted loss 6.50 exceeds the cap 5.00" in comp.refusal
    assert not comp.assignments

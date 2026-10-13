"""Closure of Verifier finding F5: engine behaviours that survived mutation (S* anchoring, stale rows, the
opt-in gate). Scores are ILLUSTRATIVE FIXTURES, never evidence."""
from __future__ import annotations

from ai_resources.scoring.compose import compose
from scoring_fakes import FABLE, GEMINI, HAIKU, SONNET, SOL, fixture_db, make_input, row


def test_s_star_is_anchored_at_the_best_verified_score_not_the_best_unverified():
    rows = []
    for role in ("implementer",):
        rows += [row(role, SONNET, 6.0), row(role, SOL, 9.5)]
    comp = compose(make_input(fixture_db(rows), roles=("implementer",), enabled=("anthropic", "openai"),
                              allow_unverified=True))
    a = comp.assignments[0]
    assert a.s_star == 6.0                       # verified anchor, not 9.5
    assert comp.why_rows[0].s_star == 6.0


def test_s_star_falls_back_to_the_best_overall_when_nothing_is_verified():
    rows = [row("implementer", SOL, 7.0), row("implementer", GEMINI, 8.0)]
    comp = compose(make_input(fixture_db(rows), roles=("implementer",), enabled=("openai", "google"),
                              allow_unverified=True))
    assert comp.assignments[0].s_star == 8.0


def test_a_stale_row_is_rejected_and_accepted_only_on_request():
    rows = [row("implementer", SONNET, 8.0, as_of="2020-01-01"), row("implementer", HAIKU, 5.0)]
    base = dict(roles=("implementer",), enabled=("anthropic",))
    comp = compose(make_input(fixture_db(rows), **base))
    assert [a.model for a in comp.assignments] == [HAIKU]
    assert any(r.kind == "stale" and r.model == SONNET for r in comp.rejected)
    comp2 = compose(make_input(fixture_db(rows), accept_stale=True, **base))
    assert comp2.assignments[0].model == SONNET and "stale-accepted" in comp2.assignments[0].flags


def test_an_opt_in_model_is_rejected_unless_fable_is_allowed():
    rows = [row("implementer", FABLE, 9.5), row("implementer", SONNET, 8.0)]
    base = dict(roles=("implementer",), enabled=("anthropic",))
    comp = compose(make_input(fixture_db(rows), **base))
    assert comp.assignments[0].model == SONNET
    assert any(r.kind == "opt-in-required" and r.model == FABLE for r in comp.rejected)
    comp2 = compose(make_input(fixture_db(rows), fable_allowed=True, **base))
    assert comp2.assignments[0].model == FABLE

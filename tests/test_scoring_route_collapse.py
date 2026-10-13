"""Closure of Verifier finding F2: a profile entry names a model, never a route, so the engine must not
propose route-level picks the stored overlay cannot realise (ADR-0006 section 8). Scores are ILLUSTRATIVE
FIXTURES, never evidence."""
from __future__ import annotations

from ai_resources.scoring import profile_out
from ai_resources.scoring.compose import compose
from scoring_fakes import OPUS, SONNET, SOL, fixture_db, make_input, row

ROLES = ("planner", "implementer", "tester", "explore", "code-reviewer")


def _rows():
    rows = []
    for role in ROLES:
        for model in (SONNET, OPUS):
            rows.append(row(role, model, 8.0))                              # anthropic-direct
            rows.append(row(role, model, 8.0, route="vertex-global"))
        rows.append(row(role, SOL, 7.8))
    return rows


def _compose(**kw):
    kw.setdefault("static_weights", {"claude-subscription": 0.3, "vertex-claude": 1.0, "openai-api": 0.5})
    return compose(make_input(fixture_db(_rows()), roles=ROLES, enabled=("anthropic", "vertex", "openai"),
                              allow_unverified=True, **kw))


def test_a_model_has_one_route_across_the_whole_team():
    comp = _compose()
    routes: dict[str, set[str]] = {}
    for a in comp.assignments:
        routes.setdefault(a.model, set()).add(a.route)
    assert routes and all(len(r) == 1 for r in routes.values()), routes


def test_the_route_shown_is_the_route_the_provenance_and_overlay_describe():
    comp = _compose()
    prov = profile_out.provenance(comp)
    assert prov["routes"] == {a.role: a.route for a in comp.assignments}
    overlay = profile_out.to_customizations(comp)
    assert {r: e["model"] for r, e in overlay.items()} == {a.role: a.model for a in comp.assignments}
    # the pool shown for a role is the pool of the one route kept for that model
    for a in comp.assignments:
        same = [b for b in comp.assignments if b.model == a.model]
        assert {b.pool_id for b in same} == {a.pool_id}


def test_dropped_routes_of_a_chosen_model_are_reported_as_collapsed():
    comp = _compose()
    kept = {a.model: a.route for a in comp.assignments}
    collapsed = [r for r in comp.rejected if r.kind == "route-collapsed"]
    assert collapsed
    assert all(r.route != kept.get(r.model) for r in collapsed)

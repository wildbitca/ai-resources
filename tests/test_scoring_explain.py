"""The why table: text and JSON renderings of a composition."""
from __future__ import annotations

import json
import pathlib

from ai_resources.scoring import explain, headroom as hr
from ai_resources.scoring.compose import compose
from scoring_fakes import GEMINI, HAIKU, OPUS, SONNET, SOL, fixture_db, make_input, row

GOLDEN = pathlib.Path(__file__).parent / "fixtures" / "scoring_explain_golden.json"
ROLES = ("planner", "implementer", "tester")


def fixture_composition(**kw):
    rows = []
    for role, scores in {"planner": (6.4, 5.8, 6.2, 6.0), "implementer": (6.4, 5.8, 6.0, 5.9),
                         "tester": (6.4, 5.8, 6.3, 3.5)}.items():
        s, o, sol, h = scores
        rows += [row(role, SONNET, s), row(role, OPUS, o), row(role, SOL, sol), row(role, HAIKU, h),
                 row(role, GEMINI, 2.0), row(role, SOL, sol, route="openclaw-codex-engine")]
    kw.setdefault("allow_unverified", True)
    kw.setdefault("enabled", ("anthropic", "openai"))
    kw.setdefault("static_weights", {"claude-subscription": 0.3, "openai-api": 1.0})
    kw.setdefault("references", {"all-gemini": {r: GEMINI for r in ROLES},
                                 "all-openai-via-codex": {r: SOL + "@openclaw-codex-engine" for r in ROLES}})
    return compose(make_input(fixture_db(rows), roles=ROLES, **kw))


def test_json_matches_the_golden_document():
    doc = explain.to_json(fixture_composition())
    expected = json.loads(GOLDEN.read_text())
    assert doc == expected


def test_every_assignment_has_a_row_and_the_roles_are_in_the_table():
    comp = fixture_composition()
    doc = explain.to_json(comp)
    assert {r["role"] for r in doc["rows"]} == {a.role for a in comp.assignments} == set(ROLES)
    text = explain.render_text(comp)
    for role in ROLES:
        assert role in text
    for column in ("imp", "class", "band", "chosen", "score", "label", "as_of", "price in/out", "pool",
                   "headroom", "gate", "usage", "runner-up", "flags"):
        assert column in text


def test_reference_rows_are_present_with_their_gate_status():
    doc = explain.to_json(fixture_composition())
    refs = {r["name"]: r for r in doc["references"]}
    assert set(refs) == {"all-gemini", "all-openai-via-codex"}
    assert refs["all-gemini"]["gate"] == "unverified" and refs["all-gemini"]["covered_roles"] == 3
    codex = refs["all-openai-via-codex"]
    assert codex["gate"] == "blocked" and "instructions-only" in codex["detail"]
    assert codex["weighted_score"] > 0


def test_footer_names_unknown_headroom_and_the_unverified_state():
    comp = fixture_composition()
    lines = explain.footer_lines(comp)
    assert any("Headroom unknown or stale for" in ln and "static weights were used" in ln for ln in lines)
    assert any("UNVERIFIED today" in ln for ln in lines)
    assert any(ln.startswith("weighted loss") and comp.input_digest in ln for ln in lines)


def test_a_live_reading_is_shown_with_source_and_percentage_and_leaves_the_unknown_line():
    reading = hr.HeadroomReading("claude-subscription", hr.KNOWN, 0.4, 123456, "statusline-sidecar")
    comp = fixture_composition(readings={"claude-subscription": reading, "openai-api": hr.unknown_reading("openai-api")})
    text = explain.render_text(comp)
    assert "40% used" in text and "resets_at 123456" in text
    assert any("openai-api" in ln for ln in explain.footer_lines(comp) if ln.startswith("Headroom unknown"))


def test_price_column_shows_validity_and_the_verify_marker():
    rows = [row("tester", HAIKU, 5.0, route="openrouter")]
    comp = compose(make_input(fixture_db(rows), roles=("tester",), enabled=("openrouter",), allow_unverified=True,
                              mode_key="multi-model:openrouter"))
    # openrouter is a gateway provider: it needs the provider enabled, and Haiku's OpenRouter price is flagged.
    assert comp.assignments and comp.assignments[0].price_verify is True
    assert "[verify]" in explain.render_text(comp)


def test_refusal_is_rendered_and_serialisable():
    comp = compose(make_input(fixture_db([]), roles=ROLES, enabled=()))
    assert comp.refused
    assert explain.render_text(comp).startswith("No team proposed")
    assert json.loads(explain.dumps(comp))["refusal"] == comp.refusal

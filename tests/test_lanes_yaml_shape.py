"""`lanes.yaml` shape (ADR-0019 D2): matrix only. None of the removed concepts can come back as a key or a word."""
from __future__ import annotations

import pytest
import yaml

from ai_resources.scoring import routing
from ai_resources.setup import lanes_render, profiles, state

FORBIDDEN_KEYS = {"candidates", "second", "resolved", "agents", "roles", "gates", "held", "pins", "mcp", "role_meta",
                  "pool_caps", "verify_capability", "display-only", "blocked"}
FORBIDDEN_WORDS = ("display-only", "(lane)", "(second)", "second opinion", "static weight")


def _keys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield str(k)
            yield from _keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from _keys(v)


def _text(profile="balanced", *, acks=None, override=None):
    s = state.SetupState()
    s.mode = "single-model"
    doc = profiles.load_profile(profile, "single-model")
    if override:
        doc = {**doc, "routing": override}
    s.profile.lanes = lanes_render.lanes_state(doc, orchestrators=["main"], gates={"antigravity": True})
    s.profile.lane_acks = acks
    return lanes_render.expected_text(s)


@pytest.mark.parametrize("profile", ["balanced", "performance", "low-cost", "subscription-claude-55"])
def test_the_document_has_exactly_the_matrix_keys(profile):
    doc = yaml.safe_load(_text(profile))
    assert set(doc) == {"mode", "generated_from", "routing", "digest"}
    assert doc["generated_from"] == "scoring.routing"
    assert set(doc["routing"]) == {"matrix", "balancing"}


def test_no_removed_concept_appears_as_a_key_or_a_word():
    google = {"google": lanes_render.make_ack("google", ["explore"], "2026-10-27T00:00:00Z", account="a@example.dev")}
    text = _text(acks=google)
    doc = yaml.safe_load(text)
    assert not FORBIDDEN_KEYS & set(_keys(doc["routing"])) and not FORBIDDEN_KEYS & (set(doc) - {"acks"})
    low = text.lower()
    assert not [w for w in FORBIDDEN_WORDS if w in low]


def test_the_google_ack_is_the_only_optional_section():
    google = {"google": lanes_render.make_ack("google", ["explore", "tester"], "2026-10-27T00:00:00Z",
                                              account="a@example.dev")}
    doc = yaml.safe_load(_text(acks=google))
    assert set(doc) == {"mode", "generated_from", "routing", "acks", "digest"}
    assert doc["acks"] == {"google": {"roles": ["explore", "tester"], "account": "a@example.dev",
                                      "text_sha256": google["google"]["text_sha256"]}}


def test_every_role_has_one_primary_and_a_fallback_list_and_a_class():
    matrix = yaml.safe_load(_text())["routing"]["matrix"]
    assert set(matrix) == set(routing.ROLES)
    for name, entry in matrix.items():
        assert set(entry) <= {"class", "primary", "fallback", "checker", "note"}, name
        assert set(entry["primary"]) == {"cell", "pool"} and isinstance(entry["fallback"], list)
        assert entry["class"] in routing.CLASS_POOLS


def test_the_digest_covers_everything_but_itself_and_is_deterministic():
    a, b = yaml.safe_load(_text()), yaml.safe_load(_text())
    assert a == b and a["digest"] == lanes_render.doc_digest(a)
    a["routing"]["matrix"]["explore"]["primary"]["cell"] = "claude-code:haiku"
    assert a["digest"] != lanes_render.doc_digest(a)


def test_rendering_is_stable_text():
    assert _text() == _text()
    assert _text().startswith("# Generated from scoring.routing")

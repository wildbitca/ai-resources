"""The compat gate adapter: it reads the matrix and never changes it (AC-10)."""
from __future__ import annotations

import pytest

from ai_resources.scoring import db as scores_db
from ai_resources.scoring import gate as gate_mod
from ai_resources.scoring.compose import MODES, compose
from ai_resources.setup import compat
from scoring_fakes import GEMINI, HAIKU, OPUS, SONNET, SOL, fixture_db, make_input, row

L = compat.MULTI_LITELLM


def test_anthropic_cell_is_declared():
    for mode in compat.MODES:
        v = gate_mod.gate_from_compat("claude", None, "anthropic", mode)
        assert v.allowed and v.verified and v.grade == gate_mod.DECLARED


def test_unverified_cell_needs_the_flag_and_is_graded_unverified():
    no = gate_mod.gate_from_compat("claude", None, "google", L, allow_unverified=False)
    assert not no.allowed and no.grade == gate_mod.UNVERIFIED and no.kind == gate_mod.KIND_UNVERIFIED
    yes = gate_mod.gate_from_compat("claude", None, "google", L, allow_unverified=True)
    assert yes.allowed and not yes.verified and yes.grade == gate_mod.UNVERIFIED


def test_skip_and_instructions_only_cells_cannot_serve_a_role_even_with_the_flag():
    skip = gate_mod.gate_from_compat("claude", None, "google", compat.SINGLE, allow_unverified=True)
    assert not skip.allowed and skip.kind == gate_mod.KIND_COMPAT_SKIP
    codex = gate_mod.gate_from_compat("codex", None, "openai", L, allow_unverified=True)
    assert not codex.allowed and codex.kind == gate_mod.KIND_COMPAT_SKIP


def test_engine_cell_is_read_through_the_engines_table():
    assert gate_mod.gate_from_compat("openclaw", "claude-code", "anthropic", L).allowed
    assert not gate_mod.gate_from_compat("openclaw", "claude-code", "google", L).allowed
    assert gate_mod.gate_from_compat("openclaw", "codex", "openai", L).allowed


def test_a_provider_outside_the_matrix_is_skipped_not_raised():
    assert not gate_mod.gate_from_compat("claude", None, "nonexistent", L).allowed


def test_the_gate_does_not_change_the_matrix():
    before = compat.markdown_table()
    gate_mod.gate_from_compat("claude", None, "google", L, allow_unverified=True)
    gate_mod.make_gate(scores_db.ScoresDB(scores_db.load_shipped()))(SONNET, "anthropic-direct")
    assert compat.markdown_table() == before


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("allow", [False, True])
def test_every_chosen_cell_passes_compat_plan_with_no_skip(mode, allow):
    rows = []
    for role in ("planner", "implementer", "tester", "code-reviewer", "explore"):
        rows += [row(role, SONNET, 6.4), row(role, OPUS, 5.8), row(role, HAIKU, 5.9), row(role, GEMINI, 6.0),
                 row(role, SOL, 6.1)]
    db = fixture_db(rows)
    comp = compose(make_input(db, mode=mode, allow_unverified=allow, enabled=("anthropic", "google", "openai"),
                              roles=("planner", "implementer", "tester", "code-reviewer", "explore")))
    assert comp.assignments
    from ai_resources import selection as sel
    for a in comp.assignments:
        provider = db.compat_provider(a.model, a.route)
        s = sel.Selection(shape="single", slots={f"{provider}:x": sel.SlotSel(ref=a.model)})
        actions = compat.plan(s, "multi-model", "litellm", ["claude"], allow_unverified=allow)
        assert actions[0].action != compat.SKIP, (a.role, a.model, a.route)
        assert (actions[0].unverified) == (not a.verified)

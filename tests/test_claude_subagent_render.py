"""The render-only subagent pass: the S12 re-render diffs it against disk, so it must equal what setup writes."""
from __future__ import annotations

from pathlib import Path

import pytest

from ai_resources.setup.cockpits import claude

EXECUTORS = {"by_role": {"implementer": {"model": "google/gemini-3.7-flash"}, "planner": {"model": "claude-opus-5"}},
             "by_persona": {"code-reviewer-angular": {"model": "anthropic/claude-sonnet-5"}}}


@pytest.fixture
def agents(tmp_path, monkeypatch):
    monkeypatch.setattr(claude, "AGENTS_DIR", tmp_path / "agents")
    return tmp_path / "agents"


def test_the_render_equals_what_the_writer_puts_on_disk(agents):
    rendered = claude.render_subagent_files(EXECUTORS, "/kit", "multi-model")
    assert not agents.exists()                                     # render-only: nothing was created
    claude._generate_subagent_files(EXECUTORS, "/kit", "multi-model")
    on_disk = {p.stem: p.read_text(encoding="utf-8") for p in agents.glob("*.md")}
    assert rendered == on_disk and len(rendered) == 40


def test_the_model_line_follows_the_executors(agents):
    rendered = claude.render_subagent_files(EXECUTORS, "/kit", "multi-model")
    assert "model: google/gemini-3.7-flash" in rendered["implementer"]
    assert "model: anthropic/claude-sonnet-5" in rendered["code-reviewer-angular"]
    assert "model: claude-opus-5" in rendered["planner"]


def test_a_persona_inherits_its_roles_model_in_the_render(agents):
    rendered = claude.render_subagent_files(EXECUTORS, "/kit", "multi-model")
    assert "model: google/gemini-3.7-flash" in rendered["implementer-angular"]


def test_single_model_aliases_render_as_aliases(agents):
    rendered = claude.render_subagent_files({"by_role": {"implementer": {"model": "sonnet"}}}, "/kit", "single-model")
    assert "model: sonnet" in rendered["implementer"]


def test_strict_single_model_refuses_a_non_native_id(agents):
    from ai_resources.setup.cockpits import _shared
    with pytest.raises(_shared.SkipCockpit):
        claude.render_subagent_files(EXECUTORS, "/kit", "single-model", strict=True)
    legacy = claude.render_subagent_files(EXECUTORS, "/kit", "single-model", strict=False)
    assert "model: claude-opus-5" in legacy["planner"] and "gemini" not in legacy["implementer"]   # the legacy fallback


def test_a_description_with_quotes_is_escaped_in_the_text():
    text = claude._subagent_text("x", 'say "hi"\nnow', "Read", "sonnet", "body")
    assert 'description: "say \\"hi\\" now"' in text and text.endswith("body")

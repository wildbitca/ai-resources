"""The kit-managed block in ~/.claude/CLAUDE.md: small, safe, polling guidance once, operator text untouched."""
from __future__ import annotations

import pytest

from ai_resources.setup.cockpits import _shared, claude


@pytest.mark.parametrize("mode", ["single-model", "multi-model"])
def test_the_block_stays_small(mode):
    md = claude._claude_md("/kit", "http://127.0.0.1:4000", mode)
    assert len(md.splitlines()) < 200 and len(md) < 4000


def test_the_block_keeps_the_mandatory_safety_rules():
    md = claude._claude_md("/kit", "http://gw", "single-model")
    assert "Never block one tool call on a long wait" in md
    assert "never call the native AskUserQuestion" in md


def test_the_polling_guidance_appears_exactly_once():
    md = claude._claude_md("/kit", "http://gw", "multi-model")
    assert md.count("## Waiting") == 1 and md.count("sessions_yield") == 1
    assert len(claude.POLLING_GUIDANCE.splitlines()) < 10


def test_workflows_are_reachable_without_listing_their_descriptions():
    md = claude._claude_md("/kit", "http://gw", "single-model")
    assert "/kit-plan" in md and "/workflow-<name>" in md and "/kit/skills/<id>/SKILL.md" in md


def test_operator_text_outside_the_markers_is_untouched_and_a_second_run_is_a_no_op(tmp_path):
    path = tmp_path / "CLAUDE.md"
    mine = "# My rules\n\n- ClickUp: always English\n- Clusters: only wildbit\n"
    path.write_text(mine)
    md = claude._claude_md("/kit", "http://gw", "single-model")
    assert _shared.write_managed_block(path, md) is True
    first = path.read_text()
    assert mine.strip() in first and first.count(_shared.MANAGED_BEGIN) == 1
    assert _shared.write_managed_block(path, md) is False
    assert path.read_text() == first


def test_a_block_written_by_an_older_kit_is_replaced_not_duplicated(tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_text(f"{_shared.MANAGED_BEGIN}\n# ai-resources (Claude Code)\n\n## Using the kit\n\nold\n{_shared.MANAGED_END}\n\nmine\n")
    _shared.write_managed_block(path, claude._claude_md("/kit", "http://gw", "single-model"))
    text = path.read_text()
    assert "old" not in text and text.count("## Using the kit") == 1 and text.rstrip().endswith("mine")

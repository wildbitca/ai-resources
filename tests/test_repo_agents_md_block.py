"""The kit block committed at the top of this repo's own AGENTS.md (v1.10.3).

This repo is the workspace of the `ai` OpenClaw agent, so `ai-resources setup` writes the kit
block into its AGENTS.md. That block is a repo artifact: this test fails when it drifts from
what the renderer produces. It reads a real file of the checkout and touches no host state.
    pytest tests/test_repo_agents_md_block.py -q
"""
from __future__ import annotations

import re
from pathlib import Path

from ai_resources.setup.cockpits import _shared

REPO_AGENTS_MD = Path(__file__).resolve().parent.parent / "AGENTS.md"
KIT_ROOT_LINE = re.compile(r"^Kit root: `([^`]+)`", re.MULTILINE)


def _committed_block() -> tuple[str, str]:
    """(the marker-to-marker span of AGENTS.md, exactly as written; the kit root it names)."""
    text = REPO_AGENTS_MD.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    begin, end = _shared._managed_block_span(lines)
    assert begin == 0 and end is not None, "AGENTS.md must open with one complete kit block"
    span = "".join(lines[begin:end + 1])
    root = KIT_ROOT_LINE.search(span)
    assert root, "the committed block must name its kit root"
    return span, root.group(1)


def _rendered(kit_root: str) -> str:
    # Same shape as `write_managed_block`: markers around the stripped body, one line each.
    body = _shared.openclaw_agent_kit_md(kit_root, delegates=True)
    return "\n".join([_shared.MANAGED_BEGIN, *body.strip().splitlines(), _shared.MANAGED_END]) + "\n"


def test_the_committed_repo_agents_md_block_is_what_the_kit_renders():
    span, kit_root = _committed_block()
    assert span == _rendered(kit_root), (
        "AGENTS.md drifted from the renderer: run `ai-resources setup` and commit the result")


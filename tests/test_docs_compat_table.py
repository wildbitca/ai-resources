"""The docs and rule 017 say what compat.py says: one table, no hand-kept copies."""
from __future__ import annotations

import pathlib
import re

from ai_resources.setup import compat

REPO = pathlib.Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "multi-model.md"
RULE = REPO / "rules" / "017-multimodel-routing.mdc"
BEGIN, END = "<!-- compat-matrix:begin -->\n", "<!-- compat-matrix:end -->"


def test_the_docs_table_equals_the_generated_table_byte_for_byte():
    text = DOC.read_text(encoding="utf-8")
    assert BEGIN in text and END in text
    embedded = text[text.index(BEGIN) + len(BEGIN):text.index(END)]
    assert embedded == compat.markdown_table(), (
        "docs/multi-model.md is out of date; regenerate the block between the compat-matrix markers "
        "from scripts/ai_resources/setup/compat.py (markdown_table())")


def test_the_docs_no_longer_say_every_cockpit_talks_to_a_gateway():
    text = DOC.read_text(encoding="utf-8")
    assert "The cockpit always talks to a gateway" not in text


def test_rule_017_no_longer_routes_cursor_or_gemini_cli_through_litellm():
    text = RULE.read_text(encoding="utf-8")
    assert "Cockpit (Claude Code / Cursor / Gemini CLI / etc.)" not in text
    assert not re.search(r"(Cursor|Gemini CLI)[^\n]*(through|via|behind) (LiteLLM|the gateway)", text), \
        "rule 017 must not say Cursor or Gemini CLI route through LiteLLM"
    assert "ANTHROPIC_BASE_URL=http://127.0.0.1:4000" not in text.split("## Single source of truth")[0].split("Cursor")[0]


def test_rule_017_points_at_the_matrix_and_names_the_stale_example_ids_no_more():
    text = RULE.read_text(encoding="utf-8")
    assert "compat.py" in text
    for stale in ("claude-sonnet-4-6", "claude-opus-4-7", "gemini-2.5-pro"):
        assert stale not in text, stale


def test_the_generated_instruction_text_matches_the_matrix_wording():
    from ai_resources.setup.cockpits import _shared
    md = _shared.multimodel_protocol_md("/kit", "http://gw", "multi-model", "instructions_only")
    assert "uses its own model settings" in md and "go through" not in md

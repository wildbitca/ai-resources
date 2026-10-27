"""One source of truth for sizing: scorecard.json holds the rules and the spelling tables, sizing-core.js.inc the script logic."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ai_resources import generate, scorecard

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = ("kit-plan.js", "kit-implement.js")
RULES = scorecard.load_rules()


def _block(text: str) -> str:
    start = text.index("// BEGIN sizing-core")
    return text[start:text.index(generate.SIZING_CORE_END) + len(generate.SIZING_CORE_END)]


@pytest.mark.parametrize("name", SCRIPTS)
def test_scripts_carry_the_generated_sizing_block(name):
    text = (REPO / "workflows" / "scripts" / name).read_text(encoding="utf-8")
    assert _block(text) == generate.render_sizing_core(), "run `ai-resources generate`"
    assert generate.splice_sizing_core(text) == text


def test_both_scripts_carry_the_same_block():
    blocks = {_block((REPO / "workflows" / "scripts" / n).read_text(encoding="utf-8")) for n in SCRIPTS}
    assert len(blocks) == 1


def test_splice_without_markers_is_an_error():
    with pytest.raises(ValueError):
        generate.splice_sizing_core("export const meta = {}\n")


def test_spelling_table_covers_the_modes_and_the_script_names():
    assert set(RULES["size_flags"]) == {"inline", "lite", "light", "standard", "full"}
    assert set(RULES["sizes"]) == set(scorecard.MODES) and set(RULES["size_flags"].values()) == set(RULES["sizes"].values())
    assert {name: scorecard.mode_of_spelling(name) for name in RULES["size_flags"]} == {
        "inline": "lite", "lite": "lite", "light": "standard", "standard": "standard", "full": "full"}
    assert scorecard.mode_of_spelling("bogus") is None and scorecard.mode_of_spelling(None) is None


def test_abbreviations_come_from_the_rules():
    assert scorecard.ABBREV == RULES["abbrev"] and set(RULES["abbrev"]) == set(RULES["dimensions"])
    block = _block((REPO / "workflows" / "scripts" / "kit-plan.js").read_text(encoding="utf-8"))
    assert json.loads(re.search(r"^const SCORECARD = (\{.*\})$", block, re.M).group(1))["abbrev"] == RULES["abbrev"]

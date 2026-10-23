"""ADR-0016 D4: ~/.gemini/GEMINI.md gets the kit's managed block (and the operator rules) with the operator's own text
outside the markers untouched. No GEMINI.md test existed before round 4."""
from __future__ import annotations

import pytest

from ai_resources.setup import operator_rules as rules, state
from ai_resources.setup.cockpits import _shared, gemini

BEGIN = "<!-- BEGIN ai-resources"
END = "<!-- END ai-resources -->"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(_shared, "link_agents_skills", lambda *a, **k: None)
    return tmp_path / "home"


def run():
    s = state.SetupState()
    gemini.configure({"state": s, "gateway_url": ""})
    return gemini.GEMINI_MD_PATH.read_text(encoding="utf-8")


def test_a_fresh_gemini_md_is_only_the_managed_block_in_the_scratch_home(home):
    assert str(gemini.GEMINI_MD_PATH).startswith(str(gemini.CONFIG_ROOT))
    assert not str(gemini.GEMINI_MD_PATH).startswith(str(home))                 # the suite's redirect, not the real HOME
    text = run()
    assert text.count(BEGIN) == 1 and text.count(END) == 1
    assert "# ai-resources (Gemini CLI)" in text and "## Operator rules" not in text


def test_the_operators_own_lines_outside_the_markers_survive_every_run(home):
    gemini.GEMINI_MD_PATH.parent.mkdir(parents=True, exist_ok=True)
    gemini.GEMINI_MD_PATH.write_text("# Mine\n\nalways say hi\n", encoding="utf-8")
    text = run()
    assert "always say hi" in text and text.count(BEGIN) == 1
    assert run() == text                                                         # idempotent


def test_the_operator_rules_are_rendered_inside_the_block_and_follow_the_file(home):
    rules.create("# Operator rules\n\n## IDIOMA\n\nEnglish artifacts.\n")
    text = run()
    inside = text[text.index(BEGIN):text.index(END)]
    assert "## Operator rules" in inside and "English artifacts." in inside
    rules.path().write_text("# Operator rules\n\n## IDIOMA\n\nSpanish chat only.\n", encoding="utf-8")
    again = run()
    assert "Spanish chat only." in again and "English artifacts." not in again
    assert again.count(BEGIN) == 1


def test_removing_the_rules_file_removes_the_section_on_the_next_run(home):
    rules.create("# Operator rules\n\n## IDIOMA\n\nEnglish artifacts.\n")
    run()
    rules.path().unlink()
    assert "## Operator rules" not in run()

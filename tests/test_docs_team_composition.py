"""The composition docs exist, link to each other and name the flags the CLI really has (AC-19)."""
from __future__ import annotations

import pathlib
import re

from ai_resources import cli

REPO = pathlib.Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "team-composition.md"
ADR = REPO / "docs" / "decisions" / "0006-scored-team-composition.md"
README = REPO / "scores" / "README.md"
FLAGS = ("--compose", "--headroom", "--headroom-live", "--why-json", "--allow-fable")


def test_the_three_documents_exist_and_the_adr_has_a_status():
    for p in (DOC, ADR, README, REPO / "scores" / "model-scores.yaml"):
        assert p.is_file(), p
    assert re.search(r"\*\*Status\*\*: (Proposed|Accepted)", ADR.read_text())


def test_every_composition_flag_is_a_real_setup_flag_and_documented():
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if getattr(a, "choices", None) and "setup" in a.choices)
    help_text = sub.choices["setup"].format_help()
    guide = (REPO / "docs" / "multi-model.md").read_text()
    for flag in FLAGS:
        assert flag in help_text, flag
        assert flag in guide or flag in DOC.read_text(), flag


def test_documents_state_the_load_bearing_facts_plainly():
    text = DOC.read_text()
    for needle in ("unverified today", "direct use only", "instructions only", "agentId", "Phase B", "static level"):
        assert needle in text, needle


def test_multi_model_guide_links_the_composition_doc_and_keeps_the_compat_block_markers():
    guide = (REPO / "docs" / "multi-model.md").read_text()
    assert "(team-composition.md)" in guide
    assert "<!-- compat-matrix:begin -->" in guide and "<!-- compat-matrix:end -->" in guide

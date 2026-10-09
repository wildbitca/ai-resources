"""The version the code reports, the top CHANGELOG heading and the Homebrew formula agree."""
from __future__ import annotations

import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources import __version__  # noqa: E402


def _top_changelog_version() -> str:
    return re.search(r"^## \[(\d+\.\d+\.\d+)\]", (REPO / "CHANGELOG.md").read_text(encoding="utf-8"), re.M).group(1)


def test_the_reported_version_is_the_top_changelog_heading():
    assert __version__ == _top_changelog_version()


def test_the_formula_points_at_the_same_version_and_tag():
    formula = (REPO / "Formula" / "ai-resources.rb").read_text(encoding="utf-8")
    assert re.search(r'^\s*version "([^"]+)"', formula, re.M).group(1) == __version__
    assert re.search(r'tag: "v([^"]+)"', formula).group(1) == __version__


def test_the_formula_pins_no_revision_that_only_exists_after_tagging():
    formula = (REPO / "Formula" / "ai-resources.rb").read_text(encoding="utf-8")
    assert "revision:" not in formula


def test_the_claude_plugin_manifest_carries_the_same_version_and_the_kit_license():
    manifest = json.loads((REPO / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["version"] == __version__
    assert manifest["license"] == "Apache-2.0"
    assert "Apache License" in (REPO / "LICENSE").read_text(encoding="utf-8").splitlines()[0]

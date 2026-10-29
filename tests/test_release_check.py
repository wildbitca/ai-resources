"""Unit tests for scripts/ci/release_check.py (pure logic, no network)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "ci"))
import release_check as rc  # noqa: E402

PKG = '"""x"""\n__version__ = "1.2.3"\n'
PLUGIN = json.dumps({"name": "x", "version": "1.2.3"})
FORMULA = '  url "https://x.git",\n      tag: "v1.2.3"\n  version "1.2.3"\n'
CHANGELOG = "# Changelog\n\n## [Unreleased]\n\n## [1.2.3] — 2026-01-01 — Title\n\n### Fixed\n- a\n\n## [1.2.2] — old\n- b\n"


def test_consistent_release_passes_and_extracts_notes():
    version, notes, errors = rc.check(PKG, PLUGIN, FORMULA, CHANGELOG)
    assert (version, errors) == ("1.2.3", [])
    assert notes.strip() == "### Fixed\n- a"
    assert "1.2.2" not in notes


def test_plugin_mismatch_reported():
    _, _, errors = rc.check(PKG, json.dumps({"version": "1.2.2"}), FORMULA, CHANGELOG)
    assert any("plugin.json" in e for e in errors)


def test_formula_tag_and_version_mismatch_reported():
    bad = FORMULA.replace('tag: "v1.2.3"', 'tag: "v1.2.2"').replace('version "1.2.3"', 'version "1.2.2"')
    _, _, errors = rc.check(PKG, PLUGIN, bad, CHANGELOG)
    assert sum("Formula" in e for e in errors) == 2


def test_missing_changelog_section_reported():
    _, notes, errors = rc.check(PKG, PLUGIN, FORMULA, "## [Unreleased]\n")
    assert notes is None and any("no '## [1.2.3]'" in e for e in errors)


def test_empty_changelog_section_reported():
    _, _, errors = rc.check(PKG, PLUGIN, FORMULA, "## [1.2.3] — d\n\n## [1.2.2]\n- x\n")
    assert any("empty" in e for e in errors)


def test_non_semver_version_rejected():
    _, _, errors = rc.check('__version__ = "1.2"\n', PLUGIN, FORMULA, CHANGELOG)
    assert errors and "X.Y.Z" in errors[0]


def test_last_section_runs_to_end_of_file():
    assert rc.extract_notes("## [1.0.0] — d\n- only\n", "1.0.0") == "- only"


def test_main_writes_notes_and_prints_outputs(tmp_path, capsys):
    root = tmp_path / "repo"
    (root / "scripts/ai_resources").mkdir(parents=True)
    (root / ".claude-plugin").mkdir()
    (root / "Formula").mkdir()
    (root / "scripts/ai_resources/__init__.py").write_text(PKG)
    (root / ".claude-plugin/plugin.json").write_text(PLUGIN)
    (root / "Formula/ai-resources.rb").write_text(FORMULA)
    (root / "CHANGELOG.md").write_text(CHANGELOG)
    notes = tmp_path / "notes.md"
    assert rc.main(["--root", str(root), "--notes-file", str(notes)]) == 0
    assert capsys.readouterr().out.split() == ["version=1.2.3", "tag=v1.2.3"]
    assert "### Fixed" in notes.read_text()


def test_main_fails_on_missing_file(tmp_path):
    assert rc.main(["--root", str(tmp_path), "--notes-file", str(tmp_path / "n")]) == 2

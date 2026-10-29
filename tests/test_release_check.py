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


def test_a_version_with_a_trailing_newline_or_non_ascii_digits_is_rejected():
    assert rc.read_package_version('__version__ = "4.0.0\n"\n') is None
    for bad in ('__version__ = "4.0.0\n"\n', '__version__ = "4.0.\u0663"\n', '__version__ = "4.0.0 "\n'):
        _, _, errors = rc.check(bad, PLUGIN, FORMULA, CHANGELOG)
        assert errors and "X.Y.Z" in errors[0], bad
    assert not rc.SEMVER.fullmatch("4.0.0\n")


def test_last_section_runs_to_end_of_file():
    assert rc.extract_notes("## [1.0.0] — d\n- only\n", "1.0.0") == "- only"


def test_main_writes_notes_and_prints_outputs(tmp_path, capsys):
    root = tmp_path / "repo"
    (root / "scripts/ai_resources").mkdir(parents=True)
    (root / ".claude-plugin").mkdir()
    (root / "Formula").mkdir()
    (root / "scripts/ai_resources/__init__.py").write_text(PKG, encoding="utf-8")
    (root / ".claude-plugin/plugin.json").write_text(PLUGIN, encoding="utf-8")
    (root / "Formula/ai-resources.rb").write_text(FORMULA, encoding="utf-8")
    (root / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    notes = tmp_path / "notes.md"
    assert rc.main(["--root", str(root), "--notes-file", str(notes)]) == 0
    assert capsys.readouterr().out.split() == ["version=1.2.3", "tag=v1.2.3"]
    assert "### Fixed" in notes.read_text(encoding="utf-8")


def test_main_fails_on_missing_file(tmp_path):
    assert rc.main(["--root", str(tmp_path), "--notes-file", str(tmp_path / "n")]) == 2


# Extraction an operator would write in a workflow shell step; it must agree with extract_notes.
AWK_NOTES = (
    'index($0, "## [" v "]") == 1 {f=1; next} '
    'f && index($0, "## [") == 1 {exit} '
    "f {print}"
)


def _awk_notes(changelog: str, version: str) -> str:
    import shutil
    import subprocess

    import pytest

    awk = shutil.which("awk")
    if not awk:
        pytest.skip("awk not available")
    out = subprocess.run(
        [awk, "-v", f"v={version}", AWK_NOTES], input=changelog.encode("utf-8"), capture_output=True, check=True
    ).stdout.decode("utf-8")
    return out.strip("\n")


def test_em_dash_header_is_matched_exactly_as_in_the_real_changelog():
    changelog = "## [Unreleased]\n\n## [3.2.11] — 2026-10-29 — Backup stage cleanup\n\n### Fixed\n- é ü — x\n\n## [3.2.10] — 2026-10-01\n- old\n"
    notes = rc.extract_notes(changelog, "3.2.11")
    assert notes == "### Fixed\n- é ü — x"
    assert rc.extract_notes(changelog, "3.2.1") is None  # no prefix match on a longer version


def test_awk_extraction_gives_the_same_notes_as_extract_notes():
    changelog = "## [3.2.11] — 2026-10-29 — Backup stage cleanup\n\n### Fixed\n- é — x\n\n## [3.2.10] — 2026-10-01\n- old\n"
    assert _awk_notes(changelog, "3.2.11") == rc.extract_notes(changelog, "3.2.11")
    real = (Path(__file__).resolve().parents[1] / "CHANGELOG.md").read_text(encoding="utf-8")
    assert _awk_notes(real, "3.2.11") == rc.extract_notes(real, "3.2.11")


def test_main_writes_utf8_notes_regardless_of_locale(tmp_path, monkeypatch):
    root = tmp_path
    (root / "scripts/ai_resources").mkdir(parents=True)
    (root / ".claude-plugin").mkdir()
    (root / "Formula").mkdir()
    (root / "scripts/ai_resources/__init__.py").write_text(PKG, encoding="utf-8")
    (root / ".claude-plugin/plugin.json").write_text(PLUGIN, encoding="utf-8")
    (root / "Formula/ai-resources.rb").write_text(FORMULA, encoding="utf-8")
    (root / "CHANGELOG.md").write_text(CHANGELOG.replace("- a", "- a — é"), encoding="utf-8")
    notes = tmp_path / "notes.md"
    assert rc.main(["--root", str(root), "--notes-file", str(notes)]) == 0
    assert "a — é" in notes.read_bytes().decode("utf-8")

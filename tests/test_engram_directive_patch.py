"""Engram directive trim: hash-guarded, backed up, idempotent, best-effort."""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from ai_resources.setup import engram_patch

ORIGINAL_LINE = (
    "  printf '%s\\n' '{\"systemMessage\":\"CRITICAL FIRST ACTION — Execute this ToolSearch NOW before responding to the "
    "user:\\nselect:mcp__engram__mem_save,mcp__engram__mem_search,mcp__engram__mem_context,mcp__engram__mem_session_summary,"
    "mcp__engram__mem_session_start,mcp__engram__mem_session_end,mcp__engram__mem_get_observation,"
    "mcp__engram__mem_suggest_topic_key,mcp__engram__mem_capture_passive,mcp__engram__mem_save_prompt,"
    "mcp__engram__mem_update,mcp__engram__mem_current_project,mcp__engram__mem_judge\\n\\nAfter loading tools, call "
    "mem_context to check for prior session history before responding.\"}'"
)
SCRIPT = f"#!/bin/bash\nprint_toolsearch_message() {{\n{ORIGINAL_LINE}\n}}\necho '{{}}'\n"


def test_the_fixture_line_is_the_one_the_kit_pins():
    assert hashlib.sha256((ORIGINAL_LINE + "\n").encode()).hexdigest() == engram_patch.ORIGINAL_LINE_SHA256


@pytest.fixture
def plugin(tmp_path):
    home = tmp_path / "home"
    scripts = home / ".claude" / "plugins" / "cache" / "engram" / "engram" / "0.1.1" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "user-prompt-submit.sh").write_text(SCRIPT)
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"plugins": {"engram@engram": [{"installPath": str(scripts.parent), "version": "0.1.1"}]}}))
    return home, scripts / "user-prompt-submit.sh"


def test_the_directive_is_removed_once_and_a_second_run_is_a_no_op(plugin):
    home, script = plugin
    first = engram_patch.trim_engram_hooks(home)
    assert list(first.values()) == ["patched"]
    text = script.read_text()
    assert "CRITICAL FIRST ACTION" not in text and "ToolSearch NOW" not in text
    assert "mem_context" in text and "echo '{}'" in text
    after_first = text
    assert list(engram_patch.trim_engram_hooks(home).values()) == ["already"]
    assert script.read_text() == after_first


def test_the_patched_script_is_valid_bash_and_valid_json(plugin):
    home, script = plugin
    engram_patch.trim_engram_hooks(home)
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True)
    assert r.returncode == 0
    out = subprocess.run(["bash", str(script)], capture_output=True, text=True).stdout
    assert out.strip() == "{}"
    line = engram_patch.REPLACEMENT_LINE.split("' '", 1)[1].rstrip("'")
    assert "systemMessage" in json.loads(line)


def test_a_backup_and_a_marker_with_the_plugin_version_are_written(plugin):
    home, script = plugin
    engram_patch.trim_engram_hooks(home)
    assert script.with_name(script.name + engram_patch.BACKUP_SUFFIX).read_text() == SCRIPT
    marker = json.loads((script.parent / engram_patch.MARKER).read_text())
    assert marker["plugin_version"] == "0.1.1"


def test_unknown_text_is_left_untouched_with_a_warning(plugin, monkeypatch):
    home, script = plugin
    changed = SCRIPT.replace("CRITICAL FIRST ACTION", "CRITICAL FIRST ACTION (v2)")
    script.write_text(changed)
    warned = []
    monkeypatch.setattr(engram_patch.ui, "warn", warned.append)
    assert list(engram_patch.trim_engram_hooks(home).values()) == ["unknown"]
    assert script.read_text() == changed and warned
    assert not script.with_name(script.name + engram_patch.BACKUP_SUFFIX).exists()


def test_a_script_without_the_directive_is_unknown_not_patched(plugin):
    home, script = plugin
    script.write_text("#!/bin/bash\necho '{}'\n")
    assert list(engram_patch.trim_engram_hooks(home).values()) == ["unknown"]
    assert script.read_text() == "#!/bin/bash\necho '{}'\n"


def test_an_absent_plugin_is_a_no_op(tmp_path):
    assert engram_patch.trim_engram_hooks(tmp_path / "nohome") == {}


def test_a_corrupt_install_record_is_a_no_op(tmp_path):
    rec = tmp_path / ".claude" / "plugins"
    rec.mkdir(parents=True)
    (rec / "installed_plugins.json").write_text("{not json")
    assert engram_patch.trim_engram_hooks(tmp_path) == {}


def test_nothing_outside_the_plugin_directory_is_written(plugin):
    home, script = plugin
    before = {p for p in home.rglob("*") if p.is_file()}
    engram_patch.trim_engram_hooks(home)
    new = {p for p in home.rglob("*") if p.is_file()} - before
    assert all(script.parent in p.parents for p in new)


def test_the_claude_cockpit_wrapper_never_raises(monkeypatch):
    from ai_resources.setup.cockpits import claude
    monkeypatch.setattr(engram_patch, "trim_engram_hooks", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert claude._trim_engram_hooks() == {}


# --- ADR-0014 S10: what the kit does NOT control in engram -----------------------------------------------------------

def test_mem_judge_only_ever_sat_in_the_toolsearch_line_the_kit_already_replaces():
    """The S0 spike found `mem_judge` named only inside the ToolSearch `select:` list of user-prompt-submit.sh. That
    line is replaced hash-guarded, so no second patch exists: the judge itself is server-side."""
    assert "mem_judge" in ORIGINAL_LINE and "mem_judge" not in engram_patch.REPLACEMENT_LINE
    assert engram_patch.REPLACEMENT_LINE.count("mem_") >= 1                    # the replacement still tells the model to read context


def test_the_kit_patches_one_known_line_and_the_unpatchable_parts_are_documented():
    """No hash-guardable site exists for the SessionStart `/context` payload (the script prints what the server returns
    and has no size knob), so the module stays a single-line patch and ADR-0014 lists the rest as not kit-controllable."""
    assert engram_patch.SCRIPT_NAME == "user-prompt-submit.sh"
    assert "SessionStart" in engram_patch.__doc__ and "not controllable" in engram_patch.__doc__
    adr = (Path(__file__).resolve().parents[1] / "docs" / "decisions" / "0014-token-cost-round-2.md").read_text()
    assert "Not kit-controllable" in adr and "SessionStart" in adr and "mem_judge" in adr

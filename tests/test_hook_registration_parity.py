"""The four places a kit hook is declared must agree.

A hook script is declared in `hooks/hooks.json` (the plugin route), in the Claude cockpit's
`*_HOOK_SCRIPTS` tuples (what `_is_kit_hook_command` / the OpenClaw predicate match), in the
dicts `_kit_hooks()` / `_openclaw_hooks()` build for `settings.json`, and as a file in
`hooks/`. When one drifts, a hook is either installed twice, never removed on teardown, or
registered against a file that does not exist. Run with:
    pytest tests/ -q
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

from ai_resources.setup.cockpits import claude  # noqa: E402

HOOKS_DIR = REPO / "hooks"


def _declared_scripts() -> tuple[str, ...]:
    """Every hook script the cockpit knows about, kit-wide and OpenClaw-specific."""
    return tuple(claude.KIT_HOOK_SCRIPTS) + tuple(getattr(claude, "OPENCLAW_HOOK_SCRIPTS", ()))


def _script_in(command: str) -> str:
    m = re.search(r"/hooks/([A-Za-z0-9_]+\.py)", command)
    assert m, f"hook command does not point into hooks/: {command}"
    return m.group(1)


def _scripts_from_hooks(hooks: dict) -> set[str]:
    return {_script_in(h["command"])
            for entries in hooks.values() for entry in entries for h in entry["hooks"]}


def _all_cockpit_hooks() -> dict:
    merged: dict[str, list] = {}
    builders = [claude._kit_hooks("/kit")]
    if hasattr(claude, "_openclaw_hooks"):
        builders.append(claude._openclaw_hooks("/kit", team=True, guard=True))
    for built in builders:
        for event, entries in built.items():
            merged.setdefault(event, []).extend(entries)
    return merged


def test_every_declared_script_exists_on_disk():
    for script in _declared_scripts():
        assert (HOOKS_DIR / script).is_file(), f"{script} is declared but missing from hooks/"


def test_every_script_on_disk_is_declared():
    on_disk = {p.name for p in HOOKS_DIR.glob("*.py")}
    assert on_disk == set(_declared_scripts()), (
        f"hooks/*.py and the cockpit tuples disagree: only on disk "
        f"{sorted(on_disk - set(_declared_scripts()))}, only declared "
        f"{sorted(set(_declared_scripts()) - on_disk)}")


def test_hooks_json_names_exactly_the_declared_scripts():
    doc = json.loads((HOOKS_DIR / "hooks.json").read_text(encoding="utf-8"))
    from_json = _scripts_from_hooks(doc["hooks"])
    assert from_json == set(_declared_scripts()), (
        f"hooks.json only {sorted(from_json - set(_declared_scripts()))}, "
        f"cockpit only {sorted(set(_declared_scripts()) - from_json)}")


def test_settings_builders_name_exactly_the_declared_scripts():
    built = _scripts_from_hooks(_all_cockpit_hooks())
    assert built == set(_declared_scripts()), (
        f"builders only {sorted(built - set(_declared_scripts()))}, "
        f"tuples only {sorted(set(_declared_scripts()) - built)}")


def test_hooks_json_and_settings_builders_register_the_same_events():
    doc = json.loads((HOOKS_DIR / "hooks.json").read_text(encoding="utf-8"))

    def pairs(hooks: dict) -> set[tuple[str, str, str]]:
        return {(event, entry.get("matcher", ""), _script_in(h["command"]))
                for event, entries in hooks.items() for entry in entries for h in entry["hooks"]}

    assert pairs(doc["hooks"]) == pairs(_all_cockpit_hooks())


def test_kit_predicate_recognises_every_kit_script_and_only_those():
    for script in claude.KIT_HOOK_SCRIPTS:
        assert claude._is_kit_hook_command(f'python3 "/x/hooks/{script}"')
    for script in getattr(claude, "OPENCLAW_HOOK_SCRIPTS", ()):
        assert not claude._is_kit_hook_command(f'python3 "/x/hooks/{script}"'), (
            f"{script} is opt-in: the always-on kit merge must not claim it")

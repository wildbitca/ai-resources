"""openclaw.plugin.json must describe what index.js actually registers.

Node's own registration is exercised in openclaw-plugin/ai-resources/test/index.test.mjs
(run separately: `node --test`, local-only — see NODE in the plan). This file checks the
static shapes a plain Python read can verify without a JS runtime, so `pytest tests/` alone
still catches a manifest that drifted from the plugin.
"""
from __future__ import annotations

import json
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
PLUGIN_DIR = REPO / "openclaw-plugin" / "ai-resources"

FORBIDDEN_CLAUDE_FLAGS = ("--strict-mcp-config", "--setting-sources", "--disallowedTools", "--permission-mode")


def _manifest() -> dict:
    return json.loads((PLUGIN_DIR / "openclaw.plugin.json").read_text(encoding="utf-8"))


def _index_source() -> str:
    return (PLUGIN_DIR / "index.js").read_text(encoding="utf-8")


def test_manifest_is_valid_json_with_the_required_fields():
    manifest = _manifest()
    assert manifest["id"] == "ai-resources"
    assert set(manifest["cliBackends"]) == {"agy-cli", "claude-kit"}
    assert manifest["activation"]["onStartup"] is True
    assert manifest["minGatewayVersion"]


def test_manifest_backend_ids_match_the_ids_registered_in_index_js():
    manifest = _manifest()
    registered = set(re.findall(r'id:\s*"([a-z0-9-]+-cli|claude-kit)"', _index_source()))
    assert set(manifest["cliBackends"]) == registered


def test_activation_onstartup_is_true_not_false():
    # S0 decision: with activation.onStartup false the gateway reported
    # "Unknown CLI backend: agy-cli" — this must never regress silently.
    assert _manifest()["activation"] == {"onStartup": True}


def test_claude_kit_args_never_carry_the_flags_core_would_inject_for_the_bundled_backend():
    """AC-03 (static half): scan the literal args arrays in index.js for claude-kit."""
    src = _index_source()
    # Isolate the claude-kit backend builder body so an unrelated string match in the
    # agy-cli section (e.g. inside a comment) can't hide a real regression.
    start = src.index("export function buildClaudeBackend")
    end = src.index("\n// --- /claude and /equipo", start)
    body = src[start:end]
    assert '"--dangerously-skip-permissions"' in body
    for flag in FORBIDDEN_CLAUDE_FLAGS:
        assert flag not in body, f"forbidden flag present in claude-kit backend: {flag}"


def test_package_json_declares_an_esm_module_matching_the_import_export_syntax_in_index_js():
    package = json.loads((PLUGIN_DIR / "package.json").read_text(encoding="utf-8"))
    assert package.get("type") == "module"


def test_bridge_script_exists_and_is_stdlib_only():
    bridge_src = (PLUGIN_DIR / "bridge.py").read_text(encoding="utf-8")
    # Only modules from the Python standard library may be imported: the bridge runs
    # under whatever interpreter `agy mcp add` was pointed at, which may have no venv.
    import_lines = [l for l in bridge_src.splitlines() if l.startswith(("import ", "from "))]
    stdlib = {"json", "os", "re", "sys", "urllib.request", "__future__"}
    for line in import_lines:
        mod = line.split()[1].split(".")[0]
        assert mod in {m.split(".")[0] for m in stdlib}, f"non-stdlib import in bridge.py: {line}"


def test_package_json_declares_the_openclaw_extensions_entry_point():
    """`openclaw plugins install` refuses a package without `openclaw.extensions`
    ("package.json missing openclaw.extensions"), and it must point at the real entry
    file — verified live on OpenClaw 2026.9.4."""
    package = json.loads((PLUGIN_DIR / "package.json").read_text(encoding="utf-8"))
    extensions = package.get("openclaw", {}).get("extensions")
    assert extensions == ["./index.js"], extensions
    for entry in extensions:
        assert (PLUGIN_DIR / entry).is_file(), entry

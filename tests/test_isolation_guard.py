"""ADR-0016 S2: no path the setup code resolves may sit under the operator's real HOME during a test.

The conftest redirects every path function and constant to a scratch directory; this test is the tripwire that fails
when a new writer is added without a redirect.
"""
from __future__ import annotations

import os
import pwd
from pathlib import Path

REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def _under_real_home(p) -> bool:
    try:
        Path(p).resolve().relative_to(REAL_HOME)
    except ValueError:
        return False
    return True


def _scratch_roots():
    """The scratch roots pytest hands out live under the system temp dir or the repo basetemp, never the real HOME
    config directories. A path under HOME is only a failure when it is one of the operator's own state trees."""
    return [REAL_HOME / n for n in (".claude", ".openclaw", ".gemini", ".config/ai-resources", ".local/share/ai-resources",
                                    ".local/state/ai-resources", ".cache/ai-resources")]


def _is_operators_tree(p) -> bool:
    return any(Path(p).resolve() == r or r in Path(p).resolve().parents for r in _scratch_roots())


def _paths():
    from ai_resources import agy_gate_evidence, model_pins
    from ai_resources.setup import agy_mcp, live_auth
    from ai_resources.setup.cockpits import claude, gemini
    from ai_resources.setup.cockpits import openclaw as oc
    out = {
        "claude.CONFIG_ROOT": claude.CONFIG_ROOT, "claude.SETTINGS_PATH": claude.SETTINGS_PATH,
        "claude.CLAUDE_MD_PATH": claude.CLAUDE_MD_PATH, "claude.AGENTS_DIR": claude.AGENTS_DIR,
        "openclaw.CONFIG_ROOT": oc.CONFIG_ROOT,
        "gemini.CONFIG_ROOT": gemini.CONFIG_ROOT, "gemini.GEMINI_MD_PATH": gemini.GEMINI_MD_PATH,
        "gemini.SETTINGS_PATH": gemini.SETTINGS_PATH, "gemini.AGY_MCP_PATH": gemini.AGY_MCP_PATH,
        "agy_mcp.mcp_path()": agy_mcp.mcp_path(), "model_pins.overlay_path()": model_pins.overlay_path(),
        "live_auth.agy_home()": live_auth.agy_home(),
    }
    try:
        from ai_resources.setup import operator_rules
    except ImportError:
        pass
    else:
        out["operator_rules.path()"] = operator_rules.path()
    return out


def test_no_resolved_setup_path_is_under_the_operators_real_trees():
    bad = {k: str(v) for k, v in _paths().items() if _is_operators_tree(v)}
    assert not bad, f"paths that reach the operator's real HOME during a test: {bad}"


def test_the_gate_record_module_has_no_host_overlay_at_all():
    from ai_resources import agy_gate_evidence as record_module
    assert not hasattr(record_module, "HOST_OVERLAY_ENABLED")
    assert not hasattr(record_module, "_host_default_record")


def test_the_live_runner_never_runs_a_real_binary():
    from ai_resources.setup import live_auth
    rc, out = live_auth.run(["openclaw", "models", "status", "--json"])
    assert rc == 127 and "not run by tests" in out
    rc, out = live_auth.run(["claude", "auth", "status"])
    assert rc == 0 and '"authMethod": "claude.ai"' in out and "@" not in out


def test_a_native_handoff_without_a_stub_fails_loudly():
    import pytest
    from ai_resources.setup import live_auth
    with pytest.raises(AssertionError, match="native flow"):
        live_auth.run_handoff(["claude"])


def test_a_host_binary_is_refused_as_if_it_were_not_installed():
    import subprocess

    import pytest
    for argv in (["systemctl", "--user", "is-enabled", "openclaw-gateway.service"],
                 ["npm", "install", "-g", "openclaw@latest"]):
        with pytest.raises(FileNotFoundError):
            subprocess.run(argv, capture_output=True, timeout=5)


def test_a_stub_binary_under_the_test_temp_dir_still_runs(tmp_path):
    import subprocess
    stub = tmp_path / "openclaw"
    stub.write_text("#!/bin/sh\necho stub $1\n")
    stub.chmod(0o755)
    out = subprocess.run([str(stub), "--version"], capture_output=True, text=True, timeout=5)
    assert out.stdout.strip() == "stub --version"
    on_path = subprocess.run(["openclaw", "x"], capture_output=True, text=True, timeout=5,
                             env={**os.environ, "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"})
    assert on_path.stdout.strip() == "stub x"


def test_the_refusal_reads_shell_strings_and_absolute_paths():
    from conftest import host_binary_refused
    roots = ("/nonexistent-root",)
    env = {"PATH": "/usr/bin:/bin"}
    assert host_binary_refused(["git", "status"], env, roots) is None
    assert host_binary_refused(["/usr/bin/systemctl", "--user"], env, roots) == "systemctl"
    assert host_binary_refused("/usr/bin/curl -s https://example.invalid", env, roots) == "curl"
    assert host_binary_refused([], env, roots) is None

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


def test_the_host_overlay_of_the_gate_record_is_off_during_tests():
    from ai_resources import agy_gate_evidence
    assert getattr(agy_gate_evidence, "HOST_OVERLAY_ENABLED", False) is False


def test_the_live_runner_never_runs_a_real_binary():
    from ai_resources.setup import live_auth
    rc, out = live_auth.run(["claude", "auth", "status"])
    assert rc == 127 and "not run by tests" in out


def test_a_native_handoff_without_a_stub_fails_loudly():
    import pytest
    from ai_resources.setup import live_auth
    with pytest.raises(AssertionError, match="native flow"):
        live_auth.run_handoff(["claude"])

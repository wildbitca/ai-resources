"""Integration pins for the OpenClaw facts the image-generation setup rests on (ADR-0011, plan I0).

INTENTIONALLY RETIRED. These ran a real `openclaw config patch --dry-run` against a scratch copy of the config, opt-in
through `AI_RESOURCES_LIVE_FACTS=1`. The suite now refuses host binaries (tests/conftest.py `_no_host_binaries`), so the
call would raise "host binary blocked in tests: openclaw" instead of reaching OpenClaw. They are kept as the record of
what each fact was and how it was checked; to re-check a fact, run the same dry run by hand against a scratch config
(`OPENCLAW_CONFIG_PATH=<scratch> openclaw config patch --stdin --dry-run`), never from pytest.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess

import pytest

OPENCLAW = shutil.which("openclaw")
pytestmark = pytest.mark.skip(
    reason="retired: host binaries are refused in tests, so the real `openclaw config patch --dry-run` cannot run here; "
           "use a dry run by hand against a scratch config instead")

REAL = pathlib.Path.home() / ".openclaw" / "openclaw.json"
TIMEOUT = 110


def _sha(path: pathlib.Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


@pytest.fixture(autouse=True)
def _real_config_is_never_touched():
    before = _sha(REAL)
    yield
    assert _sha(REAL) == before, "a dry run changed the operator's real openclaw.json"


@pytest.fixture
def scratch(tmp_path):
    cfg = tmp_path / "openclaw.json"
    cfg.write_text(json.dumps({"tools": {"deny": ["bundle-mcp"]}}), encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    return cfg, state


def patch(scratch, doc, *replace):
    cfg, state = scratch
    env = {**os.environ, "OPENCLAW_CONFIG_PATH": str(cfg), "OPENCLAW_STATE_DIR": str(state)}
    argv = [OPENCLAW, "config", "patch", "--stdin", "--dry-run"]
    for rp in replace:
        argv += ["--replace-path", rp]
    return subprocess.run(argv, input=json.dumps(doc), capture_output=True, text=True, env=env, timeout=TIMEOUT,
                          cwd=str(cfg.parent))


def test_a_tools_deny_list_patch_dry_run_is_accepted(scratch):
    proc = patch(scratch, {"tools": {"deny": ["bundle-mcp", "image_generate"]}}, "tools.deny")
    assert proc.returncode == 0, proc.stderr[-400:]


def test_b_a_non_string_element_is_rejected_by_the_patch_schema_check(scratch):
    """F13. If this ever passes, the patch-mode schema check is vacuous and `openclaw_tool_policy.check_shape` is the
    only validation (ADR-0011 decision 13)."""
    proc = patch(scratch, {"tools": {"deny": ["bundle-mcp", 7]}}, "tools.deny")
    assert proc.returncode != 0


def test_c_the_media_model_patch_dry_run_is_accepted(scratch):
    proc = patch(scratch, {"agents": {"defaults": {"mediaModels": {"image": {
        "primary": "openai/gpt-image-2", "timeoutMs": 180000}}}}}, "agents.defaults.mediaModels.image")
    assert proc.returncode == 0, proc.stderr[-400:]

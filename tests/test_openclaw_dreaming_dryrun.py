"""The memory-core dreaming fill against the real `openclaw config patch --dry-run` (scratch config, never the operator's)."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess

import pytest

from ai_resources.setup import cockpit_auth, state
from ai_resources.setup.cockpits import openclaw as oc

pytestmark = pytest.mark.integration

OPENCLAW = shutil.which("openclaw")
REAL = pathlib.Path.home() / ".openclaw" / "openclaw.json"


def _sha(path: pathlib.Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def test_the_dreaming_model_fill_is_accepted_by_a_real_config_patch_dry_run(tmp_path):
    if not OPENCLAW:
        pytest.skip("openclaw is not on PATH")
    g = state.SetupState()
    cockpit_auth.set_providers(g, {"google": "api_key"})
    patch = oc._dreaming_fill({}, g)
    assert patch is not None
    cfg = tmp_path / "openclaw.json"
    cfg.write_text(json.dumps({}), encoding="utf-8")
    (tmp_path / "state").mkdir()
    env = {**os.environ, "OPENCLAW_CONFIG_PATH": str(cfg), "OPENCLAW_STATE_DIR": str(tmp_path / "state")}
    before = _sha(REAL)
    try:
        proc = subprocess.run([OPENCLAW, "config", "patch", "--stdin", "--dry-run"], input=json.dumps(patch), capture_output=True,
                              text=True, env=env, timeout=110, cwd=str(tmp_path))
    except FileNotFoundError as e:
        if "host binary blocked in tests" in str(e):
            pytest.skip("the test harness refuses host binaries; run this dry run by hand against a scratch config")
        raise
    assert _sha(REAL) == before, "a dry run changed the operator's real openclaw.json"
    assert proc.returncode == 0, proc.stderr[-400:] or proc.stdout[-400:]
    assert json.loads(cfg.read_text(encoding="utf-8")) == {}, "a dry run wrote the scratch config"

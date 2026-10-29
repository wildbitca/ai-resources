"""The node_scripts exemption lets through the workflow-script harness invocation and nothing else."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

# Integration: drives a real external tool, process or lock; skipped unless --integration (see tests/conftest.py).
pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[1]
HARNESS = str(REPO / "tests" / "kit_script_harness.mjs")
PLAN_JS = str(REPO / "workflows" / "scripts" / "kit-plan.js")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


@needs_node
def test_an_unmarked_test_cannot_run_node_at_all(tmp_path):
    with pytest.raises(FileNotFoundError, match="host binary blocked"):
        subprocess.run(["node", "-e", "1"], capture_output=True)
    with pytest.raises(FileNotFoundError, match="host binary blocked"):
        subprocess.run(["node", HARNESS, PLAN_JS, str(tmp_path / "c.json")], capture_output=True)


@needs_node
@pytest.mark.node_scripts
def test_a_marked_test_runs_only_the_harness_invocation(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text('{"args": {"goal": "g", "size": "inline"}, "responses": {}}')
    out = subprocess.run(["node", HARNESS, PLAN_JS, str(cfg)], capture_output=True, text=True, check=True, timeout=60)
    assert '"size":"inline"' in out.stdout
    for refused in (["node", "-e", "1"],                                   # arbitrary code
                    ["node", HARNESS, str(tmp_path / "evil.js"), str(cfg)],  # a script outside workflows/scripts
                    ["node", HARNESS, PLAN_JS, str(cfg), "--extra"],         # a different argument shape
                    ["bash", "-c", f"node -e 1"]):                           # through a shell
        with pytest.raises(FileNotFoundError, match="host binary blocked"):
            subprocess.run(refused, capture_output=True)

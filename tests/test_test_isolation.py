"""The suite must never reach the operator's real OpenClaw workspace (1.10.2).

`openclaw.teardown` removes the kit block from `workspace_dir(doc) / "AGENTS.md"`. With a config that
names no `agents.defaults.workspace` that is `CONFIG_ROOT / "workspace"`, and CONFIG_ROOT was the real
`~/.openclaw`: every run of the suite stripped the block from the live `main` AGENTS.md, which is the
regression v1.9.7 reported fixed. conftest now points CONFIG_ROOT at tmp_path for every test.
"""
from __future__ import annotations

import json
import os
import pwd
from pathlib import Path

from ai_resources.setup import state
from ai_resources.setup.cockpits import _shared, openclaw


def _real_openclaw_root() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".openclaw"


def test_the_openclaw_config_root_is_never_the_real_one():
    root = openclaw.CONFIG_ROOT.resolve()
    real = _real_openclaw_root().resolve()
    assert root != real and real not in root.parents, root


def test_a_teardown_with_no_configured_workspace_touches_only_tmp(tmp_path, monkeypatch):
    cfg = tmp_path / "openclaw.json"
    cfg.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(cfg))
    ws = openclaw.workspace_dir({})
    assert ws.resolve().is_relative_to(tmp_path.parent.resolve()), ws
    ws.mkdir(parents=True)
    agents_md = ws / "AGENTS.md"
    hand_written = "# Jarvis\n\nMine.\n"
    agents_md.write_text(f"{_shared.MANAGED_BEGIN}\nkit\n{_shared.MANAGED_END}\n\n{hand_written}", encoding="utf-8")
    s = state.SetupState()
    s.openclaw.mcp_mirrored = {"x": {"previous": None, "applied": {}}}
    monkeypatch.setattr(openclaw, "_openclaw", lambda *a, **k: (0, "ok"))
    openclaw.teardown(s)
    assert agents_md.read_text(encoding="utf-8") == hand_written   # the sandbox's own file, and only that

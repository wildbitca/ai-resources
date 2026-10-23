"""Ticking Antigravity (`agy`) in the wizard runs the module that writes its files (GEMINI.md, MCP file, skills)."""
from __future__ import annotations

import types

from ai_resources.setup import state, wizard
from ai_resources.setup.cockpits import gemini

from test_lanes_wiring import _lanes_state
from wizard_fakes import Script


def test_an_agy_tick_runs_the_agy_writer(monkeypatch):
    Script().install(monkeypatch)
    ran: list[str] = []
    fake = {cid: types.SimpleNamespace(ID=cid, NAME=cid, configure=(lambda ctx, cid=cid: ran.append(cid) or []))
            for cid in wizard.ALL_COCKPITS}
    monkeypatch.setattr(wizard, "ALL_COCKPITS", fake)
    s = _lanes_state()
    s.mode = "single-model"
    s.profile.customizations["__targets__"] = {"selected": ["agy"]}
    wizard._step9_apply(s)
    assert "gemini" in ran


def test_the_agy_settings_patch_never_forces_an_api_key_sign_in():
    assert "selectedAuthType" not in gemini._settings_patch()
    assert gemini._settings_patch("oauth-personal")["selectedAuthType"] == "oauth-personal"
